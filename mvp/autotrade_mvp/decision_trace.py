"""Append-only, tamper-evident decision traces for the simulated runtime."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any


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


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


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
        refs = trace["evidence_refs"]
        if not isinstance(refs, list) or any(not isinstance(item, str) or not item for item in refs):
            raise ValueError("evidence_refs must be a list of non-empty strings")
        if len(refs) != len(set(refs)):
            raise ValueError("evidence_refs must not contain duplicates")

    def append(self, trace: dict[str, Any]) -> bool:
        """Append a trace once; identical retry is a no-op, conflicting retry fails closed."""

        self._validate_input(trace)
        records = self._load()
        trace_id = trace["trace_id"]
        for existing in records:
            if existing.get("trace_id") == trace_id:
                if _semantic_payload(existing) != trace:
                    raise ValueError("trace_id already exists with different decision content")
                return False

        if records and not self.verify():
            raise ValueError("Existing decision trace chain is corrupt")

        previous_hash = records[-1]["record_hash"] if records else GENESIS_HASH
        record = dict(trace)
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
