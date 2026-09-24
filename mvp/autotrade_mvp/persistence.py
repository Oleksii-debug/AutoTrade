from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


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

    SCHEMA_VERSION = 2

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

    @classmethod
    def _migration_statements(cls, version: int) -> tuple[str, ...]:
        if version == 1:
            return (
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
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_events_aggregate
                    ON events(aggregate_type, aggregate_id, aggregate_version)
                """,
                """
                CREATE TABLE IF NOT EXISTS outbox (
                    outbox_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id) ON DELETE RESTRICT,
                    topic TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_outbox_pending
                    ON outbox(delivered_at, created_at, outbox_id)
                """,
                """
                CREATE TABLE IF NOT EXISTS command_dedupe (
                    command_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    state_version INTEGER NOT NULL CHECK (state_version >= 0),
                    created_at TEXT NOT NULL
                )
                """,
            )
        if version == 2:
            return (
                """
                CREATE TABLE IF NOT EXISTS projection_checkpoints (
                    projection_name TEXT NOT NULL,
                    aggregate_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    aggregate_version INTEGER NOT NULL CHECK (aggregate_version >= 0),
                    state_json TEXT NOT NULL,
                    state_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (projection_name, aggregate_type, aggregate_id)
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_projection_checkpoint_version
                    ON projection_checkpoints(
                        aggregate_type, aggregate_id, aggregate_version
                    )
                """,
            )
        raise ValueError(f"Unsupported journal migration version: {version}")

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations "
                    "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                versions = [
                    int(row[0])
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                if any(version > self.SCHEMA_VERSION for version in versions):
                    raise ValueError("Journal schema is newer than this runtime")
                if versions:
                    expected = list(range(1, versions[-1] + 1))
                    if versions != expected:
                        raise ValueError("Journal schema migration history is not contiguous")

                current = versions[-1] if versions else 0
                for version in range(current + 1, self.SCHEMA_VERSION + 1):
                    for statement in self._migration_statements(version):
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (version, self._now()),
                    )

                required_tables = {
                    "schema_migrations",
                    "events",
                    "outbox",
                    "command_dedupe",
                    "projection_checkpoints",
                }
                present_tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                missing_tables = required_tables - present_tables
                if missing_tables:
                    raise ValueError(
                        "Journal schema is incomplete: " + ", ".join(sorted(missing_tables))
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def current_schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()
        return 0 if row is None or row[0] is None else int(row[0])

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

    def save_projection_checkpoint(
        self,
        *,
        projection_name: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        state: Any,
    ) -> bool:
        """Persist only derived projection state; the event journal remains authoritative."""

        self._require_text(projection_name, "projection_name")
        self._require_text(aggregate_type, "aggregate_type")
        self._require_text(aggregate_id, "aggregate_id")
        if (
            not isinstance(aggregate_version, int)
            or isinstance(aggregate_version, bool)
            or aggregate_version < 0
        ):
            raise ValueError("aggregate_version must be a non-negative integer")
        state_json = canonical_json(state)
        state_hash = payload_digest(state)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT MAX(aggregate_version) FROM events "
                    "WHERE aggregate_type = ? AND aggregate_id = ?",
                    (aggregate_type, aggregate_id),
                ).fetchone()[0]
                journal_version = 0 if current is None else int(current)
                if aggregate_version > journal_version:
                    raise ValueError("projection checkpoint cannot outrun the journal")

                existing = connection.execute(
                    """
                    SELECT aggregate_version, state_json, state_hash
                    FROM projection_checkpoints
                    WHERE projection_name = ?
                      AND aggregate_type = ?
                      AND aggregate_id = ?
                    """,
                    (projection_name, aggregate_type, aggregate_id),
                ).fetchone()
                if existing is not None:
                    existing_version = int(existing["aggregate_version"])
                    exact = (
                        existing_version == aggregate_version
                        and existing["state_json"] == state_json
                        and existing["state_hash"] == state_hash
                    )
                    if exact:
                        connection.commit()
                        return False
                    if aggregate_version <= existing_version:
                        raise ValueError(
                            "projection checkpoint cannot regress or change at the same version"
                        )

                connection.execute(
                    """
                    INSERT INTO projection_checkpoints(
                        projection_name, aggregate_type, aggregate_id,
                        aggregate_version, state_json, state_hash, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(projection_name, aggregate_type, aggregate_id)
                    DO UPDATE SET
                        aggregate_version = excluded.aggregate_version,
                        state_json = excluded.state_json,
                        state_hash = excluded.state_hash,
                        updated_at = excluded.updated_at
                    """,
                    (
                        projection_name,
                        aggregate_type,
                        aggregate_id,
                        aggregate_version,
                        state_json,
                        state_hash,
                        self._now(),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return True

    def load_projection_checkpoint(
        self,
        *,
        projection_name: str,
        aggregate_type: str,
        aggregate_id: str,
    ) -> dict[str, Any] | None:
        self._require_text(projection_name, "projection_name")
        self._require_text(aggregate_type, "aggregate_type")
        self._require_text(aggregate_id, "aggregate_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT aggregate_version, state_json, state_hash, updated_at
                FROM projection_checkpoints
                WHERE projection_name = ?
                  AND aggregate_type = ?
                  AND aggregate_id = ?
                """,
                (projection_name, aggregate_type, aggregate_id),
            ).fetchone()
            if row is None:
                return None
            current = connection.execute(
                "SELECT MAX(aggregate_version) FROM events "
                "WHERE aggregate_type = ? AND aggregate_id = ?",
                (aggregate_type, aggregate_id),
            ).fetchone()[0]

        state = json.loads(row["state_json"])
        if payload_digest(state) != row["state_hash"]:
            raise ValueError("projection checkpoint hash does not match state")
        journal_version = 0 if current is None else int(current)
        if int(row["aggregate_version"]) > journal_version:
            raise ValueError("projection checkpoint is ahead of the journal")
        return {
            "projection_name": projection_name,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "aggregate_version": int(row["aggregate_version"]),
            "state": state,
            "state_hash": row["state_hash"],
            "updated_at": row["updated_at"],
        }

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

    def commit_command(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        request: Any,
        result: Any,
        state_version: int,
        events: list[tuple[dict[str, Any], str | None]],
    ) -> tuple[Any, bool, tuple[AppendResult, ...]]:
        """Atomically commit command dedupe, ordered events and outbox rows."""

        self._require_text(command_id, "command_id")
        self._require_text(idempotency_key, "idempotency_key")
        if not isinstance(state_version, int) or isinstance(state_version, bool) or state_version < 0:
            raise ValueError("state_version must be a non-negative integer")
        if not events:
            raise ValueError("At least one event is required")

        request_hash = payload_digest(request)
        result_json = canonical_json(result)
        prepared: list[dict[str, Any]] = []
        seen_event_ids: set[str] = set()

        for envelope, outbox_topic in events:
            if not isinstance(envelope, dict):
                raise ValueError("Each event envelope must be an object")
            event_id = self._require_text(envelope.get("event_id"), "event_id")
            if event_id in seen_event_ids:
                raise ValueError("event_id is duplicated within the transaction")
            seen_event_ids.add(event_id)
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
            payload_json = canonical_json(payload)
            supplied_hash = envelope.get("payload_hash")
            if supplied_hash != payload_digest(payload):
                raise ValueError("payload_hash does not match payload")
            committed_at = self._require_text(envelope.get("committed_at"), "committed_at")
            if outbox_topic is not None:
                self._require_text(outbox_topic, "outbox_topic")
            prepared.append(
                {
                    "event_id": event_id,
                    "event_type": event_type,
                    "aggregate_type": aggregate_type,
                    "aggregate_id": aggregate_id,
                    "aggregate_version": aggregate_version,
                    "payload_json": payload_json,
                    "payload_hash": supplied_hash,
                    "committed_at": committed_at,
                    "outbox_topic": outbox_topic,
                    "outbox_payload": canonical_json(envelope),
                }
            )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM command_dedupe WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if existing["request_hash"] != request_hash:
                        raise ValueError(
                            "idempotency_key was already used for a different request"
                        )
                    connection.commit()
                    return json.loads(existing["result_json"]), False, ()

                if connection.execute(
                    "SELECT 1 FROM command_dedupe WHERE command_id = ?",
                    (command_id,),
                ).fetchone() is not None:
                    raise ValueError("command_id already exists with another idempotency key")

                next_versions: dict[tuple[str, str], int] = {}
                for item in prepared:
                    if connection.execute(
                        "SELECT 1 FROM events WHERE event_id = ?",
                        (item["event_id"],),
                    ).fetchone() is not None:
                        raise ValueError("event_id already exists for another command")
                    key = (item["aggregate_type"], item["aggregate_id"])
                    if key not in next_versions:
                        current = connection.execute(
                            "SELECT MAX(aggregate_version) FROM events "
                            "WHERE aggregate_type = ? AND aggregate_id = ?",
                            key,
                        ).fetchone()[0]
                        next_versions[key] = 1 if current is None else int(current) + 1
                    expected_version = next_versions[key]
                    if item["aggregate_version"] != expected_version:
                        raise ValueError(
                            f"aggregate_version must be {expected_version} "
                            f"for {item['aggregate_type']}/{item['aggregate_id']}"
                        )
                    next_versions[key] += 1

                connection.execute(
                    """
                    INSERT INTO command_dedupe(
                        command_id, idempotency_key, request_hash, result_json,
                        state_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        command_id,
                        idempotency_key,
                        request_hash,
                        result_json,
                        state_version,
                        self._now(),
                    ),
                )

                appended: list[AppendResult] = []
                for item in prepared:
                    connection.execute(
                        """
                        INSERT INTO events(
                            event_id, event_type, aggregate_type, aggregate_id,
                            aggregate_version, payload_json, payload_hash, committed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item["event_id"],
                            item["event_type"],
                            item["aggregate_type"],
                            item["aggregate_id"],
                            item["aggregate_version"],
                            item["payload_json"],
                            item["payload_hash"],
                            item["committed_at"],
                        ),
                    )
                    if item["outbox_topic"] is not None:
                        outbox_id = "outbox-" + sha256(
                            item["event_id"].encode("utf-8")
                        ).hexdigest()[:32]
                        connection.execute(
                            """
                            INSERT INTO outbox(
                                outbox_id, event_id, topic, payload_json, created_at
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                outbox_id,
                                item["event_id"],
                                item["outbox_topic"],
                                item["outbox_payload"],
                                self._now(),
                            ),
                        )
                    appended.append(
                        AppendResult(item["event_id"], item["aggregate_version"], True)
                    )

                connection.commit()
                return result, True, tuple(appended)
            except Exception:
                connection.rollback()
                raise
