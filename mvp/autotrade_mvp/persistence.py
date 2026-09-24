from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_digest(value: Any) -> str:
    return "sha256:" + sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AppendResult:
    event_id: str
    aggregate_version: int
    inserted: bool


class JournalStore:
    """SQLite journal/outbox primitive for the network-free MVP.

    It deliberately cannot send network requests. The outbox records durable
    publication intent; a separate qualified dispatcher must perform delivery.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
            if any(version > self.SCHEMA_VERSION for version in versions):
                raise ValueError("Journal schema is newer than this runtime")
            if self.SCHEMA_VERSION not in versions:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        event_id TEXT PRIMARY KEY,
                        event_type TEXT NOT NULL,
                        aggregate_type TEXT NOT NULL,
                        aggregate_id TEXT NOT NULL,
                        aggregate_version INTEGER NOT NULL CHECK (aggregate_version > 0),
                        payload_json TEXT NOT NULL,
                        payload_hash TEXT NOT NULL,
                        committed_at TEXT NOT NULL,
                        UNIQUE (aggregate_type, aggregate_id, aggregate_version)
                    );
                    CREATE INDEX IF NOT EXISTS idx_events_aggregate
                        ON events(aggregate_type, aggregate_id, aggregate_version);

                    CREATE TABLE IF NOT EXISTS outbox (
                        outbox_id TEXT PRIMARY KEY,
                        event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id) ON DELETE RESTRICT,
                        topic TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        delivered_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_outbox_pending
                        ON outbox(delivered_at, created_at, outbox_id);

                    CREATE TABLE IF NOT EXISTS command_dedupe (
                        command_id TEXT PRIMARY KEY,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        request_hash TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        state_version INTEGER NOT NULL CHECK (state_version >= 0),
                        created_at TEXT NOT NULL
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (self.SCHEMA_VERSION, self._now()),
                )
            connection.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _require_text(value: Any, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be non-empty text")
        return value

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        self._require_text(event_id, "event_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT event_id, event_type, aggregate_type, aggregate_id,
                       aggregate_version, payload_json, payload_hash, committed_at
                FROM events WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "aggregate_type": row["aggregate_type"],
            "aggregate_id": row["aggregate_id"],
            "aggregate_version": row["aggregate_version"],
            "payload": json.loads(row["payload_json"]),
            "payload_hash": row["payload_hash"],
            "committed_at": row["committed_at"],
        }

    def next_aggregate_version(self, aggregate_type: str, aggregate_id: str) -> int:
        self._require_text(aggregate_type, "aggregate_type")
        self._require_text(aggregate_id, "aggregate_id")
        with self._connect() as connection:
            current = connection.execute(
                "SELECT MAX(aggregate_version) FROM events WHERE aggregate_type = ? AND aggregate_id = ?",
                (aggregate_type, aggregate_id),
            ).fetchone()[0]
        return 1 if current is None else int(current) + 1

    def append_event(self, envelope: dict[str, Any], *, outbox_topic: str | None = None) -> AppendResult:
        event_id = self._require_text(envelope.get("event_id"), "event_id")
        event_type = self._require_text(envelope.get("event_type"), "event_type")
        aggregate_type = self._require_text(envelope.get("aggregate_type"), "aggregate_type")
        aggregate_id = self._require_text(envelope.get("aggregate_id"), "aggregate_id")
        try:
            aggregate_version = int(envelope["aggregate_version"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("aggregate_version must be a positive integer") from error
        if aggregate_version <= 0:
            raise ValueError("aggregate_version must be a positive integer")
        payload = envelope.get("payload")
        expected_hash = payload_digest(payload)
        supplied_hash = envelope.get("payload_hash")
        if supplied_hash != expected_hash:
            raise ValueError("payload_hash does not match payload")
        payload_json = canonical_json(payload)
        committed_at = self._require_text(envelope.get("committed_at"), "committed_at")
        if outbox_topic is not None:
            self._require_text(outbox_topic, "outbox_topic")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
            if existing is not None:
                exact = (
                    existing["event_type"] == event_type
                    and existing["aggregate_type"] == aggregate_type
                    and existing["aggregate_id"] == aggregate_id
                    and existing["aggregate_version"] == aggregate_version
                    and existing["payload_json"] == payload_json
                    and existing["payload_hash"] == supplied_hash
                )
                if not exact:
                    connection.rollback()
                    raise ValueError("event_id conflicts with an existing event")
                connection.commit()
                return AppendResult(event_id, aggregate_version, False)

            current = connection.execute(
                "SELECT MAX(aggregate_version) FROM events WHERE aggregate_type = ? AND aggregate_id = ?",
                (aggregate_type, aggregate_id),
            ).fetchone()[0]
            expected_version = 1 if current is None else int(current) + 1
            if aggregate_version != expected_version:
                connection.rollback()
                raise ValueError(
                    f"aggregate_version must be {expected_version} for {aggregate_type}/{aggregate_id}"
                )

            connection.execute(
                """
                INSERT INTO events(
                    event_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, payload_json, payload_hash, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_type,
                    aggregate_type,
                    aggregate_id,
                    aggregate_version,
                    payload_json,
                    supplied_hash,
                    committed_at,
                ),
            )
            if outbox_topic is not None:
                outbox_payload = canonical_json(envelope)
                outbox_id = "outbox-" + sha256(event_id.encode("utf-8")).hexdigest()[:32]
                connection.execute(
                    """
                    INSERT INTO outbox(outbox_id, event_id, topic, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (outbox_id, event_id, outbox_topic, outbox_payload, self._now()),
                )
            connection.commit()
        return AppendResult(event_id, aggregate_version, True)

    def load_events(self, aggregate_type: str, aggregate_id: str) -> list[dict[str, Any]]:
        self._require_text(aggregate_type, "aggregate_type")
        self._require_text(aggregate_id, "aggregate_id")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, event_type, aggregate_type, aggregate_id,
                       aggregate_version, payload_json, payload_hash, committed_at
                FROM events
                WHERE aggregate_type = ? AND aggregate_id = ?
                ORDER BY aggregate_version
                """,
                (aggregate_type, aggregate_id),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "aggregate_type": row["aggregate_type"],
                "aggregate_id": row["aggregate_id"],
                "aggregate_version": row["aggregate_version"],
                "payload": json.loads(row["payload_json"]),
                "payload_hash": row["payload_hash"],
                "committed_at": row["committed_at"],
            }
            for row in rows
        ]

    def pending_outbox(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT outbox_id, event_id, topic, payload_json, created_at
                FROM outbox
                WHERE delivered_at IS NULL
                ORDER BY created_at, outbox_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "outbox_id": row["outbox_id"],
                "event_id": row["event_id"],
                "topic": row["topic"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def mark_outbox_delivered(self, outbox_id: str) -> bool:
        self._require_text(outbox_id, "outbox_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT delivered_at FROM outbox WHERE outbox_id = ?", (outbox_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(outbox_id)
            if row["delivered_at"] is not None:
                connection.commit()
                return False
            connection.execute(
                "UPDATE outbox SET delivered_at = ? WHERE outbox_id = ? AND delivered_at IS NULL",
                (self._now(), outbox_id),
            )
            connection.commit()
        return True

    def record_command(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        request: Any,
        result: Any,
        state_version: int,
    ) -> tuple[Any, bool]:
        self._require_text(command_id, "command_id")
        self._require_text(idempotency_key, "idempotency_key")
        if not isinstance(state_version, int) or state_version < 0:
            raise ValueError("state_version must be a non-negative integer")
        request_hash = payload_digest(request)
        result_json = canonical_json(result)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM command_dedupe WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    connection.rollback()
                    raise ValueError("idempotency_key was already used for a different request")
                connection.commit()
                return json.loads(existing["result_json"]), False
            if connection.execute(
                "SELECT 1 FROM command_dedupe WHERE command_id = ?", (command_id,)
            ).fetchone() is not None:
                connection.rollback()
                raise ValueError("command_id already exists with another idempotency key")
            connection.execute(
                """
                INSERT INTO command_dedupe(
                    command_id, idempotency_key, request_hash, result_json, state_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (command_id, idempotency_key, request_hash, result_json, state_version, self._now()),
            )
            connection.commit()
        return result, True
