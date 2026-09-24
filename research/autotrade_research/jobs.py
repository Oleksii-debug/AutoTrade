"""Durable, bounded research-job coordination.

This store is intentionally restricted to research work. It cannot submit or
retry financial orders. Lease generation fences stale workers, and final result
publication is compare-and-swap on the current generation.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import UUID, uuid4


FINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED"}
ALLOWED_STATES = {"QUEUED", "RUNNING", "WAITING_EXTERNAL", *FINAL_STATES}


class JobError(RuntimeError):
    pass


class JobConflictError(JobError):
    pass


class JobLeaseError(JobError):
    pass


class JobBudgetError(JobError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def _require_research_kind(kind: str) -> str:
    normalized = _require_text(kind, "kind")
    if not normalized.lower().startswith("research."):
        raise ValueError("durable job kind must use the research.* namespace")
    return normalized


def _validate_hashes(input_hashes: list[str]) -> list[str]:
    if not isinstance(input_hashes, list) or not input_hashes:
        raise ValueError("input_hashes must be a non-empty list")
    result: list[str] = []
    for digest in input_hashes:
        value = _require_text(digest, "input hash")
        if not value.startswith("sha256:") or len(value) != 71:
            raise ValueError("input hash must be a sha256 digest")
        hex_part = value[7:]
        if any(ch not in "0123456789abcdef" for ch in hex_part):
            raise ValueError("input hash must be lowercase hexadecimal")
        result.append(value)
    return result


def _validate_budget(budget: dict[str, int | float]) -> dict[str, float]:
    if not isinstance(budget, dict) or not budget:
        raise ValueError("resource_budget must be a non-empty object")
    normalized: dict[str, float] = {}
    for name, raw in budget.items():
        key = _require_text(name, "budget key")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("resource budget values must be numeric")
        value = float(raw)
        if value < 0 or value == float("inf") or value != value:
            raise ValueError("resource budget values must be finite and non-negative")
        normalized[key] = value
    return normalized


class ResearchJobStore:
    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations")]
            if any(version > self.SCHEMA_VERSION for version in versions):
                connection.rollback()
                raise JobError("job database schema is newer than this runtime")
            if self.SCHEMA_VERSION not in versions:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        dedupe_key TEXT NOT NULL UNIQUE,
                        input_hashes_json TEXT NOT NULL,
                        state TEXT NOT NULL,
                        generation INTEGER NOT NULL CHECK (generation > 0),
                        attempt INTEGER NOT NULL CHECK (attempt >= 0),
                        owner TEXT,
                        lease_until TEXT,
                        checkpoint_ref TEXT,
                        resource_budget_json TEXT NOT NULL,
                        resource_usage_json TEXT NOT NULL,
                        output_refs_json TEXT NOT NULL,
                        error_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_jobs_claim
                        ON jobs(state, lease_until, created_at, job_id);
                    """
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (self.SCHEMA_VERSION, _iso(datetime.now(timezone.utc))),
                )
            connection.commit()

    @staticmethod
    def _row_record(row: sqlite3.Row) -> dict[str, Any]:
        record = {
            "job_id": row["job_id"],
            "kind": row["kind"],
            "input_hashes": json.loads(row["input_hashes_json"]),
            "dedupe_key": row["dedupe_key"],
            "state": row["state"],
            "generation": str(row["generation"]),
            "attempt": row["attempt"],
            "resource_budget": json.loads(row["resource_budget_json"]),
            "output_refs": json.loads(row["output_refs_json"]),
        }
        if row["owner"] is not None:
            record["owner"] = row["owner"]
        if row["lease_until"] is not None:
            record["lease_until"] = row["lease_until"]
        if row["checkpoint_ref"] is not None:
            record["checkpoint_ref"] = row["checkpoint_ref"]
        if row["error_json"] is not None:
            record["error"] = json.loads(row["error_json"])
        return record

    def enqueue(
        self,
        *,
        kind: str,
        dedupe_key: str,
        input_hashes: list[str],
        resource_budget: dict[str, int | float],
        job_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        job_kind = _require_research_kind(kind)
        key = _require_text(dedupe_key, "dedupe_key")
        hashes = _validate_hashes(input_hashes)
        budget = _validate_budget(resource_budget)
        current = _utc(now or datetime.now(timezone.utc))
        identifier = str(uuid4()) if job_id is None else str(UUID(_require_text(job_id, "job_id")))

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM jobs WHERE dedupe_key = ?", (key,)).fetchone()
            if existing is not None:
                same = (
                    existing["kind"] == job_kind
                    and json.loads(existing["input_hashes_json"]) == hashes
                    and json.loads(existing["resource_budget_json"]) == budget
                )
                if not same:
                    connection.rollback()
                    raise JobConflictError("dedupe_key conflicts with another research job")
                connection.commit()
                return self._row_record(existing), False

            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, kind, dedupe_key, input_hashes_json, state, generation,
                    attempt, owner, lease_until, checkpoint_ref, resource_budget_json,
                    resource_usage_json, output_refs_json, error_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'QUEUED', 1, 0, NULL, NULL, NULL, ?, '{}', '[]', NULL, ?, ?)
                """,
                (identifier, job_kind, key, _json(hashes), _json(budget), _iso(current), _iso(current)),
            )
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
            connection.commit()
        return self._row_record(row), True

    def get(self, job_id: str) -> dict[str, Any]:
        identifier = str(UUID(_require_text(job_id, "job_id")))
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
        if row is None:
            raise KeyError(identifier)
        return self._row_record(row)

    def claim(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = 60,
    ) -> dict[str, Any] | None:
        worker = _require_text(worker_id, "worker_id")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("lease_seconds must be a positive integer")
        current = _utc(now or datetime.now(timezone.utc))
        lease_until = current + timedelta(seconds=lease_seconds)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state = 'QUEUED'
                ORDER BY created_at, job_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE jobs
                SET state='RUNNING', owner=?, lease_until=?, attempt=attempt+1, updated_at=?
                WHERE job_id=? AND state='QUEUED'
                """,
                (worker, _iso(lease_until), _iso(current), row["job_id"]),
            )
            claimed = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
            connection.commit()
        return self._row_record(claimed)

    def requeue_expired(self, *, now: datetime | None = None) -> int:
        current = _utc(now or datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT job_id FROM jobs
                WHERE state='RUNNING' AND lease_until IS NOT NULL AND lease_until <= ?
                """,
                (_iso(current),),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE jobs
                    SET state='QUEUED', owner=NULL, lease_until=NULL,
                        generation=generation+1, updated_at=?
                    WHERE job_id=? AND state='RUNNING'
                    """,
                    (_iso(current), row["job_id"]),
                )
            connection.commit()
        return len(rows)

    def renew(
        self,
        job_id: str,
        *,
        worker_id: str,
        generation: int,
        now: datetime | None = None,
        lease_seconds: int = 60,
    ) -> dict[str, Any]:
        identifier = str(UUID(_require_text(job_id, "job_id")))
        worker = _require_text(worker_id, "worker_id")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise ValueError("generation must be a positive integer")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("lease_seconds must be a positive integer")
        current = _utc(now or datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
            self._require_live_lease(row, worker, generation, current)
            connection.execute(
                "UPDATE jobs SET lease_until=?, updated_at=? WHERE job_id=?",
                (_iso(current + timedelta(seconds=lease_seconds)), _iso(current), identifier),
            )
            updated = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
            connection.commit()
        return self._row_record(updated)

    @staticmethod
    def _require_live_lease(
        row: sqlite3.Row | None,
        worker: str,
        generation: int,
        now: datetime,
    ) -> None:
        if row is None:
            raise KeyError("job")
        if row["state"] != "RUNNING":
            raise JobLeaseError("job is not running")
        if row["owner"] != worker or row["generation"] != generation:
            raise JobLeaseError("worker does not own the current job generation")
        if row["lease_until"] is None or _parse(row["lease_until"]) <= now:
            raise JobLeaseError("job lease has expired")

    def checkpoint(
        self,
        job_id: str,
        *,
        worker_id: str,
        generation: int,
        checkpoint_ref: str,
        resource_usage: dict[str, int | float],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        identifier = str(UUID(_require_text(job_id, "job_id")))
        worker = _require_text(worker_id, "worker_id")
        checkpoint = _require_text(checkpoint_ref, "checkpoint_ref")
        usage = _validate_budget(resource_usage)
        current = _utc(now or datetime.now(timezone.utc))

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
            self._require_live_lease(row, worker, generation, current)
            budget = json.loads(row["resource_budget_json"])
            for key, value in usage.items():
                if key not in budget or value > float(budget[key]):
                    connection.rollback()
                    raise JobBudgetError(f"resource budget exceeded or undeclared: {key}")
            connection.execute(
                """
                UPDATE jobs SET checkpoint_ref=?, resource_usage_json=?, updated_at=?
                WHERE job_id=?
                """,
                (checkpoint, _json(usage), _iso(current), identifier),
            )
            updated = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
            connection.commit()
        return self._row_record(updated)

    def cancel(self, job_id: str, *, now: datetime | None = None) -> bool:
        identifier = str(UUID(_require_text(job_id, "job_id")))
        current = _utc(now or datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(identifier)
            if row["state"] in FINAL_STATES:
                connection.commit()
                return False
            connection.execute(
                """
                UPDATE jobs
                SET state='CANCELLED', owner=NULL, lease_until=NULL, updated_at=?
                WHERE job_id=?
                """,
                (_iso(current), identifier),
            )
            connection.commit()
        return True

    def succeed(
        self,
        job_id: str,
        *,
        worker_id: str,
        generation: int,
        output_refs: list[str],
        now: datetime | None = None,
    ) -> bool:
        identifier = str(UUID(_require_text(job_id, "job_id")))
        worker = _require_text(worker_id, "worker_id")
        if not isinstance(output_refs, list) or not output_refs:
            raise ValueError("output_refs must be a non-empty list")
        outputs = [_require_text(value, "output_ref") for value in output_refs]
        current = _utc(now or datetime.now(timezone.utc))

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(identifier)
            if row["state"] == "SUCCEEDED":
                existing = json.loads(row["output_refs_json"])
                if existing == outputs and row["generation"] == generation:
                    connection.commit()
                    return False
                connection.rollback()
                raise JobConflictError("job already published a different accepted result")
            self._require_live_lease(row, worker, generation, current)
            connection.execute(
                """
                UPDATE jobs
                SET state='SUCCEEDED', output_refs_json=?, owner=NULL, lease_until=NULL, updated_at=?
                WHERE job_id=? AND state='RUNNING' AND generation=?
                """,
                (_json(outputs), _iso(current), identifier, generation),
            )
            connection.commit()
        return True

    def fail(
        self,
        job_id: str,
        *,
        worker_id: str,
        generation: int,
        error: dict[str, Any],
        now: datetime | None = None,
    ) -> None:
        identifier = str(UUID(_require_text(job_id, "job_id")))
        worker = _require_text(worker_id, "worker_id")
        if not isinstance(error, dict) or not error:
            raise ValueError("error must be a non-empty object")
        current = _utc(now or datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (identifier,)).fetchone()
            self._require_live_lease(row, worker, generation, current)
            connection.execute(
                """
                UPDATE jobs
                SET state='FAILED', error_json=?, owner=NULL, lease_until=NULL, updated_at=?
                WHERE job_id=?
                """,
                (_json(error), _iso(current), identifier),
            )
            connection.commit()
