"""Append-only, tamper-evident decision traces for the simulated runtime."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import unquote_plus, urlsplit, urlunsplit


GENESIS_HASH = "0" * 64
_BUILD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+@-]{0,127}$")
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
    "credential",
    "credential_id",
    "proxy_authorization",
    "session",
    "session_id",
    "session_token",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "api_secret",
    "x_api_key",
    "x_txc_apikey",
    "x_txc_payload",
    "x_txc_signature",
    "private_key",
    "private_key_pem",
}


def _normalized_key(value: object) -> str:
    if type(value) is not str:
        raise ValueError("diagnostic keys must be exact strings")
    # Preserve acronym boundaries as well as ordinary camelCase so aliases such
    # as XApiKey, accessToken and proxyAuthorization canonicalize to the same
    # security vocabulary as x_api_key, access_token and proxy_authorization.
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value.strip())
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", separated)
    return "_".join(
        part
        for part in "".join(
            character.lower() if character.isalnum() else "_"
            for character in separated
        ).split("_")
        if part
    )


# These are non-secret diagnostic counters whose names intentionally mention a
# security concept.  The key alone is never sufficient to bypass redaction:
# only an exact non-negative integer value may use this narrow exemption.
_BENIGN_DIAGNOSTIC_KEYS = {
    "api_secret_rotation_count",
    "token_budget",
}

_COMPOUND_SENSITIVE_KEY_PARTS = {
    "authorization",
    "password",
    "secret",
    "credential",
    "cookie",
    "session",
    "token",
    "apikey",
}


def _is_sensitive_key(value: object) -> bool:
    normalized = _normalized_key(value)
    if normalized in _SENSITIVE_KEYS:
        return True

    parts = tuple(part for part in normalized.split("_") if part)
    part_set = set(parts)
    if any(part in _COMPOUND_SENSITIVE_KEY_PARTS for part in parts):
        return True
    if {"api", "key"} <= part_set or {"private", "key"} <= part_set:
        return True
    return "txc" in part_set and bool(
        part_set & {"apikey", "payload", "signature"}
    )


_EMBEDDED_SECRET_PATTERNS = (
    re.compile(
        r"""(?i)(?:["'])?\b(authorization(?:[_-]?header)?|proxy[_-]?authorization)\b"""
        r"""(?:["'])?\s*[:=]\s*"""
        r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\r\n]+)"""
    ),
    re.compile(
        r"(?i)\b(https?://)[^/@\s]+@"
    ),
    re.compile(
        r"""(?i)(?:["'])?\b(api[_-]?key|x[_-]?api[_-]?key|x[_-]?txc[_-]?apikey|x[_-]?txc[_-]?payload|x[_-]?txc[_-]?signature|"""
        r"""token|bearer[_-]?token|access[_-]?token|refresh[_-]?token|id[_-]?token|session|session[_-]?token|session[_-]?id|"""
        r"""secret|credential|cookie|api[_-]?secret|client[_-]?secret|private[_-]?key(?:[_-]?pem)?|password(?:[_-]?hash)?)\b"""
        r"""(?:["'])?\s*[:=]\s*"""
        r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\[REDACTED\][^&;,\r\n]*|[^&;,\r\n]+)"""
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
    redacted = _redact(decoded)
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
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not parsed.query
    ):
        return None

    changed = False
    query_parts: list[str] = []
    for part in parsed.query.split("&"):
        raw_key, separator, _ = part.partition("=")
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
    structured_json = _redact_structured_json_text(value)
    if structured_json is not None:
        # Structured redaction has already recursively sanitized every JSON value.
        # Do not run the generic text regexes over the serialized JSON: those
        # regexes intentionally normalize key/value syntax and would make the
        # valid redacted JSON unparsable.
        return structured_json
    redacted = _redact_structured_url_query(value) or value
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
    if type(value) is str:
        return _redact_embedded_secret_text(value)
    if type(value) is dict:
        result: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("diagnostic object keys must be exact strings")
            normalized = _normalized_key(key)
            benign_counter = (
                normalized in _BENIGN_DIAGNOSTIC_KEYS
                and type(item) is int
                and item >= 0
            )
            if _is_sensitive_key(key) and not benign_counter:
                result[key] = "[REDACTED]"
            else:
                result[key] = _redact(item)
        return result
    if type(value) is list:
        return [_redact(item) for item in value]
    if type(value) is tuple:
        return tuple(_redact(item) for item in value)
    if isinstance(value, (str, Mapping, list, tuple)):
        raise ValueError(
            "diagnostic structured values must use exact built-in containers and strings"
        )
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _validate_digest_map(
    name: str,
    value: object,
    expected_ids: Iterable[str],
) -> None:
    if type(value) is not dict:
        raise ValueError(f"{name} must be an exact object")
    expected_items = tuple(expected_ids)
    if any(type(identity) is not str for identity in expected_items):
        raise ValueError(f"{name} linked identities must be exact strings")
    for identity, digest in value.items():
        if type(identity) is not str:
            raise ValueError(f"{name} keys must be exact strings")
        if (
            type(digest) is not str
            or len(digest) != 64
            or digest != digest.lower()
            or any(ch not in "0123456789abcdef" for ch in digest)
        ):
            raise ValueError(f"{name} must contain lowercase SHA-256 digests")
    # Set construction is intentionally last: after the exact-string fence no
    # caller-controlled __hash__/__eq__ implementation can execute here.
    if set(value) != set(expected_items):
        raise ValueError(f"{name} keys must exactly match linked identities")


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
        if type(trace) is not dict:
            raise ValueError("Decision trace must be an exact object")
        for field in REQUIRED_FIELDS:
            if field not in trace:
                raise ValueError(f"Missing decision trace field: {field}")
        for field in ("trace_id", "input_hash", "strategy_version", "decision", "decision_reason", "risk_outcome"):
            value = trace[field]
            if type(value) is not str or not value.strip():
                raise ValueError(f"{field} must be a non-empty string")
        input_hash = trace["input_hash"]
        if (
            len(input_hash) != 64
            or input_hash != input_hash.lower()
            or any(ch not in "0123456789abcdef" for ch in input_hash)
        ):
            raise ValueError("input_hash must be a lowercase SHA-256 hex digest")

        source_sha = trace.get("source_sha")
        if source_sha is not None and (
            type(source_sha) is not str
            or len(source_sha) not in {40, 64}
            or source_sha != source_sha.lower()
            or any(ch not in "0123456789abcdef" for ch in source_sha)
        ):
            raise ValueError("source_sha must be a canonical lowercase Git/object SHA")

        build_id = trace.get("build_id")
        if build_id is not None and (
            type(build_id) is not str
            or _BUILD_ID_PATTERN.fullmatch(build_id) is None
        ):
            raise ValueError("build_id must be a canonical bounded build token")

        refs = trace["evidence_refs"]
        if (
            type(refs) is not list
            or any(
                type(item) is not str
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
            type(correlation_id) is not str or not correlation_id.strip()
        ):
            raise ValueError("correlation_id must be a non-empty string")

        event_ids = trace.get("event_ids")
        if event_ids is not None:
            if (
                type(event_ids) is not list
                or not event_ids
                or any(
                    type(item) is not str
                    or not item.strip()
                    or item != item.strip()
                    for item in event_ids
                )
            ):
                raise ValueError("event_ids must contain canonical non-empty strings")
            if len(event_ids) != len(set(event_ids)):
                raise ValueError("event_ids must not contain duplicates")

        evidence_digests = trace.get("evidence_digests")
        if evidence_digests is not None:
            _validate_digest_map("evidence_digests", evidence_digests, refs)
        event_digests = trace.get("event_digests")
        if event_digests is not None:
            _validate_digest_map("event_digests", event_digests, event_ids or [])

        attributes = trace.get("attributes")
        if attributes is not None and type(attributes) is not dict:
            raise ValueError("attributes must be an exact object")

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

        if type(trace_id) is not str or not trace_id.strip():
            raise ValueError("trace_id must be an exact non-empty string")
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

    def reconstruct_exact(
        self,
        trace_id: str,
        *,
        expected_source_sha: str,
        expected_build_id: str,
        available_event_digests: Mapping[str, str],
        available_evidence_digests: Mapping[str, str],
    ) -> dict[str, Any]:
        """Reconstruct only when links, content digests and source/build identity match."""

        if type(available_event_digests) is not dict:
            raise ValueError("available_event_digests must be an exact object")
        if type(available_evidence_digests) is not dict:
            raise ValueError("available_evidence_digests must be an exact object")
        _validate_digest_map(
            "available_event_digests",
            available_event_digests,
            available_event_digests.keys(),
        )
        _validate_digest_map(
            "available_evidence_digests",
            available_evidence_digests,
            available_evidence_digests.keys(),
        )

        candidate = {
            "trace_id": "identity-check",
            "input_hash": "0" * 64,
            "strategy_version": "identity-check",
            "decision": "identity-check",
            "decision_reason": "identity-check",
            "risk_outcome": "identity-check",
            "evidence_refs": [],
            "source_sha": expected_source_sha,
            "build_id": expected_build_id,
        }
        self._validate_input(candidate)
        record = self.reconstruct(
            trace_id,
            available_event_ids=available_event_digests.keys(),
            available_evidence_ids=available_evidence_digests.keys(),
        )
        if record.get("source_sha") != expected_source_sha:
            raise ValueError("trace source identity mismatch")
        if record.get("build_id") != expected_build_id:
            raise ValueError("trace build identity mismatch")

        stored_event_digests = record.get("event_digests")
        stored_evidence_digests = record.get("evidence_digests")
        if type(stored_event_digests) is not dict or type(stored_evidence_digests) is not dict:
            raise ValueError("trace evidence digest identity missing")
        for identity in record.get("event_ids", []):
            if available_event_digests.get(identity) != stored_event_digests.get(identity):
                raise ValueError("trace event digest mismatch")
        for identity in record["evidence_refs"]:
            if available_evidence_digests.get(identity) != stored_evidence_digests.get(identity):
                raise ValueError("trace evidence digest mismatch")
        return record

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
        source_sha = record.get("source_sha")
        build_id = record.get("build_id")
        lines.append(f"Source SHA: {source_sha or 'unavailable'}")
        lines.append(f"Build: {build_id or 'unavailable'}")

        lines.append("Durable events:")
        event_ids = record.get("event_ids", [])
        event_digests = record.get("event_digests", {})
        if event_ids:
            for item in event_ids:
                digest = event_digests.get(item)
                lines.append(f"- {item}" + (f" sha256 {digest}" if digest else ""))
        else:
            lines.append("- none")

        lines.append("Evidence:")
        evidence_digests = record.get("evidence_digests", {})
        if record["evidence_refs"]:
            for item in record["evidence_refs"]:
                digest = evidence_digests.get(item)
                lines.append(f"- {item}" + (f" sha256 {digest}" if digest else ""))
        else:
            lines.append("- none")

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

    def __init__(self, max_items: int = 256, max_label_bytes: int = 4096) -> None:
        if type(max_items) is not int or max_items <= 0:
            raise ValueError("max_items must be an exact positive integer")
        if type(max_label_bytes) is not int or max_label_bytes <= 0:
            raise ValueError("max_label_bytes must be an exact positive integer")
        self._items: deque[dict[str, Any]] = deque(maxlen=max_items)
        self._max_label_bytes = max_label_bytes
        self._dropped = 0

    @property
    def dropped(self) -> int:
        return self._dropped

    def record(self, name: str, value: float, **labels: Any) -> None:
        if type(name) is not str or not name.strip():
            raise ValueError("metric name must be an exact non-empty string")
        if type(value) not in (int, float) or not isfinite(value):
            raise ValueError("metric value must be an exact finite number")
        redacted_labels = _redact(dict(labels))
        try:
            encoded_labels = canonical_json(redacted_labels).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("metric labels must be finite JSON values") from error
        if len(encoded_labels) > self._max_label_bytes:
            raise ValueError("metric labels exceed bounded size")
        if len(self._items) == self._items.maxlen:
            self._dropped += 1
        self._items.append(
            {"name": name.strip(), "value": value, "labels": redacted_labels}
        )

    def snapshot(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._items)
