"""Append-only, tamper-evident decision traces for the simulated runtime."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping


GENESIS_HASH = "0" * 64
REQUIRED_FIELDS = (
    "trace_id",
    "input_hash",
    "strategy_version",
    "decision",
    "decision_reason",
    "risk_outcome",
    "evidence_refs",
)

_SENSITIVE_KEYS = {
    "authorization",
    "authorization_header",
    "bearer_token",
    "cookie",
    "password",
    "password_hash",
    "secret",
    "client_secret",
    "session",
    "session_id",
    "session_token",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "x_api_key",
    "private_key",
    "private_key_pem",
}


def _normalized_key(value: object) -> str:
    return "_".join(
        part
        for part in "".join(
            character.lower() if character.isalnum() else "_"
            for character in str(value).strip()
        ).split("_")
        if part
    )


_EMBEDDED_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*[^,;\r\n]+"
    ),
    re.compile(
        r"(?i)\b(https?://)[^/@\s]+@"
    ),
    re.compile(
        r"(?i)\b(api[_-]?key|token|access[_-]?token|refresh[_-]?token|session|session[_-]?token|"
        r"secret|credential|client[_-]?secret|private[_-]?key|password)\s*[:=]\s*([^&\s;,]+)"
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


def _redact_embedded_secret_text(value: str) -> str:
    if any(marker in value for marker in _PRIVATE_KEY_MARKERS):
        return "[REDACTED]"
    redacted = value
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


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_embedded_secret_text(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = _normalized_key(key)
            result[str(key)] = "[REDACTED]" if normalized in _SENSITIVE_KEYS else _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _hash_record(record: dict[str, Any]) -> str:
    payload = {key: value for key, value in record.items() if key != "record_hash"}
    return sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _semantic_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key not in {"recorded_at", "previous_hash", "record_hash"}
    }


class DecisionTraceStore:
    """Durable JSONL trace store with idempotent append and hash-chain verification."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("Decision trace row must be an object")
                records.append(record)
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("Corrupt decision trace store") from error
        return records

    @staticmethod
    def _validate_input(trace: dict[str, Any]) -> None:
        if not isinstance(trace, dict):
            raise ValueError("Decision trace must be an object")
        for field in REQUIRED_FIELDS:
            if field not in trace:
                raise ValueError(f"Missing decision trace field: {field}")
        for field in ("trace_id", "input_hash", "strategy_version", "decision", "decision_reason", "risk_outcome"):
            value = trace[field]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a non-empty string")
        input_hash = trace["input_hash"]
        if (
            len(input_hash) != 64
            or input_hash != input_hash.lower()
            or any(ch not in "0123456789abcdef" for ch in input_hash)
        ):
            raise ValueError("input_hash must be a lowercase SHA-256 hex digest")

        refs = trace["evidence_refs"]
        if (
            not isinstance(refs, list)
            or any(
                not isinstance(item, str)
                or not item.strip()
                or item != item.strip()
                for item in refs
            )
        ):
            raise ValueError("evidence_refs must contain canonical non-empty strings")
        if len(refs) != len(set(refs)):
            raise ValueError("evidence_refs must not contain duplicates")

        correlation_id = trace.get("correlation_id")
        if correlation_id is not None and (
            not isinstance(correlation_id, str) or not correlation_id.strip()
        ):
            raise ValueError("correlation_id must be a non-empty string")

        event_ids = trace.get("event_ids")
        if event_ids is not None:
            if (
                not isinstance(event_ids, list)
                or not event_ids
                or any(
                    not isinstance(item, str)
                    or not item.strip()
                    or item != item.strip()
                    for item in event_ids
                )
            ):
                raise ValueError("event_ids must contain canonical non-empty strings")
            if len(event_ids) != len(set(event_ids)):
                raise ValueError("event_ids must not contain duplicates")

        attributes = trace.get("attributes")
        if attributes is not None and not isinstance(attributes, dict):
            raise ValueError("attributes must be an object")

    def append(self, trace: dict[str, Any]) -> bool:
        """Append a trace once; identical retry is a no-op, conflicting retry fails closed."""

        prepared = _redact(trace)
        self._validate_input(prepared)
        records = self._load()
        # Integrity verification must precede idempotency handling. Otherwise an
        # identical retry could silently succeed against a tampered hash chain.
        if records and not self.verify():
            raise ValueError("Existing decision trace chain is corrupt")

        trace_id = prepared["trace_id"]
        for existing in records:
            if existing.get("trace_id") == trace_id:
                if _semantic_payload(existing) != prepared:
                    raise ValueError("trace_id already exists with different decision content")
                return False

        previous_hash = records[-1]["record_hash"] if records else GENESIS_HASH
        record = dict(prepared)
        record["recorded_at"] = datetime.now(timezone.utc).isoformat()
        record["previous_hash"] = previous_hash
        record["record_hash"] = _hash_record(record)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True

    def records(self) -> list[dict[str, Any]]:
        records = self._load()
        if records and not self.verify():
            raise ValueError("Decision trace chain is corrupt")
        return records

    def reconstruct(
        self,
        trace_id: str,
        *,
        available_event_ids: Iterable[str],
        available_evidence_ids: Iterable[str],
    ) -> dict[str, Any]:
        """Reconstruct a durable decision only when all linked evidence is available."""

        if not isinstance(trace_id, str) or not trace_id.strip():
            raise ValueError("trace_id is required")
        events = set(available_event_ids)
        evidence = set(available_evidence_ids)
        record = next(
            (item for item in self.records() if item.get("trace_id") == trace_id),
            None,
        )
        if record is None:
            raise KeyError(trace_id)

        event_ids = record.get("event_ids", [])
        evidence_refs = record["evidence_refs"]
        missing_events = [item for item in event_ids if item not in events]
        missing_evidence = [item for item in evidence_refs if item not in evidence]
        if missing_events or missing_evidence:
            raise ValueError(
                "trace evidence incomplete: "
                f"missing_events={missing_events}, missing_evidence={missing_evidence}"
            )
        return _semantic_payload(record)

    def accessible_export(self, trace_id: str) -> str:
        """Return a linear, screen-reader-friendly view of one verified trace."""

        record = next(
            (item for item in self.records() if item.get("trace_id") == trace_id),
            None,
        )
        if record is None:
            raise KeyError(trace_id)

        lines = [
            f"Decision trace: {record['trace_id']}",
            f"Strategy: {record['strategy_version']}",
            f"Decision: {record['decision']}",
            f"Reason: {record['decision_reason']}",
            f"Risk outcome: {record['risk_outcome']}",
        ]
        correlation_id = record.get("correlation_id")
        if correlation_id:
            lines.append(f"Correlation: {correlation_id}")

        lines.append("Durable events:")
        event_ids = record.get("event_ids", [])
        lines.extend(f"- {item}" for item in event_ids) if event_ids else lines.append("- none")

        lines.append("Evidence:")
        lines.extend(f"- {item}" for item in record["evidence_refs"]) if record["evidence_refs"] else lines.append("- none")

        lines.append("Attributes:")
        attributes = record.get("attributes", {})
        if attributes:
            for key in sorted(attributes):
                lines.append(f"- {key}: {canonical_json(attributes[key])}")
        else:
            lines.append("- none")
        return "\n".join(lines) + "\n"

    def verify(self) -> bool:
        try:
            records = self._load()
        except ValueError:
            return False

        seen: set[str] = set()
        expected_previous = GENESIS_HASH
        for record in records:
            try:
                self._validate_input(record)
                trace_id = record["trace_id"]
                if trace_id in seen:
                    return False
                seen.add(trace_id)
                if record.get("previous_hash") != expected_previous:
                    return False
                recorded_at = record.get("recorded_at")
                if not isinstance(recorded_at, str) or not recorded_at:
                    return False
                record_hash = record.get("record_hash")
                if not isinstance(record_hash, str) or len(record_hash) != 64:
                    return False
                if _hash_record(record) != record_hash:
                    return False
                expected_previous = record_hash
            except (KeyError, TypeError, ValueError):
                return False
        return True



class BoundedMetricBacklog:
    """Bounded diagnostic queue; unlike durable traces, metrics may be dropped."""

    def __init__(self, max_items: int = 256) -> None:
        if not isinstance(max_items, int) or isinstance(max_items, bool) or max_items <= 0:
            raise ValueError("max_items must be a positive integer")
        self._items: deque[dict[str, Any]] = deque(maxlen=max_items)
        self._dropped = 0

    @property
    def dropped(self) -> int:
        return self._dropped

    def record(self, name: str, value: float, **labels: Any) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("metric name is required")
        if len(self._items) == self._items.maxlen:
            self._dropped += 1
        self._items.append(
            {"name": name.strip(), "value": value, "labels": _redact(dict(labels))}
        )

    def snapshot(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._items)
