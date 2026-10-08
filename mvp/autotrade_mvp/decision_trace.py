"""Append-only, tamper-evident decision traces for the simulated runtime."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import unquote_plus, urlsplit, urlunsplit
from unicodedata import category as unicode_category

from autotrade_runtime.artifacts.durable_publish import (
    DurablePublishLockError,
    atomic_write_bytes,
    durable_path_lock,
    validate_publication_destination,
)
from autotrade_runtime.strict_json import strict_json_loads


GENESIS_HASH = "0" * 64
_CONCRETE_PATH_TYPE = type(Path())
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
    re.compile(r"(?i)\b(https?://)[^/@\s]+@"),
    re.compile(
        r"""(?i)(?:["'])?\b(api[_-]?key|x[_-]?api[_-]?key|x[_-]?txc[_-]?apikey|x[_-]?txc[_-]?payload|x[_-]?txc[_-]?signature|"""
        r"""token|bearer[_-]?token|access[_-]?token|refresh[_-]?token|id[_-]?token|session|session[_-]?token|session[_-]?id|"""
        r"""secret|credential|cookie|api[_-]?secret|client[_-]?secret|private[_-]?key(?:[_-]?pem)?|password(?:[_-]?hash)?)\b"""
        r"""(?:["'])?\s*[:=]\s*"""
        r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\[REDACTED\][^&;,\r\n]*|[^&;,\r\n]+)"""
    ),
)

_PRIVATE_KEY_MARKERS = (
    "-----BEGIN " + "PRIVATE KEY-----",
    "-----BEGIN " + "ENCRYPTED PRIVATE KEY-----",
    "-----BEGIN " + "RSA PRIVATE KEY-----",
    "-----BEGIN " + "DSA PRIVATE KEY-----",
    "-----BEGIN " + "EC PRIVATE KEY-----",
    "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
)

_URL_QUERY_SENSITIVE_KEYS = _SENSITIVE_KEYS | {
    "credential",
    "proxy_authorization",
}


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
        normalized = _normalized_key(unquote_plus(raw_key))
        if normalized in _URL_QUERY_SENSITIVE_KEYS:
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
    """Redact exact built-in diagnostic structures without caller callbacks."""

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
            result[key] = (
                "[REDACTED]"
                if _is_sensitive_key(key) and not benign_counter
                else _redact(item)
            )
        return result
    if type(value) is list:
        return [_redact(item) for item in value]
    if type(value) is tuple:
        # JSON has arrays, not tuples. Normalize before hashing/persistence so
        # an identical retry compares equal to the durable round-trip.
        return [_redact(item) for item in value]
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, (str, Mapping, list, tuple, int, float)):
        raise ValueError(
            "diagnostic values must use exact built-in containers and JSON scalars"
        )
    raise ValueError(
        "diagnostic values must use exact built-in containers and JSON scalars"
    )


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _accessible_inline_text(value: str) -> str:
    """Keep diagnostic export values on one unambiguous visual/speech line."""

    if type(value) is not str:
        raise ValueError("accessible diagnostic text must be an exact string")
    rendered: list[str] = []
    for character in value:
        if unicode_category(character) in {"Cc", "Cf", "Zl", "Zp"}:
            codepoint = ord(character)
            rendered.append(
                f"\\u{codepoint:04x}"
                if codepoint <= 0xFFFF
                else f"\\U{codepoint:08x}"
            )
        else:
            rendered.append(character)
    return "".join(rendered)


def _exact_identity_set(value: object, *, name: str) -> set[str]:
    if type(value) not in (list, tuple):
        raise ValueError(f"{name} must be an exact list or tuple")
    if any(type(identity) is not str for identity in value):
        raise ValueError(f"{name} must contain exact strings")
    # Hash only after exact-string admission.
    return set(value)


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
    # Build sets only after exact-string admission so caller-defined hash/equality
    # cannot execute inside the durable provenance boundary.
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


def _trace_entry_identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _trace_generation_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _read_trace_text_descriptor_bound(path: Path) -> str | None:
    """Read one stable regular-file generation and reject path swaps."""

    try:
        initial = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError("Corrupt decision trace store: cannot inspect path") from error

    if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
        raise ValueError("Corrupt decision trace store: unsafe path alias")

    try:
        validate_publication_destination(path)
    except (DurablePublishLockError, OSError) as error:
        raise ValueError("Corrupt decision trace store: unsafe path alias") from error

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(
            "Corrupt decision trace store: path changed before descriptor read"
        ) from error

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ValueError("Corrupt decision trace store: unsafe path alias")
        if (
            _trace_entry_identity(initial) != _trace_entry_identity(opened)
            or _trace_generation_identity(initial)
            != _trace_generation_identity(opened)
        ):
            raise ValueError(
                "Corrupt decision trace store: path changed before descriptor read"
            )

        expected_bytes = opened.st_size
        remaining = expected_bytes + 1
        copied = 0
        chunks: list[bytes] = []
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            copied += len(chunk)
            remaining -= len(chunk)

        after = os.fstat(descriptor)
        try:
            current = os.stat(path, follow_symlinks=False)
        except OSError as error:
            raise ValueError(
                "Corrupt decision trace store: path changed during descriptor read"
            ) from error

        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or _trace_entry_identity(opened) != _trace_entry_identity(after)
            or _trace_generation_identity(opened)
            != _trace_generation_identity(after)
            or _trace_entry_identity(opened) != _trace_entry_identity(current)
            or _trace_generation_identity(opened)
            != _trace_generation_identity(current)
            or copied != expected_bytes
        ):
            raise ValueError(
                "Corrupt decision trace store: path changed during descriptor read"
            )
    finally:
        os.close(descriptor)

    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeError as error:
        raise ValueError("Corrupt decision trace store") from error


class DecisionTraceStore:
    """Durable JSONL trace store with idempotent append and hash-chain verification."""

    def __init__(self, path: str | Path):
        # Admit only exact built-in path carriers before any path protocol call.
        # An arbitrary PathLike.__fspath__ callback must not execute inside the
        # durable trace authority boundary.
        if type(path) is str:
            path_text = path
        elif type(path) is _CONCRETE_PATH_TYPE:
            path_text = os.fspath(path)
        else:
            raise TypeError("path must be an exact str or pathlib Path")
        # Freeze the selected durable location at composition time. A later
        # process-wide CWD change must not retarget either the trace file or
        # its canonical writer lock.
        self.path = Path(os.path.abspath(path_text))

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.parent.exists():
            return []
        raw = _read_trace_text_descriptor_bound(self.path)
        if raw is None:
            return []
        if raw == "":
            return []
        if not raw.endswith("\n"):
            raise ValueError("Corrupt decision trace store: missing canonical newline")

        lines = raw[:-1].split("\n")
        if any(line == "" for line in lines):
            raise ValueError("Corrupt decision trace store: blank row")

        records: list[dict[str, Any]] = []
        for line in lines:
            try:
                record = strict_json_loads(line)
            except ValueError as error:
                raise ValueError("Corrupt decision trace store") from error
            if type(record) is not dict:
                raise ValueError("Decision trace row must be an exact object")
            try:
                canonical = canonical_json(record)
            except (TypeError, ValueError) as error:
                raise ValueError("Corrupt decision trace store") from error
            if canonical != line:
                raise ValueError(
                    "Corrupt decision trace store: non-canonical durable row"
                )
            records.append(record)
        return records

    @staticmethod
    def _validate_input(trace: dict[str, Any]) -> None:
        """Validate the durable row shape, including legacy evidence-unbound rows."""

        if type(trace) is not dict:
            raise ValueError("Decision trace must be an exact object")
        for field in REQUIRED_FIELDS:
            if field not in trace:
                raise ValueError(f"Missing decision trace field: {field}")
        for field in (
            "trace_id",
            "input_hash",
            "strategy_version",
            "decision",
            "decision_reason",
            "risk_outcome",
        ):
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
        if type(refs) is not list or any(
            type(item) is not str
            or not item.strip()
            or item != item.strip()
            for item in refs
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

    @staticmethod
    def _require_linked_evidence(trace: Mapping[str, Any]) -> None:
        refs = trace.get("evidence_refs")
        if type(refs) is not list or not refs:
            raise ValueError(
                "evidence_refs must contain at least one canonical non-empty string"
            )

    def append(self, trace: dict[str, Any]) -> bool:
        """Append one trace under the canonical cross-process writer lock."""

        try:
            prepared = _redact(trace)
        except RecursionError as error:
            raise ValueError(
                "Decision trace exceeds strict JSON resource domain"
            ) from error
        try:
            strict_json_loads(canonical_json(prepared))
        except (ValueError, RecursionError) as error:
            raise ValueError(
                "Decision trace exceeds strict JSON resource domain"
            ) from error
        store_owned = {"recorded_at", "previous_hash", "record_hash"} & set(prepared)
        if store_owned:
            raise ValueError(
                "decision trace contains store-owned fields: "
                + ", ".join(sorted(store_owned))
            )
        self._validate_input(prepared)
        self._require_linked_evidence(prepared)
        with durable_path_lock(self.path):
            try:
                records = self._load()
            except ValueError as error:
                raise ValueError("Existing decision trace chain is corrupt") from error
            # Validate the exact loaded snapshot before idempotency handling. A
            # second path read could otherwise verify a newer file while stale or
            # corrupt rows from the first read are still used for the append.
            if records and not self._records_are_valid(records):
                raise ValueError("Existing decision trace chain is corrupt")

            trace_id = prepared["trace_id"]
            prepared_semantics = canonical_json(prepared)
            for existing in records:
                if existing.get("trace_id") == trace_id:
                    if canonical_json(_semantic_payload(existing)) != prepared_semantics:
                        raise ValueError(
                            "trace_id already exists with different decision content"
                        )
                    return False

            previous_hash = records[-1]["record_hash"] if records else GENESIS_HASH
            record = dict(prepared)
            record["recorded_at"] = datetime.now(timezone.utc).isoformat()
            record["previous_hash"] = previous_hash
            record["record_hash"] = _hash_record(record)

            payload = "".join(
                canonical_json(item) + "\n" for item in [*records, record]
            ).encode("utf-8")
            atomic_write_bytes(self.path, payload)
            return True

    def records(self) -> list[dict[str, Any]]:
        with durable_path_lock(self.path):
            try:
                records = self._load()
            except ValueError as error:
                raise ValueError("Decision trace chain is corrupt") from error
            if records and not self._records_are_valid(records):
                raise ValueError("Decision trace chain is corrupt")
            return records

    def reconstruct(
        self,
        trace_id: str,
        *,
        available_event_ids: Iterable[str],
        available_evidence_ids: Iterable[str],
    ) -> dict[str, Any]:
        """Validate a durable trace against caller-declared link availability."""

        if type(trace_id) is not str or not trace_id.strip():
            raise ValueError("trace_id must be an exact non-empty string")
        events = _exact_identity_set(
            available_event_ids,
            name="available_event_ids",
        )
        evidence = _exact_identity_set(
            available_evidence_ids,
            name="available_evidence_ids",
        )
        record = next(
            (item for item in self.records() if item.get("trace_id") == trace_id),
            None,
        )
        if record is None:
            raise KeyError(trace_id)

        evidence_refs = record["evidence_refs"]
        if not evidence_refs:
            raise ValueError("trace evidence incomplete: no linked evidence")
        event_ids = record.get("event_ids", [])
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
        """Reconstruct only exact source/build and linked content identity."""

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
            available_event_ids=tuple(available_event_digests),
            available_evidence_ids=tuple(available_evidence_digests),
        )
        if record.get("source_sha") != expected_source_sha:
            raise ValueError("trace source identity mismatch")
        if record.get("build_id") != expected_build_id:
            raise ValueError("trace build identity mismatch")

        stored_event_digests = record.get("event_digests")
        stored_evidence_digests = record.get("evidence_digests")
        if (
            type(stored_event_digests) is not dict
            or type(stored_evidence_digests) is not dict
        ):
            raise ValueError("trace evidence digest identity missing")
        for identity in record.get("event_ids", []):
            if available_event_digests.get(identity) != stored_event_digests.get(identity):
                raise ValueError("trace event digest mismatch")
        for identity in record["evidence_refs"]:
            if (
                available_evidence_digests.get(identity)
                != stored_evidence_digests.get(identity)
            ):
                raise ValueError("trace evidence digest mismatch")
        return record

    def accessible_export(
        self,
        trace_id: str,
        *,
        available_event_ids: Iterable[str] | None = None,
        available_evidence_ids: Iterable[str] | None = None,
    ) -> str:
        """Return a linear view that never presents caller claims as verified evidence."""

        if type(trace_id) is not str or not trace_id.strip():
            raise ValueError("trace_id must be an exact non-empty string")
        if (available_event_ids is None) != (available_evidence_ids is None):
            raise ValueError(
                "available_event_ids and available_evidence_ids must be supplied together"
            )

        record = next(
            (item for item in self.records() if item.get("trace_id") == trace_id),
            None,
        )
        if record is None:
            raise KeyError(trace_id)

        evidence_refs = record["evidence_refs"]
        if available_event_ids is None:
            evidence_status = "UNVERIFIED"
            if evidence_refs:
                evidence_note = (
                    "Explanation is diagnostic only; linked evidence availability was not checked."
                )
            else:
                evidence_note = (
                    "Explanation is diagnostic only; this legacy trace has no linked evidence."
                )
        else:
            self.reconstruct(
                trace_id,
                available_event_ids=available_event_ids,
                available_evidence_ids=available_evidence_ids,
            )
            evidence_status = "UNVERIFIED"
            evidence_note = (
                "Caller-declared linked evidence is complete, but no product-selected "
                "durable evidence authority verified those identifiers."
            )

        lines = [
            f"Decision trace: {_accessible_inline_text(record['trace_id'])}",
            f"Evidence status: {evidence_status}",
            evidence_note,
            f"Strategy: {_accessible_inline_text(record['strategy_version'])}",
            f"Decision: {_accessible_inline_text(record['decision'])}",
            f"Reason: {_accessible_inline_text(record['decision_reason'])}",
            f"Risk outcome: {_accessible_inline_text(record['risk_outcome'])}",
        ]
        correlation_id = record.get("correlation_id")
        if correlation_id:
            lines.append(f"Correlation: {_accessible_inline_text(correlation_id)}")
        lines.append(
            "Source SHA: "
            + _accessible_inline_text(record.get("source_sha") or "unavailable")
        )
        lines.append(
            "Build: "
            + _accessible_inline_text(record.get("build_id") or "unavailable")
        )

        lines.append("Durable events:")
        event_ids = record.get("event_ids", [])
        event_digests = record.get("event_digests", {})
        if event_ids:
            for item in event_ids:
                digest = event_digests.get(item)
                lines.append(
                    f"- {_accessible_inline_text(item)}"
                    + (f" sha256 {digest}" if digest else "")
                )
        else:
            lines.append("- none")

        lines.append("Evidence:")
        evidence_digests = record.get("evidence_digests", {})
        if evidence_refs:
            for item in evidence_refs:
                digest = evidence_digests.get(item)
                lines.append(
                    f"- {_accessible_inline_text(item)}"
                    + (f" sha256 {digest}" if digest else "")
                )
        else:
            lines.append("- none")

        lines.append("Attributes:")
        attributes = record.get("attributes", {})
        if attributes:
            for key in sorted(attributes):
                lines.append(
                    f"- {_accessible_inline_text(key)}: "
                    + _accessible_inline_text(canonical_json(attributes[key]))
                )
        else:
            lines.append("- none")
        return "\n".join(lines) + "\n"

    def _records_are_valid(self, records: list[dict[str, Any]]) -> bool:
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
                if type(recorded_at) is not str or not recorded_at:
                    return False
                record_hash = record.get("record_hash")
                if (
                    type(record_hash) is not str
                    or len(record_hash) != 64
                    or record_hash != record_hash.lower()
                    or any(ch not in "0123456789abcdef" for ch in record_hash)
                ):
                    return False
                if _hash_record(record) != record_hash:
                    return False
                expected_previous = record_hash
            except (KeyError, TypeError, ValueError):
                return False
        return True

    def verify(self) -> bool:
        try:
            records = self._load()
        except ValueError:
            return False
        return self._records_are_valid(records)


class BoundedMetricBacklog:
    """Bounded diagnostic queue; unlike durable traces, metrics may be dropped."""

    def __init__(self, max_items: int = 256) -> None:
        if type(max_items) is not int or max_items <= 0:
            raise ValueError("max_items must be a positive integer")
        self._items: deque[dict[str, Any]] = deque(maxlen=max_items)
        self._dropped = 0

    @property
    def dropped(self) -> int:
        return self._dropped

    def record(self, name: str, value: float, **labels: Any) -> None:
        if type(name) is not str or not name.strip():
            raise ValueError("metric name is required")
        if type(value) not in (int, float):
            raise ValueError("metric value must be a finite number")
        if type(value) is float and not isfinite(value):
            raise ValueError("metric value must be a finite number")
        try:
            redacted_labels = _redact(dict(labels))
        except RecursionError as error:
            raise ValueError(
                "metric item is not JSON compliant or exceeds strict JSON resource domain"
            ) from error
        item = {"name": name.strip(), "value": value, "labels": redacted_labels}
        try:
            encoded_item = canonical_json(item)
            strict_json_loads(encoded_item)
        except (ValueError, RecursionError) as error:
            raise ValueError(
                "metric item is not JSON compliant or exceeds strict JSON resource domain"
            ) from error
        if len(self._items) == self._items.maxlen:
            self._dropped += 1
        self._items.append(item)

    def snapshot(self) -> tuple[dict[str, Any], ...]:
        # Never expose mutable references owned by the backlog. Strict JSON
        # round-trip yields detached built-in values under the same admission
        # domain used by record().
        return tuple(
            strict_json_loads(canonical_json(item)) for item in self._items
        )
