from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any


TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}


@dataclass(frozen=True)
class JobLease:
    job_id: str
    version: int
    owner: str
    lease_token: int
    lease_expires_at: str
    payload: Any
    attempt: int


class DurableJobStore:
    """SQLite durable-job authority for background research and learning work.

    The store never grants trading authority. A job version can produce at most
    one accepted result. Lease tokens fence stale workers after expiry/restart.
    """

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
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _iso(value: datetime) -> str:
        if value.tzinfo is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _parse(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("stored datetime must be timezone-aware")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)

    @staticmethod
    def _require_text(value: Any, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be non-empty text")
        return value

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS job_schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            versions = [row[0] for row in connection.execute(
                "SELECT version FROM job_schema_migrations ORDER BY version"
            )]
            if any(version > self.SCHEMA_VERSION for version in versions):
                raise ValueError("Durable job schema is newer than this runtime")
            if self.SCHEMA_VERSION not in versions:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT NOT NULL,
                        version INTEGER NOT NULL CHECK (version > 0),
                        payload_json TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')
                        ),
                        priority INTEGER NOT NULL,
                        max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
                        attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
                        lease_owner TEXT,
                        lease_token INTEGER NOT NULL DEFAULT 0 CHECK (lease_token >= 0),
                        lease_expires_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_error TEXT,
                        PRIMARY KEY(job_id, version),
                        CHECK (
                            (status = 'RUNNING' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
                            OR
                            (status <> 'RUNNING' AND lease_owner IS NULL AND lease_expires_at IS NULL)
                        )
                    );

                    CREATE INDEX IF NOT EXISTS idx_jobs_claim
                        ON jobs(status, priority DESC, created_at, job_id, version);

                    CREATE TABLE IF NOT EXISTS job_results (
                        job_id TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        result_json TEXT NOT NULL,
                        accepted_at TEXT NOT NULL,
                        lease_token INTEGER NOT NULL,
                        PRIMARY KEY(job_id, version),
                        FOREIGN KEY(job_id, version)
                            REFERENCES jobs(job_id, version) ON DELETE RESTRICT
                    );

                    CREATE TRIGGER IF NOT EXISTS job_results_immutable_update
                    BEFORE UPDATE ON job_results
                    BEGIN
                        SELECT RAISE(ABORT, 'job result is immutable');
                    END;

                    CREATE TRIGGER IF NOT EXISTS job_results_immutable_delete
                    BEFORE DELETE ON job_results
                    BEGIN
                        SELECT RAISE(ABORT, 'job result is immutable');
                    END;
                    """
                )
                connection.execute(
                    "INSERT INTO job_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (self.SCHEMA_VERSION, self._iso(self._now())),
                )
            connection.commit()

    def enqueue(
        self,
        *,
        job_id: str,
        version: int,
        payload: Any,
        priority: int = 0,
        max_attempts: int = 3,
    ) -> bool:
        self._require_text(job_id, "job_id")
        if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
            raise ValueError("version must be a positive integer")
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise ValueError("priority must be an integer")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts <= 0:
            raise ValueError("max_attempts must be a positive integer")
        payload_json = self._json(payload)
        now = self._iso(self._now())

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_json, priority, max_attempts FROM jobs WHERE job_id = ? AND version = ?",
                (job_id, version),
            ).fetchone()
            if existing is not None:
                exact = (
                    existing["payload_json"] == payload_json
                    and existing["priority"] == priority
                    and existing["max_attempts"] == max_attempts
                )
                connection.commit()
                if not exact:
                    raise ValueError("job identity conflicts with an existing version")
                return False
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, version, payload_json, status, priority, max_attempts,
                    attempt, lease_owner, lease_token, lease_expires_at,
                    created_at, updated_at, last_error
                ) VALUES (?, ?, ?, 'PENDING', ?, ?, 0, NULL, 0, NULL, ?, ?, NULL)
                """,
                (job_id, version, payload_json, priority, max_attempts, now, now),
            )
            connection.commit()
        return True

    def _recover_expired_locked(self, connection: sqlite3.Connection, now: datetime) -> None:
        rows = connection.execute(
            """
            SELECT job_id, version, attempt, max_attempts, lease_expires_at
            FROM jobs WHERE status = 'RUNNING'
            """
        ).fetchall()
        for row in rows:
            if self._parse(row["lease_expires_at"]) > now:
                continue
            terminal = row["attempt"] >= row["max_attempts"]
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?, last_error = ?
                WHERE job_id = ? AND version = ? AND status = 'RUNNING'
                """,
                (
                    "FAILED" if terminal else "PENDING",
                    self._iso(now),
                    "lease expired after maximum attempts" if terminal else "lease expired; recovered for retry",
                    row["job_id"],
                    row["version"],
                ),
            )

    def recover_expired(self, *, now: datetime | None = None) -> int:
        now = self._now() if now is None else now.astimezone(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = connection.total_changes
            self._recover_expired_locked(connection, now)
            changed = connection.total_changes - before
            connection.commit()
        return changed

    def claim(
        self,
        *,
        owner: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> JobLease | None:
        self._require_text(owner, "owner")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        now = self._now() if now is None else now.astimezone(timezone.utc)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recover_expired_locked(connection, now)
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'PENDING' AND attempt < max_attempts
                ORDER BY priority DESC, created_at, job_id, version
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            token = int(row["lease_token"]) + 1
            attempt = int(row["attempt"]) + 1
            expires = now + timedelta(seconds=lease_seconds)
            connection.execute(
                """
                UPDATE jobs
                SET status = 'RUNNING', attempt = ?, lease_owner = ?,
                    lease_token = ?, lease_expires_at = ?, updated_at = ?, last_error = NULL
                WHERE job_id = ? AND version = ? AND status = 'PENDING'
                """,
                (
                    attempt,
                    owner,
                    token,
                    self._iso(expires),
                    self._iso(now),
                    row["job_id"],
                    row["version"],
                ),
            )
            connection.commit()
            return JobLease(
                job_id=row["job_id"],
                version=row["version"],
                owner=owner,
                lease_token=token,
                lease_expires_at=self._iso(expires),
                payload=json.loads(row["payload_json"]),
                attempt=attempt,
            )

    def heartbeat(
        self,
        lease: JobLease,
        *,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> JobLease:
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        now = self._now() if now is None else now.astimezone(timezone.utc)
        expires = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, lease_owner, lease_token, lease_expires_at, payload_json, attempt "
                "FROM jobs WHERE job_id = ? AND version = ?",
                (lease.job_id, lease.version),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError((lease.job_id, lease.version))
            valid = (
                row["status"] == "RUNNING"
                and row["lease_owner"] == lease.owner
                and row["lease_token"] == lease.lease_token
                and self._parse(row["lease_expires_at"]) > now
            )
            if not valid:
                connection.rollback()
                raise ValueError("stale or expired job lease")
            connection.execute(
                "UPDATE jobs SET lease_expires_at = ?, updated_at = ? "
                "WHERE job_id = ? AND version = ?",
                (self._iso(expires), self._iso(now), lease.job_id, lease.version),
            )
            connection.commit()
            return JobLease(
                job_id=lease.job_id,
                version=lease.version,
                owner=lease.owner,
                lease_token=lease.lease_token,
                lease_expires_at=self._iso(expires),
                payload=json.loads(row["payload_json"]),
                attempt=row["attempt"],
            )

    def complete(
        self,
        lease: JobLease,
        *,
        result: Any,
        now: datetime | None = None,
    ) -> bool:
        result_json = self._json(result)
        now = self._now() if now is None else now.astimezone(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, lease_owner, lease_token, lease_expires_at "
                "FROM jobs WHERE job_id = ? AND version = ?",
                (lease.job_id, lease.version),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError((lease.job_id, lease.version))
            existing = connection.execute(
                "SELECT result_json FROM job_results WHERE job_id = ? AND version = ?",
                (lease.job_id, lease.version),
            ).fetchone()
            if existing is not None:
                connection.commit()
                if existing["result_json"] != result_json:
                    raise ValueError("job version already has a different accepted result")
                return False
            valid = (
                row["status"] == "RUNNING"
                and row["lease_owner"] == lease.owner
                and row["lease_token"] == lease.lease_token
                and self._parse(row["lease_expires_at"]) > now
            )
            if not valid:
                connection.rollback()
                raise ValueError("stale or expired job lease")
            connection.execute(
                "INSERT INTO job_results(job_id, version, result_json, accepted_at, lease_token) "
                "VALUES (?, ?, ?, ?, ?)",
                (lease.job_id, lease.version, result_json, self._iso(now), lease.lease_token),
            )
            connection.execute(
                """
                UPDATE jobs
                SET status = 'SUCCEEDED', lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?, last_error = NULL
                WHERE job_id = ? AND version = ?
                """,
                (self._iso(now), lease.job_id, lease.version),
            )
            connection.commit()
        return True

    def fail(
        self,
        lease: JobLease,
        *,
        error: str,
        retryable: bool = True,
        now: datetime | None = None,
    ) -> str:
        self._require_text(error, "error")
        now = self._now() if now is None else now.astimezone(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, lease_owner, lease_token, lease_expires_at, attempt, max_attempts "
                "FROM jobs WHERE job_id = ? AND version = ?",
                (lease.job_id, lease.version),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError((lease.job_id, lease.version))
            valid = (
                row["status"] == "RUNNING"
                and row["lease_owner"] == lease.owner
                and row["lease_token"] == lease.lease_token
                and self._parse(row["lease_expires_at"]) > now
            )
            if not valid:
                connection.rollback()
                raise ValueError("stale or expired job lease")
            next_status = "PENDING" if retryable and row["attempt"] < row["max_attempts"] else "FAILED"
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?, last_error = ?
                WHERE job_id = ? AND version = ?
                """,
                (next_status, self._iso(now), error, lease.job_id, lease.version),
            )
            connection.commit()
        return next_status

    def cancel(self, *, job_id: str, version: int) -> bool:
        self._require_text(job_id, "job_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM jobs WHERE job_id = ? AND version = ?",
                (job_id, version),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError((job_id, version))
            if row["status"] in TERMINAL_STATUSES:
                connection.commit()
                return False
            connection.execute(
                """
                UPDATE jobs
                SET status = 'CANCELLED', lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE job_id = ? AND version = ?
                """,
                (self._iso(self._now()), job_id, version),
            )
            connection.commit()
        return True

    def get(self, *, job_id: str, version: int) -> dict[str, Any]:
        self._require_text(job_id, "job_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ? AND version = ?",
                (job_id, version),
            ).fetchone()
            if row is None:
                raise KeyError((job_id, version))
            result = connection.execute(
                "SELECT result_json, accepted_at, lease_token FROM job_results "
                "WHERE job_id = ? AND version = ?",
                (job_id, version),
            ).fetchone()
        return {
            "job_id": row["job_id"],
            "version": row["version"],
            "payload": json.loads(row["payload_json"]),
            "status": row["status"],
            "priority": row["priority"],
            "max_attempts": row["max_attempts"],
            "attempt": row["attempt"],
            "lease_owner": row["lease_owner"],
            "lease_token": row["lease_token"],
            "lease_expires_at": row["lease_expires_at"],
            "last_error": row["last_error"],
            "result": None if result is None else json.loads(result["result_json"]),
            "result_accepted_at": None if result is None else result["accepted_at"],
            "result_lease_token": None if result is None else result["lease_token"],
        }
