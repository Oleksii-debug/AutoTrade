"""Evidence-bound diagnostic reconstruction for the simulated AutoTrade runtime.

Diagnostics are reconstructed from durable state and journal events. They do
not depend on sampled logs and they never authorize financial actions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from .persistence import JournalStore


_REDACTION_MARKERS = (
    "password",
    "secret",
    "token",
    "apikey",
    "authorization",
    "credential",
    "cookie",
    "privatekey",
)


def _normalized_key(value: object) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def _is_sensitive_key(value: object) -> bool:
    normalized = _normalized_key(value)
    return any(marker in normalized for marker in _REDACTION_MARKERS)


_EMBEDDED_SECRET_PATTERNS = (
    re.compile(
        r"""(?i)(?:["'])?\b(authorization|proxy-authorization)\b"""
        r"""(?:["'])?\s*[:=]\s*"""
        r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\r\n]+)"""
    ),
    re.compile(
        r"(?i)\b(https?://)[^/@\s]+@"
    ),
    re.compile(
        r"""(?i)(?:["'])?\b(api[_-]?key|token|access[_-]?token|refresh[_-]?token|session|session[_-]?token|"""
        r"""secret|credential|api[_-]?secret|client[_-]?secret|private[_-]?key|password)\b(?:["'])?\s*[:=]\s*"""
        r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^&\s;,}\]]+)"""
    ),
)

_PRIVATE_KEY_MARKERS = (
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN ENCRYPTED PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN DSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
)


def _redact_structured_json_text(value: str) -> str | None:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, (dict, list)):
        return None
    redacted = redact_diagnostic_value(decoded)
    if redacted == decoded:
        return None
    return json.dumps(
        redacted,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _redact_structured_url_query(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not parsed.query:
        return None

    changed = False
    query_parts: list[str] = []
    for part in parsed.query.split("&"):
        raw_key, separator, raw_value = part.partition("=")
        if _is_sensitive_key(unquote_plus(raw_key)):
            query_parts.append(f"{raw_key}{separator or '='}[REDACTED]")
            changed = True
        else:
            query_parts.append(part)
    if not changed:
        return None
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "&".join(query_parts), parsed.fragment)
    )


def _redact_embedded_secret_text(value: str) -> str:
    if any(marker in value for marker in _PRIVATE_KEY_MARKERS):
        return "[REDACTED]"
    redacted = _redact_structured_json_text(value) or value
    redacted = _redact_structured_url_query(redacted) or redacted
    for pattern in _EMBEDDED_SECRET_PATTERNS:
        def replacement(match: re.Match[str]) -> str:
            name = match.group(1)
            if name.lower() in {"authorization", "proxy-authorization"}:
                return f"{name}: [REDACTED]"
            if name.lower().startswith("http"):
                return f"{name}[REDACTED]@"
            separator = "=" if "=" in match.group(0) else ":"
            return f"{name}{separator}[REDACTED]"
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact_diagnostic_value(value: Any) -> Any:
    """Recursively redact credential-shaped keys and embedded secret text."""

    if isinstance(value, str):
        return _redact_embedded_secret_text(value)
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if _is_sensitive_key(key):
                result[key] = "[REDACTED]"
            else:
                result[key] = redact_diagnostic_value(child)
        return result
    if isinstance(value, list):
        return [redact_diagnostic_value(child) for child in value]
    if isinstance(value, tuple):
        return [redact_diagnostic_value(child) for child in value]
    return value


@dataclass(frozen=True)
class DecisionTrace:
    aggregate_version: int
    event_id: str
    evidence_id: str
    decision: str
    decision_reason: str
    risk_outcome: str
    order_id: str | None
    fill_id: str | None
    cash: str
    position: str
    equity: str
    reconciled: bool


@dataclass(frozen=True)
class DiagnosticSnapshot:
    symbol: str
    traces: tuple[DecisionTrace, ...]
    evidence_count: int
    pending_outbox_sample_count: int
    pending_outbox_sample_truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return redact_diagnostic_value(
            {
                "symbol": self.symbol,
                "traces": [asdict(trace) for trace in self.traces],
                "evidence_count": self.evidence_count,
                "pending_outbox_sample_count": self.pending_outbox_sample_count,
                "pending_outbox_sample_truncated": self.pending_outbox_sample_truncated,
            }
        )

    def to_text(self) -> str:
        """Return a screen-reader-friendly line-oriented diagnostic summary."""

        lines = [
            f"Symbol: {self.symbol}",
            f"Evidence records: {self.evidence_count}",
            f"Pending outbox sample: {self.pending_outbox_sample_count}",
        ]
        if self.pending_outbox_sample_truncated:
            lines.append("Pending outbox sample is truncated.")
        for trace in self.traces:
            lines.append(
                " | ".join(
                    [
                        f"Step {trace.aggregate_version}",
                        f"decision {trace.decision}",
                        f"risk {trace.risk_outcome}",
                        f"order {trace.order_id or 'none'}",
                        f"fill {trace.fill_id or 'none'}",
                        f"reconciled {str(trace.reconciled).lower()}",
                    ]
                )
            )
        return "\n".join(lines) + "\n"


_TRACE_FIELDS = (
    "evidence_id",
    "decision",
    "decision_reason",
    "risk_outcome",
    "order_id",
    "fill_id",
    "cash",
    "position",
    "equity",
    "reconciled",
)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Diagnostic source is unreadable: {path.name}") from error


def _read_evidence(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError("Learning evidence is missing")
    rows: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError("Learning evidence is unreadable") from error
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError("Learning evidence is corrupt") from error
        if not isinstance(row, dict):
            raise ValueError("Learning evidence row must be an object")
        evidence_id = row.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            raise ValueError("Learning evidence ID is invalid")
        if evidence_id in identifiers:
            raise ValueError("Duplicate learning evidence ID")
        identifiers.add(evidence_id)
        rows.append(row)
    if not rows:
        raise ValueError("Learning evidence is empty")
    return rows


def build_diagnostic_snapshot(state_dir: str | Path) -> DiagnosticSnapshot:
    """Reconstruct decision traces from durable evidence plus the event journal."""

    root = Path(state_dir)
    checkpoint = _read_json(root / "checkpoint.json")
    if not isinstance(checkpoint, dict):
        raise ValueError("Checkpoint must be an object")
    symbol = checkpoint.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("Checkpoint symbol is invalid")

    evidence = _read_evidence(root / "learning-evidence.jsonl")
    evidence_by_id = {row["evidence_id"]: row for row in evidence}
    checkpoint_ids = checkpoint.get("evidence_ids")
    if not isinstance(checkpoint_ids, list) or set(checkpoint_ids) != set(evidence_by_id):
        raise ValueError("Checkpoint and learning evidence do not describe the same episodes")
    checkpoint_records = checkpoint.get("evidence_records")
    if not isinstance(checkpoint_records, dict) or checkpoint_records != evidence_by_id:
        raise ValueError("Checkpoint evidence records mismatch durable learning evidence")

    journal_path = root / "journal.sqlite3"
    if not journal_path.is_file():
        raise ValueError("Durable journal is missing")
    store = JournalStore(journal_path)
    events = store.load_events("simulation_portfolio", symbol)

    traces: list[DecisionTrace] = []
    observed_evidence_ids: set[str] = set()
    expected_version = 1
    for event in events:
        if event.get("event_type") != "SimulationEpisodeRecorded":
            raise ValueError("Unexpected event type in simulation portfolio trace")
        version = event.get("aggregate_version")
        if version != expected_version:
            raise ValueError("Simulation portfolio trace has an aggregate-version gap")
        expected_version += 1
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("Journal trace payload must be an object")
        evidence_id = payload.get("evidence_id")
        if not isinstance(evidence_id, str) or evidence_id not in evidence_by_id:
            raise ValueError("Journal event has no matching learning evidence")
        if evidence_id in observed_evidence_ids:
            raise ValueError("Learning evidence is linked by more than one journal event")
        observed_evidence_ids.add(evidence_id)
        evidence_row = evidence_by_id[evidence_id]
        for field in _TRACE_FIELDS:
            if payload.get(field) != evidence_row.get(field):
                raise ValueError(f"Journal/evidence trace mismatch for {field}")
        traces.append(
            DecisionTrace(
                aggregate_version=version,
                event_id=event["event_id"],
                evidence_id=evidence_id,
                decision=payload["decision"],
                decision_reason=payload["decision_reason"],
                risk_outcome=payload["risk_outcome"],
                order_id=payload.get("order_id"),
                fill_id=payload.get("fill_id"),
                cash=payload["cash"],
                position=payload["position"],
                equity=payload["equity"],
                reconciled=payload["reconciled"],
            )
        )

    if observed_evidence_ids != set(evidence_by_id):
        raise ValueError("Learning evidence is missing durable journal trace linkage")

    pending_sample = store.pending_outbox(limit=1000)
    return DiagnosticSnapshot(
        symbol=symbol,
        traces=tuple(traces),
        evidence_count=len(evidence),
        pending_outbox_sample_count=len(pending_sample),
        pending_outbox_sample_truncated=len(pending_sample) == 1000,
    )
