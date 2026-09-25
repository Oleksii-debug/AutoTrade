from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def payload_digest(value: Any) -> str:
    return "sha256:" + sha256(canonical_json(value).encode("utf-8")).hexdigest()


_SEQUENCE_RE = re.compile(r"^(0|[1-9][0-9]*)$")


def _sequence(value: object, *, name: str, positive: bool = False) -> int:
    """Validate canonical Sequence text before integer persistence/arithmetic."""

    if not isinstance(value, str) or _SEQUENCE_RE.fullmatch(value) is None:
        qualifier = "positive " if positive else ""
        raise ValueError(
            f"{name} must be a {qualifier}canonical integer sequence string"
        )
    number = int(value)
    if positive and number == 0:
        raise ValueError(
            f"{name} must be a positive canonical integer sequence string"
        )
    return number


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

    SCHEMA_VERSION = 3

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
        if version == 3:
            return (
                """
                CREATE TABLE command_dedupe_v3 (
                    command_id TEXT PRIMARY KEY,
                    actor TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    state_version INTEGER NOT NULL CHECK (state_version >= 0),
                    created_at TEXT NOT NULL,
                    UNIQUE (actor, environment, idempotency_key)
                )
                """,
                """
                INSERT INTO command_dedupe_v3(
                    command_id, actor, environment, idempotency_key,
                    request_hash, result_json, state_version, created_at
                )
                SELECT
                    command_id, '__LEGACY_UNSCOPED__', '__LEGACY_UNSCOPED__',
                    idempotency_key, request_hash, result_json, state_version, created_at
                FROM command_dedupe
                """,
                "DROP TABLE command_dedupe",
                "ALTER TABLE command_dedupe_v3 RENAME TO command_dedupe",
            )
        raise ValueError(f"Unsupported journal migration version: {version}")

    @classmethod
    def _required_table_columns(cls) -> dict[str, frozenset[str]]:
        command_columns = {
            "command_id", "idempotency_key", "request_hash", "result_json",
            "state_version", "created_at",
        }
        if cls.SCHEMA_VERSION >= 3:
            command_columns.update({"actor", "environment"})
        required = {
            "schema_migrations": frozenset({"version", "applied_at"}),
            "events": frozenset({
                "event_id", "event_type", "aggregate_type", "aggregate_id",
                "aggregate_version", "payload_json", "payload_hash", "committed_at",
            }),
            "outbox": frozenset({
                "outbox_id", "event_id", "topic", "payload_json",
                "created_at", "delivered_at",
            }),
            "command_dedupe": frozenset(command_columns),
        }
        if cls.SCHEMA_VERSION >= 2:
            required["projection_checkpoints"] = frozenset({
                "projection_name", "aggregate_type", "aggregate_id",
                "aggregate_version", "state_json", "state_hash", "updated_at",
            })
        return required

    @staticmethod
    def _unique_index_columns(connection, table_name: str) -> set[tuple[str, ...]]:
        unique_indexes: set[tuple[str, ...]] = set()
        for index_row in connection.execute(f"PRAGMA index_list({table_name})"):
            if not bool(index_row["unique"]):
                continue
            index_name = str(index_row["name"]).replace("'", "''")
            columns = tuple(
                str(column_row["name"])
                for column_row in connection.execute(
                    f"PRAGMA index_info('{index_name}')"
                )
            )
            unique_indexes.add(columns)
        return unique_indexes

    @classmethod
    def _validate_key_contracts(cls, connection) -> None:
        expected_primary_keys = {
            "events": ("event_id",),
            "outbox": ("outbox_id",),
            "command_dedupe": ("command_id",),
        }
        if cls.SCHEMA_VERSION >= 2:
            expected_primary_keys["projection_checkpoints"] = (
                "projection_name",
                "aggregate_type",
                "aggregate_id",
            )
        expected_unique = {
            "events": {
                ("aggregate_type", "aggregate_id", "aggregate_version"),
            },
            "outbox": {("event_id",)},
            "command_dedupe": (
                {("actor", "environment", "idempotency_key")}
                if cls.SCHEMA_VERSION >= 3
                else {("idempotency_key",)}
            ),
        }
        for table_name, expected_pk in expected_primary_keys.items():
            pk_columns = tuple(
                str(row["name"])
                for row in sorted(
                    (
                        row
                        for row in connection.execute(
                            f"PRAGMA table_info({table_name})"
                        )
                        if int(row["pk"]) > 0
                    ),
                    key=lambda row: int(row["pk"]),
                )
            )
            if pk_columns != expected_pk:
                raise ValueError(
                    f"Journal schema table {table_name} has invalid primary key"
                )

        for table_name, required_indexes in expected_unique.items():
            actual = cls._unique_index_columns(connection, table_name)
            missing = required_indexes - actual
            if missing:
                rendered = "; ".join(",".join(columns) for columns in sorted(missing))
                raise ValueError(
                    f"Journal schema table {table_name} is missing unique constraint: "
                    + rendered
                )

        foreign_keys = [
            row
            for row in connection.execute("PRAGMA foreign_key_list(outbox)")
            if (
                str(row["table"]) == "events"
                and str(row["from"]) == "event_id"
                and str(row["to"]) == "event_id"
                and str(row["on_delete"]).upper() == "RESTRICT"
            )
        ]
        if not foreign_keys:
            raise ValueError(
                "Journal schema table outbox is missing event ownership foreign key"
            )

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
                }
                if self.SCHEMA_VERSION >= 2:
                    required_tables.add("projection_checkpoints")
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

                # Table names alone are not sufficient evidence of a valid
                # migration.  A crash or legacy partial migration can leave a
                # pre-existing table that makes CREATE TABLE IF NOT EXISTS a
                # no-op.  Verify the structural column contract before
                # recording this runtime as schema-compatible.
                for table_name, required_columns in self._required_table_columns().items():
                    actual_columns = {
                        str(row["name"])
                        for row in connection.execute(
                            f"PRAGMA table_info({table_name})"
                        )
                    }
                    missing_columns = required_columns - actual_columns
                    if missing_columns:
                        raise ValueError(
                            "Journal schema table "
                            + table_name
                            + " is missing required columns: "
                            + ", ".join(sorted(missing_columns))
                        )
                self._validate_key_contracts(connection)
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
        return value.strip()

    @staticmethod
    def _decode_event_row(row: sqlite3.Row) -> dict[str, Any]:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError) as error:
            raise ValueError("journal event payload is not valid JSON") from error
        if payload_digest(payload) != row["payload_hash"]:
            raise ValueError("journal event payload hash does not match stored payload")
        return {
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "aggregate_type": row["aggregate_type"],
            "aggregate_id": row["aggregate_id"],
            "aggregate_version": row["aggregate_version"],
            "payload": payload,
            "payload_hash": row["payload_hash"],
            "committed_at": row["committed_at"],
        }

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        event_id = self._require_text(event_id, "event_id")
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
        return self._decode_event_row(row)

    def next_aggregate_version(self, aggregate_type: str, aggregate_id: str) -> int:
        aggregate_type = self._require_text(aggregate_type, "aggregate_type")
        aggregate_id = self._require_text(aggregate_id, "aggregate_id")
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
            raw_aggregate_version = envelope["aggregate_version"]
        except KeyError as error:
            raise ValueError(
                "aggregate_version must be a positive canonical integer sequence string"
            ) from error
        aggregate_version = _sequence(
            raw_aggregate_version,
            name="aggregate_version",
            positive=True,
        )
        payload = envelope.get("payload")
        expected_hash = payload_digest(payload)
        supplied_hash = envelope.get("payload_hash")
        if supplied_hash != expected_hash:
            raise ValueError("payload_hash does not match payload")
        payload_json = canonical_json(payload)
        committed_at = self._require_text(envelope.get("committed_at"), "committed_at")
        if outbox_topic is not None:
            outbox_topic = self._require_text(outbox_topic, "outbox_topic")

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
                    and existing["committed_at"] == committed_at
                )
                existing_outbox = connection.execute(
                    "SELECT topic, payload_json FROM outbox WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                expected_outbox_payload = (
                    canonical_json(envelope) if outbox_topic is not None else None
                )
                outbox_exact = (
                    (
                        outbox_topic is None
                        and existing_outbox is None
                    )
                    or (
                        outbox_topic is not None
                        and existing_outbox is not None
                        and existing_outbox["topic"] == outbox_topic
                        and existing_outbox["payload_json"] == expected_outbox_payload
                    )
                )
                if not exact or not outbox_exact:
                    connection.rollback()
                    raise ValueError(
                        "event_id conflicts with an existing event or publication intent"
                    )
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
        aggregate_type = self._require_text(aggregate_type, "aggregate_type")
        aggregate_id = self._require_text(aggregate_id, "aggregate_id")
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
        return [self._decode_event_row(row) for row in rows]

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

        projection_name = self._require_text(
            projection_name, "projection_name"
        )
        aggregate_type = self._require_text(
            aggregate_type, "aggregate_type"
        )
        aggregate_id = self._require_text(aggregate_id, "aggregate_id")
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
        projection_name = self._require_text(
            projection_name, "projection_name"
        )
        aggregate_type = self._require_text(
            aggregate_type, "aggregate_type"
        )
        aggregate_id = self._require_text(aggregate_id, "aggregate_id")
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
                SELECT
                    outbox.outbox_id,
                    outbox.event_id,
                    outbox.topic,
                    outbox.payload_json AS outbox_payload_json,
                    outbox.created_at,
                    events.event_type,
                    events.aggregate_type,
                    events.aggregate_id,
                    events.aggregate_version,
                    events.payload_json AS event_payload_json,
                    events.payload_hash,
                    events.committed_at
                FROM outbox
                JOIN events ON events.event_id = outbox.event_id
                WHERE outbox.delivered_at IS NULL
                ORDER BY outbox.created_at, outbox.outbox_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        pending: list[dict[str, Any]] = []
        for row in rows:
            event_row = {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "aggregate_type": row["aggregate_type"],
                "aggregate_id": row["aggregate_id"],
                "aggregate_version": row["aggregate_version"],
                "payload_json": row["event_payload_json"],
                "payload_hash": row["payload_hash"],
                "committed_at": row["committed_at"],
            }
            event = self._decode_event_row(event_row)
            try:
                outbox_payload = json.loads(row["outbox_payload_json"])
            except (json.JSONDecodeError, TypeError) as error:
                raise ValueError("outbox payload is not valid JSON") from error
            expected_envelope = {
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "aggregate_type": event["aggregate_type"],
                "aggregate_id": event["aggregate_id"],
                "aggregate_version": str(event["aggregate_version"]),
                "payload": event["payload"],
                "payload_hash": event["payload_hash"],
                "committed_at": event["committed_at"],
            }
            for key, expected in expected_envelope.items():
                if outbox_payload.get(key) != expected:
                    raise ValueError(
                        "outbox payload does not match authoritative journal event"
                    )
            pending.append(
                {
                    "outbox_id": row["outbox_id"],
                    "event_id": row["event_id"],
                    "topic": row["topic"],
                    "payload": outbox_payload,
                    "created_at": row["created_at"],
                }
            )
        return pending

    def mark_outbox_delivered(self, outbox_id: str) -> bool:
        outbox_id = self._require_text(outbox_id, "outbox_id")
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

    @staticmethod
    def _command_environment(value: object) -> str:
        normalized = value.strip().upper() if isinstance(value, str) else ""
        if normalized not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
            raise ValueError(
                "environment must be REPLAY, SIMULATION, PAPER, or LIVE"
            )
        return normalized

    def _command_scope(
        self,
        *,
        actor: str,
        environment: str,
        idempotency_key: str,
    ) -> tuple[str, str, str]:
        return (
            self._require_text(actor, "actor"),
            self._command_environment(environment),
            self._require_text(idempotency_key, "idempotency_key"),
        )

    @staticmethod
    def _reject_legacy_unscoped_key(connection, idempotency_key: str) -> None:
        legacy = connection.execute(
            "SELECT 1 FROM command_dedupe "
            "WHERE actor = '__LEGACY_UNSCOPED__' "
            "AND environment = '__LEGACY_UNSCOPED__' "
            "AND idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if legacy is not None:
            raise ValueError(
                "idempotency_key exists in legacy unscoped command history; "
                "use a new key"
            )

    def record_command(
        self,
        *,
        command_id: str,
        actor: str,
        environment: str,
        idempotency_key: str,
        request: Any,
        result: Any,
        state_version: int,
    ) -> tuple[Any, bool]:
        command_id = self._require_text(command_id, "command_id")
        actor, environment, idempotency_key = self._command_scope(
            actor=actor,
            environment=environment,
            idempotency_key=idempotency_key,
        )
        if (
            not isinstance(state_version, int)
            or isinstance(state_version, bool)
            or state_version < 0
        ):
            raise ValueError("state_version must be a non-negative integer")
        request_hash = payload_digest(request)
        result_json = canonical_json(result)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._reject_legacy_unscoped_key(connection, idempotency_key)
            existing = connection.execute(
                "SELECT * FROM command_dedupe "
                "WHERE actor = ? AND environment = ? AND idempotency_key = ?",
                (actor, environment, idempotency_key),
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
                    command_id, actor, environment, idempotency_key,
                    request_hash, result_json, state_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command_id, actor, environment, idempotency_key,
                    request_hash, result_json, state_version, self._now(),
                ),
            )
            connection.commit()
        return result, True

    def commit_command(
        self,
        *,
        command_id: str,
        actor: str,
        environment: str,
        idempotency_key: str,
        request: Any,
        result: Any,
        state_version: int,
        events: list[tuple[dict[str, Any], str | None]],
    ) -> tuple[Any, bool, tuple[AppendResult, ...]]:
        """Atomically commit command dedupe, ordered events and outbox rows."""

        command_id = self._require_text(command_id, "command_id")
        actor, environment, idempotency_key = self._command_scope(
            actor=actor,
            environment=environment,
            idempotency_key=idempotency_key,
        )
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
                raw_aggregate_version = envelope["aggregate_version"]
            except KeyError as error:
                raise ValueError(
                    "aggregate_version must be a positive canonical integer sequence string"
                ) from error
            aggregate_version = _sequence(
                raw_aggregate_version,
                name="aggregate_version",
                positive=True,
            )
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
                self._reject_legacy_unscoped_key(connection, idempotency_key)
                existing = connection.execute(
                    "SELECT * FROM command_dedupe "
                    "WHERE actor = ? AND environment = ? AND idempotency_key = ?",
                    (actor, environment, idempotency_key),
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
                        command_id, actor, environment, idempotency_key,
                        request_hash, result_json, state_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        command_id,
                        actor,
                        environment,
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
