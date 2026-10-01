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


def _outbox_envelope_digest(topic: str, payload_json: str) -> str:
    """Bind publication routing and exact canonical envelope bytes together."""

    if not isinstance(topic, str) or not topic.strip() or topic != topic.strip():
        raise ValueError("outbox topic must be canonical non-empty text")
    if not isinstance(payload_json, str):
        raise TypeError("outbox payload_json must be text")
    identity = canonical_json({"topic": topic, "payload_json": payload_json})
    return "sha256:" + sha256(identity.encode("utf-8")).hexdigest()


def _event_envelope_digest(envelope_json: str) -> str:
    """Integrity-bind the exact canonical durable event-envelope bytes."""

    if not isinstance(envelope_json, str):
        raise TypeError("event envelope_json must be text")
    return "sha256:" + sha256(envelope_json.encode("utf-8")).hexdigest()


def _projection_checkpoint_digest(
    *,
    projection_name: str,
    aggregate_type: str,
    aggregate_id: str,
    aggregate_version: int,
    state: Any,
) -> str:
    """Bind a derived checkpoint to its exact projection identity and journal cut."""

    return payload_digest(
        {
            "projection_name": projection_name,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "aggregate_version": aggregate_version,
            "state": state,
        }
    )


_SEQUENCE_RE = re.compile(r"^(0|[1-9][0-9]*)$")

_EVENT_TYPE_RETIREMENT_REJECT_TRIGGER = "autotrade_reject_retired_event_type"
_EVENT_TYPE_RETIREMENT_UPDATE_TRIGGER = "autotrade_reject_retirement_update"
_EVENT_TYPE_RETIREMENT_DELETE_TRIGGER = "autotrade_reject_retirement_delete"
_EVENT_TYPE_RETIREMENT_REJECT_SQL = """
CREATE TRIGGER IF NOT EXISTS autotrade_reject_retired_event_type
BEFORE INSERT ON events
WHEN EXISTS (
    SELECT 1 FROM retired_event_types
    WHERE event_type = NEW.event_type
)
BEGIN
    SELECT RAISE(ABORT, 'event type is retired');
END
"""
_EVENT_TYPE_RETIREMENT_UPDATE_SQL = """
CREATE TRIGGER IF NOT EXISTS autotrade_reject_retirement_update
BEFORE UPDATE ON retired_event_types
BEGIN
    SELECT RAISE(ABORT, 'event type retirement is immutable');
END
"""
_EVENT_TYPE_RETIREMENT_DELETE_SQL = """
CREATE TRIGGER IF NOT EXISTS autotrade_reject_retirement_delete
BEFORE DELETE ON retired_event_types
BEGIN
    SELECT RAISE(ABORT, 'event type retirement is immutable');
END
"""


_PROTECTED_WRITER_GUARD_TRIGGER = "autotrade_protected_event_writer_guard"
_PROTECTED_WRITER_NAMESPACE_UPDATE_TRIGGER = (
    "autotrade_protected_writer_namespace_no_update"
)
_PROTECTED_WRITER_NAMESPACE_DELETE_TRIGGER = (
    "autotrade_protected_writer_namespace_no_delete"
)
_PROTECTED_WRITER_GUARD_SQL = """
CREATE TRIGGER IF NOT EXISTS autotrade_protected_event_writer_guard
BEFORE INSERT ON events
WHEN EXISTS (
    SELECT 1 FROM protected_writer_namespaces
    WHERE aggregate_type = NEW.aggregate_type
)
BEGIN
    SELECT CASE
        WHEN COALESCE(
            autotrade_protected_writer_authority(NEW.aggregate_type),
            ''
        ) != (
            SELECT writer_authority_hash
            FROM protected_writer_namespaces
            WHERE aggregate_type = NEW.aggregate_type
        )
        THEN RAISE(
            ABORT,
            'protected journal aggregate requires exact writer authority'
        )
    END;
    SELECT CASE
        WHEN NEW.writer_namespace != (
            SELECT namespace FROM protected_writer_namespaces
            WHERE aggregate_type = NEW.aggregate_type
        )
        OR NEW.writer_authority_id != (
            SELECT writer_authority_id FROM protected_writer_namespaces
            WHERE aggregate_type = NEW.aggregate_type
        )
        OR NEW.writer_authority_hash != (
            SELECT writer_authority_hash FROM protected_writer_namespaces
            WHERE aggregate_type = NEW.aggregate_type
        )
        OR NEW.writer_provenance_hash IS NULL
        THEN RAISE(
            ABORT,
            'protected journal event lacks exact writer provenance'
        )
    END;
END
"""
_PROTECTED_WRITER_NAMESPACE_UPDATE_SQL = """
CREATE TRIGGER IF NOT EXISTS autotrade_protected_writer_namespace_no_update
BEFORE UPDATE ON protected_writer_namespaces
BEGIN
    SELECT RAISE(ABORT, 'protected writer namespace is immutable');
END
"""
_PROTECTED_WRITER_NAMESPACE_DELETE_SQL = """
CREATE TRIGGER IF NOT EXISTS autotrade_protected_writer_namespace_no_delete
BEFORE DELETE ON protected_writer_namespaces
BEGIN
    SELECT RAISE(ABORT, 'protected writer namespace is immutable');
END
"""


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


def _protected_writer_authority_digest(
    *,
    aggregate_type: str,
    namespace: str,
    writer_authority_id: str,
) -> str:
    return payload_digest(
        {
            "schema_version": "1.0.0",
            "aggregate_type": aggregate_type,
            "namespace": namespace,
            "writer_authority_id": writer_authority_id,
        }
    )


def _event_writer_provenance_digest(
    *,
    event_id: str,
    aggregate_type: str,
    aggregate_id: str,
    aggregate_version: int,
    envelope_hash: str,
    journal_sequence: int,
    namespace: str,
    writer_authority_id: str,
    writer_authority_hash: str,
) -> str:
    return payload_digest(
        {
            "schema_version": "1.0.0",
            "event_id": event_id,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "aggregate_version": aggregate_version,
            "envelope_hash": envelope_hash,
            "journal_sequence": journal_sequence,
            "namespace": namespace,
            "writer_authority_id": writer_authority_id,
            "writer_authority_hash": writer_authority_hash,
        }
    )


class ProtectedWriterCapability:
    """Opaque process-lifetime handle for one durably registered writer."""

    __slots__ = (
        "aggregate_type",
        "namespace",
        "writer_authority_id",
        "writer_authority_hash",
        "_store_instance_id",
    )

    def __init__(self, *args, **kwargs):
        raise TypeError(
            "ProtectedWriterCapability is issued only by JournalStore authority"
        )

    def __init_subclass__(cls, **kwargs):
        raise TypeError("ProtectedWriterCapability cannot be subclassed")


class AggregatePreconditionFailed(ValueError):
    """A read-only aggregate head changed before any requested mutation."""


class JournalSequencePreconditionFailed(ValueError):
    """The global durable journal cut changed before a requested mutation."""


@dataclass(frozen=True)
class ExpectedAggregateHead:
    """Immutable read-only aggregate precondition for one journal transaction."""

    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    latest_event_id: str | None = None
    latest_payload_hash: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.aggregate_type, "aggregate_type"),
            (self.aggregate_id, "aggregate_id"),
        ):
            if type(value) is not str or not value or value != value.strip():
                raise ValueError(f"{name} must be canonical non-empty text")
        if type(self.aggregate_version) is not int or self.aggregate_version < 0:
            raise ValueError("aggregate_version must be a non-negative integer")
        for value, name in (
            (self.latest_event_id, "latest_event_id"),
            (self.latest_payload_hash, "latest_payload_hash"),
        ):
            if (
                value is not None
                and (
                    type(value) is not str
                    or not value
                    or value != value.strip()
                )
            ):
                raise ValueError(
                    f"{name} must be canonical non-empty text when provided"
                )
        if self.aggregate_version == 0 and (
            self.latest_event_id is not None
            or self.latest_payload_hash is not None
        ):
            raise ValueError(
                "empty aggregate precondition cannot name a latest event"
            )


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

    SCHEMA_VERSION = 11

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._protected_writer_capabilities: dict[int, ProtectedWriterCapability] = {}
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.create_function(
                "autotrade_protected_writer_authority",
                1,
                lambda _aggregate_type: None,
            )
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
        if version == 4:
            return (
                "ALTER TABLE outbox ADD COLUMN envelope_hash TEXT",
            )
        if version == 5:
            return (
                "ALTER TABLE command_dedupe ADD COLUMN result_hash TEXT",
                "ALTER TABLE events ADD COLUMN envelope_json TEXT",
                "ALTER TABLE events ADD COLUMN envelope_hash TEXT",
            )
        if version == 6:
            return (
                "ALTER TABLE events ADD COLUMN journal_sequence INTEGER",
                "UPDATE events SET journal_sequence = rowid "
                "WHERE journal_sequence IS NULL",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_journal_sequence "
                "ON events(journal_sequence)",
            )
        if version == 7:
            return (
                """
                CREATE TABLE IF NOT EXISTS global_projection_checkpoints (
                    projection_name TEXT PRIMARY KEY,
                    journal_sequence INTEGER NOT NULL CHECK (journal_sequence >= 0),
                    state_json TEXT NOT NULL,
                    state_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
            )
        if version == 8:
            # v7 checkpoint hashes covered state only, not projection identity or
            # aggregate cut. Those missing bindings cannot be reconstructed after
            # the fact without trusting potentially tampered derived rows. Drop
            # only derived checkpoints and rebuild them from the authoritative
            # event journal under the v8 digest semantics.
            return ("DELETE FROM projection_checkpoints",)
        if version == 9:
            # Pre-v9 command rows do not prove whether they came from the
            # result-only API or from an atomic event-batch commit. Preserve them
            # as explicitly ambiguous instead of inventing transactional proof.
            return (
                "ALTER TABLE command_dedupe ADD COLUMN effect_kind TEXT",
                "ALTER TABLE command_dedupe ADD COLUMN effect_json TEXT",
                "ALTER TABLE command_dedupe ADD COLUMN effect_hash TEXT",
                "UPDATE command_dedupe SET effect_kind = 'LEGACY_UNKNOWN' "
                "WHERE effect_kind IS NULL",
            )
        if version == 10:
            # Event-type retirement is a durable cross-runtime writer fence.
            # A process that initialized under an older schema still executes
            # INSERT against this same events table; the database trigger, not
            # process-local code, therefore blocks a retired writer after cutover.
            return (
                """
                CREATE TABLE IF NOT EXISTS retired_event_types (
                    event_type TEXT PRIMARY KEY,
                    retirement_id TEXT NOT NULL,
                    retirement_hash TEXT NOT NULL
                )
                """,
                _EVENT_TYPE_RETIREMENT_REJECT_SQL,
                _EVENT_TYPE_RETIREMENT_UPDATE_SQL,
                _EVENT_TYPE_RETIREMENT_DELETE_SQL,
            )
        if version == 11:
            # Protected writer provenance is separate from event payload/envelope
            # authority. Historical rows remain explicitly unqualified.
            return (
                """
                CREATE TABLE IF NOT EXISTS protected_writer_namespaces (
                    aggregate_type TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL UNIQUE,
                    writer_authority_id TEXT NOT NULL,
                    writer_authority_hash TEXT NOT NULL
                )
                """,
                "ALTER TABLE events ADD COLUMN writer_namespace TEXT",
                "ALTER TABLE events ADD COLUMN writer_authority_id TEXT",
                "ALTER TABLE events ADD COLUMN writer_authority_hash TEXT",
                "ALTER TABLE events ADD COLUMN writer_provenance_hash TEXT",
                _PROTECTED_WRITER_GUARD_SQL,
                _PROTECTED_WRITER_NAMESPACE_UPDATE_SQL,
                _PROTECTED_WRITER_NAMESPACE_DELETE_SQL,
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
        if cls.SCHEMA_VERSION >= 5:
            command_columns.add("result_hash")
        if cls.SCHEMA_VERSION >= 9:
            command_columns.update({"effect_kind", "effect_json", "effect_hash"})
        required = {
            "schema_migrations": frozenset({"version", "applied_at"}),
            "events": frozenset({
                "event_id", "event_type", "aggregate_type", "aggregate_id",
                "aggregate_version", "payload_json", "payload_hash", "committed_at",
            } | ({"envelope_json", "envelope_hash"} if cls.SCHEMA_VERSION >= 5 else set())
              | ({"journal_sequence"} if cls.SCHEMA_VERSION >= 6 else set())
              | ({
                    "writer_namespace",
                    "writer_authority_id",
                    "writer_authority_hash",
                    "writer_provenance_hash",
                } if cls.SCHEMA_VERSION >= 11 else set())),
            "outbox": frozenset({
                "outbox_id", "event_id", "topic", "payload_json",
                "created_at", "delivered_at"
            } | ({"envelope_hash"} if cls.SCHEMA_VERSION >= 4 else set())),
            "command_dedupe": frozenset(command_columns),
        }
        if cls.SCHEMA_VERSION >= 2:
            required["projection_checkpoints"] = frozenset({
                "projection_name", "aggregate_type", "aggregate_id",
                "aggregate_version", "state_json", "state_hash", "updated_at",
            })
        if cls.SCHEMA_VERSION >= 7:
            required["global_projection_checkpoints"] = frozenset({
                "projection_name", "journal_sequence",
                "state_json", "state_hash", "updated_at",
            })
        if cls.SCHEMA_VERSION >= 10:
            required["retired_event_types"] = frozenset({
                "event_type", "retirement_id", "retirement_hash",
            })
        if cls.SCHEMA_VERSION >= 11:
            required["protected_writer_namespaces"] = frozenset({
                "aggregate_type",
                "namespace",
                "writer_authority_id",
                "writer_authority_hash",
            })
        return required

    @classmethod
    def _required_column_contracts(
        cls,
    ) -> dict[str, dict[str, tuple[str, bool]]]:
        """Return the exact declared type/nullability contract for this schema version.

        Column names alone are insufficient migration evidence. SQLite will accept
        a pre-existing CREATE TABLE IF NOT EXISTS target with weaker affinity or
        nullability, which can otherwise make a partial/corrupt schema look current.
        Keep this version-aware so the legacy stores used by migration qualification
        continue to validate the exact schema they actually own.
        """

        command_columns: dict[str, tuple[str, bool]]
        if cls.SCHEMA_VERSION >= 3:
            command_columns = {
                "command_id": ("TEXT", False),
                "actor": ("TEXT", True),
                "environment": ("TEXT", True),
                "idempotency_key": ("TEXT", True),
                "request_hash": ("TEXT", True),
                "result_json": ("TEXT", True),
                "state_version": ("INTEGER", True),
                "created_at": ("TEXT", True),
            }
        else:
            command_columns = {
                "command_id": ("TEXT", False),
                "idempotency_key": ("TEXT", True),
                "request_hash": ("TEXT", True),
                "result_json": ("TEXT", True),
                "state_version": ("INTEGER", True),
                "created_at": ("TEXT", True),
            }
        if cls.SCHEMA_VERSION >= 5:
            command_columns["result_hash"] = ("TEXT", False)
        if cls.SCHEMA_VERSION >= 9:
            command_columns.update(
                {
                    "effect_kind": ("TEXT", False),
                    "effect_json": ("TEXT", False),
                    "effect_hash": ("TEXT", False),
                }
            )

        events = {
            "event_id": ("TEXT", False),
            "event_type": ("TEXT", True),
            "aggregate_type": ("TEXT", True),
            "aggregate_id": ("TEXT", True),
            "aggregate_version": ("INTEGER", True),
            "payload_json": ("TEXT", True),
            "payload_hash": ("TEXT", True),
            "committed_at": ("TEXT", True),
        }
        if cls.SCHEMA_VERSION >= 5:
            events.update(
                {
                    "envelope_json": ("TEXT", False),
                    "envelope_hash": ("TEXT", False),
                }
            )
        if cls.SCHEMA_VERSION >= 6:
            events["journal_sequence"] = ("INTEGER", False)
        if cls.SCHEMA_VERSION >= 11:
            events.update(
                {
                    "writer_namespace": ("TEXT", False),
                    "writer_authority_id": ("TEXT", False),
                    "writer_authority_hash": ("TEXT", False),
                    "writer_provenance_hash": ("TEXT", False),
                }
            )

        outbox = {
            "outbox_id": ("TEXT", False),
            "event_id": ("TEXT", True),
            "topic": ("TEXT", True),
            "payload_json": ("TEXT", True),
            "created_at": ("TEXT", True),
            "delivered_at": ("TEXT", False),
        }
        if cls.SCHEMA_VERSION >= 4:
            outbox["envelope_hash"] = ("TEXT", False)

        required = {
            "schema_migrations": {
                "version": ("INTEGER", False),
                "applied_at": ("TEXT", True),
            },
            "events": events,
            "outbox": outbox,
            "command_dedupe": command_columns,
        }
        if cls.SCHEMA_VERSION >= 2:
            required["projection_checkpoints"] = {
                "projection_name": ("TEXT", True),
                "aggregate_type": ("TEXT", True),
                "aggregate_id": ("TEXT", True),
                "aggregate_version": ("INTEGER", True),
                "state_json": ("TEXT", True),
                "state_hash": ("TEXT", True),
                "updated_at": ("TEXT", True),
            }
        if cls.SCHEMA_VERSION >= 7:
            required["global_projection_checkpoints"] = {
                "projection_name": ("TEXT", False),
                "journal_sequence": ("INTEGER", True),
                "state_json": ("TEXT", True),
                "state_hash": ("TEXT", True),
                "updated_at": ("TEXT", True),
            }
        if cls.SCHEMA_VERSION >= 10:
            required["retired_event_types"] = {
                "event_type": ("TEXT", False),
                "retirement_id": ("TEXT", True),
                "retirement_hash": ("TEXT", True),
            }
        if cls.SCHEMA_VERSION >= 11:
            required["protected_writer_namespaces"] = {
                "aggregate_type": ("TEXT", False),
                "namespace": ("TEXT", True),
                "writer_authority_id": ("TEXT", True),
                "writer_authority_hash": ("TEXT", True),
            }
        return required

    @classmethod
    def _validate_column_contracts(cls, connection) -> None:
        for table_name, expected in cls._required_column_contracts().items():
            actual = {
                str(row["name"]): (
                    str(row["type"]).strip().upper(),
                    bool(row["notnull"]),
                )
                for row in connection.execute(f"PRAGMA table_info({table_name})")
            }
            unexpected = set(actual) - set(expected)
            if unexpected:
                raise ValueError(
                    "Journal schema table "
                    + table_name
                    + " has unexpected columns: "
                    + ", ".join(sorted(unexpected))
                )
            for column_name, (expected_type, expected_not_null) in expected.items():
                if column_name not in actual:
                    # Missing columns are reported by the existing structural
                    # check with its established diagnostic.
                    continue
                declared_type, not_null = actual[column_name]
                if declared_type != expected_type:
                    raise ValueError(
                        "Journal schema table "
                        + table_name
                        + " column "
                        + column_name
                        + " has invalid declared type"
                    )
                if not_null != expected_not_null:
                    raise ValueError(
                        "Journal schema table "
                        + table_name
                        + " column "
                        + column_name
                        + " has invalid NOT NULL contract"
                    )

    @staticmethod
    def _unique_index_columns(connection, table_name: str) -> set[tuple[str, ...]]:
        unique_indexes: set[tuple[str, ...]] = set()
        for index_row in connection.execute(f"PRAGMA index_list({table_name})"):
            # A partial UNIQUE index constrains only rows matching its WHERE
            # predicate and therefore cannot satisfy a whole-table identity
            # invariant, even when PRAGMA index_info reports the same columns.
            if not bool(index_row["unique"]) or bool(index_row["partial"]):
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
        if cls.SCHEMA_VERSION >= 7:
            expected_primary_keys["global_projection_checkpoints"] = (
                "projection_name",
            )
        if cls.SCHEMA_VERSION >= 10:
            expected_primary_keys["retired_event_types"] = ("event_type",)
        if cls.SCHEMA_VERSION >= 11:
            expected_primary_keys["protected_writer_namespaces"] = (
                "aggregate_type",
            )
        expected_unique = {
            "events": {
                ("aggregate_type", "aggregate_id", "aggregate_version"),
            } | (
                {("journal_sequence",)}
                if cls.SCHEMA_VERSION >= 6
                else set()
            ),
            "outbox": {("event_id",)},
            "command_dedupe": (
                {("actor", "environment", "idempotency_key")}
                if cls.SCHEMA_VERSION >= 3
                else {("idempotency_key",)}
            ),
        }
        if cls.SCHEMA_VERSION >= 11:
            expected_unique["protected_writer_namespaces"] = {("namespace",)}
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

        if cls.SCHEMA_VERSION >= 6:
            raw_sequences = [
                row[0]
                for row in connection.execute(
                    "SELECT journal_sequence FROM events ORDER BY journal_sequence"
                )
            ]
            if any(
                type(value) is not int or value <= 0
                for value in raw_sequences
            ) or raw_sequences != list(range(1, len(raw_sequences) + 1)):
                raise ValueError(
                    "journal sequence authority is not a contiguous canonical positive integer series"
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

    @classmethod
    def _validate_event_type_retirement_contract(cls, connection) -> None:
        if cls.SCHEMA_VERSION < 10:
            return
        expected_triggers = {
            _EVENT_TYPE_RETIREMENT_REJECT_TRIGGER: _EVENT_TYPE_RETIREMENT_REJECT_SQL,
            _EVENT_TYPE_RETIREMENT_UPDATE_TRIGGER: _EVENT_TYPE_RETIREMENT_UPDATE_SQL,
            _EVENT_TYPE_RETIREMENT_DELETE_TRIGGER: _EVENT_TYPE_RETIREMENT_DELETE_SQL,
        }
        for trigger_name, expected_sql in expected_triggers.items():
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (trigger_name,),
            ).fetchone()
            if row is None or not isinstance(row["sql"], str):
                raise ValueError(
                    "Journal schema is missing event-type retirement trigger: "
                    + trigger_name
                )
            actual = " ".join(str(row["sql"]).split())
            expected = " ".join(expected_sql.replace("IF NOT EXISTS ", "").split())
            if actual != expected:
                raise ValueError(
                    "Journal event-type retirement trigger contract mismatch: "
                    + trigger_name
                )

        for row in connection.execute(
            "SELECT event_type, retirement_id, retirement_hash "
            "FROM retired_event_types ORDER BY event_type"
        ):
            event_type = row["event_type"]
            retirement_id = row["retirement_id"]
            retirement_hash = row["retirement_hash"]
            if (
                not isinstance(event_type, str)
                or not event_type.strip()
                or event_type != event_type.strip()
                or not isinstance(retirement_id, str)
                or not retirement_id.strip()
                or retirement_id != retirement_id.strip()
            ):
                raise ValueError("event type retirement record is not canonical")
            expected_hash = payload_digest(
                {"event_type": event_type, "retirement_id": retirement_id}
            )
            if retirement_hash != expected_hash:
                raise ValueError("event type retirement hash mismatch")

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
                    if version == 4:
                        for row in connection.execute(
                            "SELECT outbox_id, payload_json FROM outbox"
                        ):
                            envelope_hash = (
                                "sha256:"
                                + sha256(
                                    str(row["payload_json"]).encode("utf-8")
                                ).hexdigest()
                            )
                            connection.execute(
                                "UPDATE outbox SET envelope_hash = ? "
                                "WHERE outbox_id = ?",
                                (envelope_hash, row["outbox_id"]),
                            )
                    if version == 5:
                        for row in connection.execute(
                            "SELECT command_id, result_json FROM command_dedupe"
                        ):
                            result_json = str(row["result_json"])
                            try:
                                result_value = json.loads(result_json)
                            except (json.JSONDecodeError, TypeError) as error:
                                raise ValueError(
                                    "legacy command result is not valid JSON"
                                ) from error
                            if canonical_json(result_value) != result_json:
                                raise ValueError(
                                    "legacy command result is not canonical JSON"
                                )
                            result_hash = (
                                "sha256:"
                                + sha256(result_json.encode("utf-8")).hexdigest()
                            )
                            connection.execute(
                                "UPDATE command_dedupe SET result_hash = ? "
                                "WHERE command_id = ?",
                                (result_hash, row["command_id"]),
                            )
                        # Schema v4 authenticated only the exact outbox
                        # envelope bytes. Schema v5 persists those canonical bytes
                        # in the journal as durable event-envelope authority and
                        # strengthens publication identity to bind routing topic.
                        #
                        # A non-null v4 hash authenticates legitimate canonical
                        # fields that the legacy core event columns did not store.
                        # A hashless legacy row has no such authority, so it is
                        # accepted only when the full envelope is reconstructable
                        # from the core journal row.
                        for row in connection.execute(
                            """
                            SELECT
                                outbox.outbox_id,
                                outbox.topic,
                                outbox.payload_json AS outbox_payload_json,
                                outbox.envelope_hash AS legacy_envelope_hash,
                                events.event_id,
                                events.event_type,
                                events.aggregate_type,
                                events.aggregate_id,
                                events.aggregate_version,
                                events.payload_json AS event_payload_json,
                                events.payload_hash,
                                events.committed_at
                            FROM outbox
                            JOIN events ON events.event_id = outbox.event_id
                            """
                        ):
                            raw_outbox_payload = str(row["outbox_payload_json"])
                            try:
                                outbox_payload = json.loads(raw_outbox_payload)
                            except (json.JSONDecodeError, TypeError) as error:
                                raise ValueError(
                                    "legacy outbox payload is not valid JSON"
                                ) from error
                            if canonical_json(outbox_payload) != raw_outbox_payload:
                                raise ValueError(
                                    "legacy outbox payload is not canonical JSON"
                                )
                            event = self._decode_event_row(
                                {
                                    "event_id": row["event_id"],
                                    "event_type": row["event_type"],
                                    "aggregate_type": row["aggregate_type"],
                                    "aggregate_id": row["aggregate_id"],
                                    "aggregate_version": row["aggregate_version"],
                                    "payload_json": row["event_payload_json"],
                                    "payload_hash": row["payload_hash"],
                                    "committed_at": row["committed_at"],
                                }
                            )
                            core_envelope = {
                                "event_id": event["event_id"],
                                "event_type": event["event_type"],
                                "aggregate_type": event["aggregate_type"],
                                "aggregate_id": event["aggregate_id"],
                                "aggregate_version": str(event["aggregate_version"]),
                                "payload": event["payload"],
                                "payload_hash": event["payload_hash"],
                                "committed_at": event["committed_at"],
                            }
                            legacy_hash = row["legacy_envelope_hash"]
                            if legacy_hash is None:
                                if outbox_payload != core_envelope:
                                    raise ValueError(
                                        "legacy outbox payload is not exactly reconstructable "
                                        "from authoritative journal event"
                                    )
                            else:
                                expected_v4_hash = (
                                    "sha256:"
                                    + sha256(
                                        raw_outbox_payload.encode("utf-8")
                                    ).hexdigest()
                                )
                                if legacy_hash != expected_v4_hash:
                                    raise ValueError(
                                        "legacy outbox envelope hash does not match "
                                        "the schema-v4 payload-only digest"
                                    )
                                for key, expected in core_envelope.items():
                                    if outbox_payload.get(key) != expected:
                                        raise ValueError(
                                            "legacy outbox payload conflicts with "
                                            "authoritative journal event"
                                        )
                            connection.execute(
                                "UPDATE events SET envelope_json = ?, envelope_hash = ? "
                                "WHERE event_id = ?",
                                (
                                    raw_outbox_payload,
                                    _event_envelope_digest(raw_outbox_payload),
                                    row["event_id"],
                                ),
                            )
                            envelope_hash = _outbox_envelope_digest(
                                str(row["topic"]),
                                raw_outbox_payload,
                            )
                            connection.execute(
                                "UPDATE outbox SET envelope_hash = ? "
                                "WHERE outbox_id = ?",
                                (envelope_hash, row["outbox_id"]),
                            )

                        # Events with no publication intent have no legacy full
                        # envelope bytes to preserve. Persist the exact canonical
                        # core envelope that can be proven from the journal.
                        for row in connection.execute(
                            """
                            SELECT event_id, event_type, aggregate_type, aggregate_id,
                                   aggregate_version, payload_json, payload_hash,
                                   committed_at
                            FROM events
                            WHERE envelope_json IS NULL
                            """
                        ):
                            event = self._decode_event_row(row)
                            core_envelope = {
                                "event_id": event["event_id"],
                                "event_type": event["event_type"],
                                "aggregate_type": event["aggregate_type"],
                                "aggregate_id": event["aggregate_id"],
                                "aggregate_version": str(event["aggregate_version"]),
                                "payload": event["payload"],
                                "payload_hash": event["payload_hash"],
                                "committed_at": event["committed_at"],
                            }
                            core_envelope_json = canonical_json(core_envelope)
                            connection.execute(
                                "UPDATE events SET envelope_json = ?, envelope_hash = ? "
                                "WHERE event_id = ?",
                                (
                                    core_envelope_json,
                                    _event_envelope_digest(core_envelope_json),
                                    row["event_id"],
                                ),
                            )
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
                if self.SCHEMA_VERSION >= 7:
                    required_tables.add("global_projection_checkpoints")
                if self.SCHEMA_VERSION >= 10:
                    required_tables.add("retired_event_types")
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
                self._validate_column_contracts(connection)
                self._validate_key_contracts(connection)
                self._validate_event_type_retirement_contract(connection)
                self._validate_protected_writer_trigger_contract(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @classmethod
    def _validate_protected_writer_trigger_contract(cls, connection) -> None:
        if cls.SCHEMA_VERSION < 11:
            return
        expected_triggers = {
            _PROTECTED_WRITER_GUARD_TRIGGER: _PROTECTED_WRITER_GUARD_SQL,
            _PROTECTED_WRITER_NAMESPACE_UPDATE_TRIGGER:
                _PROTECTED_WRITER_NAMESPACE_UPDATE_SQL,
            _PROTECTED_WRITER_NAMESPACE_DELETE_TRIGGER:
                _PROTECTED_WRITER_NAMESPACE_DELETE_SQL,
        }
        for trigger_name, expected_sql in expected_triggers.items():
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (trigger_name,),
            ).fetchone()
            if row is None or not isinstance(row["sql"], str):
                raise ValueError(
                    "Journal schema is missing protected-writer trigger: "
                    + trigger_name
                )
            actual = " ".join(str(row["sql"]).split())
            expected = " ".join(
                expected_sql.replace("IF NOT EXISTS ", "").split()
            )
            if actual != expected:
                raise ValueError(
                    "Journal protected-writer trigger contract mismatch: "
                    + trigger_name
                )

    def current_schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()
        return 0 if row is None or row[0] is None else int(row[0])

    def retire_event_type(self, event_type: str, *, retirement_id: str) -> bool:
        """Durably fence all future inserts of one legacy event type.

        The fence lives in SQLite rather than process memory so an already-running
        older runtime using the same events table is blocked after the retirement
        transaction commits. Existing historical events remain readable. retirement_id is a deterministic
        migration/generation identity, never a wall-clock attestation.
        """

        if self.SCHEMA_VERSION < 10:
            raise RuntimeError("event type retirement requires journal schema v10")
        event_type = self._require_text(event_type, "event_type")
        retirement_id = self._require_text(retirement_id, "retirement_id")
        retirement_hash = payload_digest(
            {"event_type": event_type, "retirement_id": retirement_id}
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT retirement_id, retirement_hash "
                    "FROM retired_event_types WHERE event_type = ?",
                    (event_type,),
                ).fetchone()
                if existing is not None:
                    exact = (
                        existing["retirement_id"] == retirement_id
                        and existing["retirement_hash"] == retirement_hash
                    )
                    if not exact:
                        raise ValueError(
                            "event type retirement conflicts with existing fence"
                        )
                    self._validate_event_type_retirement_contract(connection)
                    connection.commit()
                    return False
                connection.execute(
                    "INSERT INTO retired_event_types("
                    "event_type, retirement_id, retirement_hash"
                    ") VALUES (?, ?, ?)",
                    (event_type, retirement_id, retirement_hash),
                )
                self._validate_event_type_retirement_contract(connection)
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

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

        row_keys = set(row.keys())
        if "journal_sequence" in row_keys:
            journal_sequence = row["journal_sequence"]
            if type(journal_sequence) is not int or journal_sequence <= 0:
                raise ValueError("journal event sequence must be a positive integer")
        if "envelope_json" in row_keys:
            raw_envelope = row["envelope_json"]
            if not isinstance(raw_envelope, str):
                raise ValueError("journal event envelope authority is missing")
            if "envelope_hash" not in row_keys:
                raise ValueError("journal event envelope hash is missing")
            if row["envelope_hash"] != _event_envelope_digest(raw_envelope):
                raise ValueError("journal event envelope hash does not match stored envelope")
            try:
                envelope = json.loads(raw_envelope)
            except (json.JSONDecodeError, TypeError) as error:
                raise ValueError("journal event envelope is not valid JSON") from error
            if canonical_json(envelope) != raw_envelope:
                raise ValueError("journal event envelope is not canonical JSON")
            core_envelope = {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "aggregate_type": row["aggregate_type"],
                "aggregate_id": row["aggregate_id"],
                "aggregate_version": str(row["aggregate_version"]),
                "payload": payload,
                "payload_hash": row["payload_hash"],
                "committed_at": row["committed_at"],
            }
            for key, expected in core_envelope.items():
                if envelope.get(key) != expected:
                    raise ValueError(
                        "journal event envelope conflicts with core journal event"
                    )
        decoded = {
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "aggregate_type": row["aggregate_type"],
            "aggregate_id": row["aggregate_id"],
            "aggregate_version": row["aggregate_version"],
            "payload": payload,
            "payload_hash": row["payload_hash"],
            "committed_at": row["committed_at"],
        }
        if "envelope_json" in row_keys:
            # The full immutable envelope is authoritative for runtime metadata
            # (environment, owner epoch, causation/correlation, evidence refs,
            # etc.). Preserve those fields after integrity verification while
            # retaining the historical load API's integer aggregate_version.
            decoded = dict(envelope)
            decoded.update(
                {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "aggregate_type": row["aggregate_type"],
                    "aggregate_id": row["aggregate_id"],
                    "aggregate_version": row["aggregate_version"],
                    "payload": payload,
                    "payload_hash": row["payload_hash"],
                    "committed_at": row["committed_at"],
                }
            )
        if "journal_sequence" in row_keys:
            decoded["journal_sequence"] = journal_sequence

        writer_columns = {
            "writer_namespace",
            "writer_authority_id",
            "writer_authority_hash",
            "writer_provenance_hash",
        }
        if writer_columns.issubset(row_keys):
            raw_writer = {name: row[name] for name in writer_columns}
            present = {
                name: value
                for name, value in raw_writer.items()
                if value is not None
            }
            if present and len(present) != len(writer_columns):
                raise ValueError(
                    "journal event writer provenance is only partially populated"
                )
            if present:
                namespace = raw_writer["writer_namespace"]
                writer_authority_id = raw_writer["writer_authority_id"]
                writer_authority_hash = raw_writer["writer_authority_hash"]
                writer_provenance_hash = raw_writer["writer_provenance_hash"]
                if (
                    type(namespace) is not str
                    or not namespace
                    or namespace != namespace.strip()
                    or type(writer_authority_id) is not str
                    or not writer_authority_id
                    or writer_authority_id != writer_authority_id.strip()
                    or type(writer_authority_hash) is not str
                    or type(writer_provenance_hash) is not str
                ):
                    raise ValueError(
                        "journal event writer provenance is non-canonical"
                    )
                expected_authority_hash = _protected_writer_authority_digest(
                    aggregate_type=str(row["aggregate_type"]),
                    namespace=namespace,
                    writer_authority_id=writer_authority_id,
                )
                if writer_authority_hash != expected_authority_hash:
                    raise ValueError(
                        "journal event writer authority hash does not match identity"
                    )
                if "journal_sequence" not in row_keys or "envelope_hash" not in row_keys:
                    raise ValueError(
                        "journal event writer provenance lacks immutable event cut"
                    )
                expected_provenance_hash = _event_writer_provenance_digest(
                    event_id=str(row["event_id"]),
                    aggregate_type=str(row["aggregate_type"]),
                    aggregate_id=str(row["aggregate_id"]),
                    aggregate_version=int(row["aggregate_version"]),
                    envelope_hash=str(row["envelope_hash"]),
                    journal_sequence=journal_sequence,
                    namespace=namespace,
                    writer_authority_id=writer_authority_id,
                    writer_authority_hash=writer_authority_hash,
                )
                if writer_provenance_hash != expected_provenance_hash:
                    raise ValueError(
                        "journal event writer provenance hash does not match event"
                    )
                decoded.update(raw_writer)
        return decoded

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        event_id = self._require_text(event_id, "event_id")
        envelope_columns = (
            ", envelope_json, envelope_hash, journal_sequence"
            if self.SCHEMA_VERSION >= 6
            else (
                ", envelope_json, envelope_hash"
                if self.SCHEMA_VERSION >= 5
                else ""
            )
        )
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT event_id, event_type, aggregate_type, aggregate_id,
                       aggregate_version, payload_json, payload_hash, committed_at
                       {envelope_columns}
                FROM events WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        return self._decode_event_row(row)

    @staticmethod
    def _aggregate_version_value(
        connection: sqlite3.Connection,
        aggregate_type: str,
        aggregate_id: str,
    ) -> int:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS event_count,
                MIN(aggregate_version) AS first_version,
                MAX(aggregate_version) AS last_version,
                COUNT(DISTINCT aggregate_version) AS distinct_version_count,
                SUM(
                    CASE
                        WHEN typeof(aggregate_version) = 'integer' THEN 0
                        ELSE 1
                    END
                ) AS non_integer_count
            FROM events
            WHERE aggregate_type = ? AND aggregate_id = ?
            """,
            (aggregate_type, aggregate_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("aggregate version authority query returned no row")
        event_count = row["event_count"]
        if type(event_count) is not int or event_count < 0:
            raise ValueError(
                "aggregate version authority count is not a canonical integer"
            )
        if event_count == 0:
            return 0
        first_version = row["first_version"]
        last_version = row["last_version"]
        distinct_version_count = row["distinct_version_count"]
        non_integer_count = row["non_integer_count"]
        if (
            type(first_version) is not int
            or type(last_version) is not int
            or type(distinct_version_count) is not int
            or type(non_integer_count) is not int
            or non_integer_count != 0
            or first_version != 1
            or last_version != event_count
            or distinct_version_count != event_count
        ):
            raise ValueError(
                "aggregate version authority is not a contiguous positive integer sequence"
            )
        return last_version

    @staticmethod
    def _validated_aggregate_preconditions(
        value: tuple[ExpectedAggregateHead, ...],
    ) -> tuple[ExpectedAggregateHead, ...]:
        if type(value) is not tuple:
            raise TypeError("aggregate_preconditions must be an exact tuple")
        if len(value) > 16:
            raise ValueError("aggregate_preconditions cannot contain more than 16 heads")
        seen: set[tuple[str, str]] = set()
        canonical: list[ExpectedAggregateHead] = []
        for item in value:
            if type(item) is not ExpectedAggregateHead:
                raise TypeError(
                    "aggregate_preconditions must contain exact ExpectedAggregateHead values"
                )
            # Reconstruct instead of trusting a previously-created frozen
            # instance: object.__setattr__ can otherwise corrupt dataclass
            # fields inside the same process after __post_init__ ran.
            normalized = ExpectedAggregateHead(
                item.aggregate_type,
                item.aggregate_id,
                item.aggregate_version,
                latest_event_id=item.latest_event_id,
                latest_payload_hash=item.latest_payload_hash,
            )
            key = (normalized.aggregate_type, normalized.aggregate_id)
            if key in seen:
                raise ValueError(
                    "aggregate_preconditions cannot repeat an aggregate identity"
                )
            seen.add(key)
            canonical.append(normalized)
        return tuple(canonical)

    @classmethod
    def _require_aggregate_preconditions(
        cls,
        connection: sqlite3.Connection,
        aggregate_preconditions: tuple[ExpectedAggregateHead, ...],
    ) -> None:
        for expected in aggregate_preconditions:
            current = cls._aggregate_version_value(
                connection,
                expected.aggregate_type,
                expected.aggregate_id,
            )
            if current != expected.aggregate_version:
                raise AggregatePreconditionFailed(
                    "aggregate head precondition failed for "
                    f"{expected.aggregate_type}/{expected.aggregate_id}: "
                    f"expected version {expected.aggregate_version}, found {current}"
                )
            if current == 0:
                continue
            row = connection.execute(
                """
                SELECT event_id, event_type, aggregate_type, aggregate_id,
                       aggregate_version, payload_json, payload_hash, committed_at,
                       envelope_json, envelope_hash, journal_sequence
                FROM events
                WHERE aggregate_type = ? AND aggregate_id = ?
                  AND aggregate_version = ?
                """,
                (
                    expected.aggregate_type,
                    expected.aggregate_id,
                    expected.aggregate_version,
                ),
            ).fetchone()
            if row is None:
                raise AggregatePreconditionFailed(
                    "aggregate head precondition row is missing for "
                    f"{expected.aggregate_type}/{expected.aggregate_id}"
                )
            event = cls._decode_event_row(row)
            if (
                expected.latest_event_id is not None
                and event["event_id"] != expected.latest_event_id
            ):
                raise AggregatePreconditionFailed(
                    "aggregate head event identity precondition failed for "
                    f"{expected.aggregate_type}/{expected.aggregate_id}"
                )
            if (
                expected.latest_payload_hash is not None
                and event["payload_hash"] != expected.latest_payload_hash
            ):
                raise AggregatePreconditionFailed(
                    "aggregate head payload precondition failed for "
                    f"{expected.aggregate_type}/{expected.aggregate_id}"
                )

    def next_aggregate_version(self, aggregate_type: str, aggregate_id: str) -> int:
        aggregate_type = self._require_text(aggregate_type, "aggregate_type")
        aggregate_id = self._require_text(aggregate_id, "aggregate_id")
        with self._connect() as connection:
            current = self._aggregate_version_value(
                connection,
                aggregate_type,
                aggregate_id,
            )
        return current + 1

    @staticmethod
    def _journal_sequence_value(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS event_count,
                MIN(journal_sequence) AS min_sequence,
                MAX(journal_sequence) AS max_sequence,
                SUM(
                    CASE
                        WHEN typeof(journal_sequence) != 'integer'
                          OR journal_sequence <= 0
                        THEN 1
                        ELSE 0
                    END
                ) AS invalid_sequence_count,
                COUNT(DISTINCT journal_sequence) AS distinct_sequence_count
            FROM events
            """
        ).fetchone()
        if row is None:
            raise ValueError("journal sequence authority query returned no row")
        event_count = row["event_count"]
        if type(event_count) is not int or event_count < 0:
            raise ValueError("journal sequence cardinality is not canonical")
        if event_count == 0:
            return 0

        minimum = row["min_sequence"]
        maximum = row["max_sequence"]
        invalid_count = row["invalid_sequence_count"]
        distinct_count = row["distinct_sequence_count"]
        if (
            type(minimum) is not int
            or type(maximum) is not int
            or type(invalid_count) is not int
            or type(distinct_count) is not int
            or invalid_count != 0
            or minimum != 1
            or maximum != event_count
            or distinct_count != event_count
        ):
            raise ValueError(
                "journal sequence authority is not a contiguous canonical "
                "positive integer series"
            )
        return maximum

    def current_journal_sequence(self) -> int:
        """Return the explicit durable global journal cursor."""

        with self._connect() as connection:
            return self._journal_sequence_value(connection)

    def load_events_after_journal_sequence(
        self,
        after_sequence: int,
        *,
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        """Load integrity-checked journal events strictly after a durable global cut."""

        if (
            type(after_sequence) is not int
            or after_sequence < 0
        ):
            raise ValueError("after_sequence must be a non-negative integer")
        if type(limit) is not int or limit < 1 or limit > 100000:
            raise ValueError("limit must be between 1 and 100000")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, event_type, aggregate_type, aggregate_id,
                       aggregate_version, payload_json, payload_hash, committed_at,
                       envelope_json, envelope_hash, journal_sequence
                FROM events
                WHERE journal_sequence > ?
                ORDER BY journal_sequence
                LIMIT ?
                """,
                (after_sequence, limit),
            ).fetchall()
        decoded = [self._decode_event_row(row) for row in rows]
        expected = after_sequence + 1
        for event in decoded:
            sequence = event.get("journal_sequence")
            if sequence != expected:
                raise ValueError(
                    "journal events after cut are not contiguous; qualification cannot infer conservation"
                )
            expected += 1
        return decoded

    @staticmethod
    def _require_exact_authority_text(value: object, name: str) -> str:
        if type(value) is not str or not value or value != value.strip():
            raise ValueError(f"{name} must be exact canonical non-empty text")
        return value

    def _protected_writer_registry(self) -> dict[int, tuple[object, str, str, str, str, int]]:
        registry = getattr(self, "_protected_writer_capabilities", None)
        if type(registry) is not dict:
            raise RuntimeError("protected writer capability registry is unavailable")
        return registry

    def bind_protected_writer(
        self,
        *,
        aggregate_type: str,
        namespace: str,
        writer_authority_id: str,
    ) -> ProtectedWriterCapability:
        """Bind a protected aggregate writer without upgrading legacy history."""

        aggregate_type = self._require_exact_authority_text(
            aggregate_type, "aggregate_type"
        )
        namespace = self._require_exact_authority_text(namespace, "namespace")
        writer_authority_id = self._require_exact_authority_text(
            writer_authority_id, "writer_authority_id"
        )
        authority_hash = _protected_writer_authority_digest(
            aggregate_type=aggregate_type,
            namespace=namespace,
            writer_authority_id=writer_authority_id,
        )
        if self.SCHEMA_VERSION < 11:
            raise RuntimeError(
                "protected writer provenance requires journal schema v11"
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT aggregate_type, namespace, writer_authority_id,
                           writer_authority_hash
                    FROM protected_writer_namespaces
                    WHERE aggregate_type = ?
                    """,
                    (aggregate_type,),
                ).fetchone()
                if existing is None:
                    legacy = connection.execute(
                        "SELECT 1 FROM events WHERE aggregate_type = ? LIMIT 1",
                        (aggregate_type,),
                    ).fetchone()
                    if legacy is not None:
                        raise ValueError(
                            "protected writer registration cannot bless "
                            "pre-existing aggregate history"
                        )
                    namespace_owner = connection.execute(
                        """
                        SELECT aggregate_type
                        FROM protected_writer_namespaces
                        WHERE namespace = ?
                        """,
                        (namespace,),
                    ).fetchone()
                    if namespace_owner is not None:
                        raise ValueError(
                            "protected writer namespace is already bound"
                        )
                    connection.execute(
                        """
                        INSERT INTO protected_writer_namespaces(
                            aggregate_type, namespace, writer_authority_id,
                            writer_authority_hash
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            aggregate_type,
                            namespace,
                            writer_authority_id,
                            authority_hash,
                        ),
                    )
                elif (
                    existing["namespace"] != namespace
                    or existing["writer_authority_id"] != writer_authority_id
                    or existing["writer_authority_hash"] != authority_hash
                ):
                    raise ValueError(
                        "protected writer registration conflicts with durable authority"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        capability = object.__new__(ProtectedWriterCapability)
        object.__setattr__(capability, "aggregate_type", aggregate_type)
        object.__setattr__(capability, "namespace", namespace)
        object.__setattr__(
            capability, "writer_authority_id", writer_authority_id
        )
        object.__setattr__(
            capability, "writer_authority_hash", authority_hash
        )
        object.__setattr__(capability, "_store_instance_id", id(self))
        self._protected_writer_registry()[id(capability)] = (
            capability,
            aggregate_type,
            namespace,
            writer_authority_id,
            authority_hash,
            id(self),
        )
        return capability

    def _protected_capability_metadata(
        self, capability: object
    ) -> dict[str, str]:
        if type(capability) is not ProtectedWriterCapability:
            raise TypeError(
                "protected writer capability must be exact "
                "ProtectedWriterCapability"
            )
        registry = self._protected_writer_registry()
        selected = registry.get(id(capability))
        if selected is None or selected[0] is not capability:
            raise ValueError(
                "protected writer capability was not issued by this JournalStore"
            )
        (
            _selected_capability,
            aggregate_type,
            namespace,
            writer_authority_id,
            writer_authority_hash,
            store_instance_id,
        ) = selected
        if store_instance_id != id(self) or capability._store_instance_id != id(self):
            raise ValueError(
                "protected writer capability belongs to a different JournalStore"
            )
        if (
            capability.aggregate_type != aggregate_type
            or capability.namespace != namespace
            or capability.writer_authority_id != writer_authority_id
            or capability.writer_authority_hash != writer_authority_hash
        ):
            raise ValueError(
                "protected writer capability scope changed after issuance"
            )
        return {
            "aggregate_type": aggregate_type,
            "namespace": namespace,
            "writer_authority_id": writer_authority_id,
            "writer_authority_hash": writer_authority_hash,
        }

    def _resolve_event_writer(
        self,
        connection: sqlite3.Connection,
        *,
        aggregate_type: str,
        capability: object | None,
    ) -> dict[str, str] | None:
        if self.SCHEMA_VERSION < 11:
            if capability is not None:
                raise RuntimeError(
                    "protected writer capability cannot be used before schema v11"
                )
            return None
        registered = connection.execute(
            """
            SELECT aggregate_type, namespace, writer_authority_id,
                   writer_authority_hash
            FROM protected_writer_namespaces
            WHERE aggregate_type = ?
            """,
            (aggregate_type,),
        ).fetchone()
        if registered is None:
            if capability is not None:
                raise ValueError(
                    "protected writer capability aggregate is not registered"
                )
            return None
        if capability is None:
            raise ValueError(
                "aggregate type requires protected writer capability"
            )
        metadata = self._protected_capability_metadata(capability)
        if (
            metadata["aggregate_type"] != aggregate_type
            or metadata["namespace"] != registered["namespace"]
            or metadata["writer_authority_id"]
            != registered["writer_authority_id"]
            or metadata["writer_authority_hash"]
            != registered["writer_authority_hash"]
        ):
            raise ValueError(
                "protected writer capability does not match durable authority"
            )
        expected_hash = _protected_writer_authority_digest(
            aggregate_type=aggregate_type,
            namespace=metadata["namespace"],
            writer_authority_id=metadata["writer_authority_id"],
        )
        if expected_hash != metadata["writer_authority_hash"]:
            raise ValueError("protected writer authority hash is invalid")
        return metadata

    @staticmethod
    def _writer_row_matches(
        row: sqlite3.Row,
        *,
        metadata: dict[str, str] | None,
    ) -> bool:
        if metadata is None:
            return all(
                row[name] is None
                for name in (
                    "writer_namespace",
                    "writer_authority_id",
                    "writer_authority_hash",
                    "writer_provenance_hash",
                )
            )
        journal_sequence = row["journal_sequence"]
        if type(journal_sequence) is not int or journal_sequence <= 0:
            return False
        expected_provenance = _event_writer_provenance_digest(
            event_id=str(row["event_id"]),
            aggregate_type=str(row["aggregate_type"]),
            aggregate_id=str(row["aggregate_id"]),
            aggregate_version=int(row["aggregate_version"]),
            envelope_hash=str(row["envelope_hash"]),
            journal_sequence=journal_sequence,
            namespace=metadata["namespace"],
            writer_authority_id=metadata["writer_authority_id"],
            writer_authority_hash=metadata["writer_authority_hash"],
        )
        return (
            row["writer_namespace"] == metadata["namespace"]
            and row["writer_authority_id"] == metadata["writer_authority_id"]
            and row["writer_authority_hash"] == metadata["writer_authority_hash"]
            and row["writer_provenance_hash"] == expected_provenance
        )

    def append_event(
        self,
        envelope: dict[str, Any],
        *,
        outbox_topic: str | None = None,
        aggregate_preconditions: tuple[ExpectedAggregateHead, ...] = (),
        expected_journal_sequence: int | None = None,
    ) -> AppendResult:
        return self._append_event(
            envelope,
            outbox_topic=outbox_topic,
            protected_writer=None,
            aggregate_preconditions=aggregate_preconditions,
            expected_journal_sequence=expected_journal_sequence,
        )

    def append_protected_event(
        self,
        capability: ProtectedWriterCapability,
        envelope: dict[str, Any],
        *,
        outbox_topic: str | None = None,
        aggregate_preconditions: tuple[ExpectedAggregateHead, ...] = (),
        expected_journal_sequence: int | None = None,
    ) -> AppendResult:
        if outbox_topic is not None:
            raise RuntimeError(
                "protected event publication requires writer-aware outbox authority"
            )
        return self._append_event(
            envelope,
            outbox_topic=None,
            protected_writer=capability,
            aggregate_preconditions=aggregate_preconditions,
            expected_journal_sequence=expected_journal_sequence,
        )

    def _append_event(
        self,
        envelope: dict[str, Any],
        *,
        outbox_topic: str | None,
        protected_writer: object | None,
        aggregate_preconditions: tuple[ExpectedAggregateHead, ...],
        expected_journal_sequence: int | None,
    ) -> AppendResult:
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
        envelope_json = canonical_json(envelope)
        envelope_hash = _event_envelope_digest(envelope_json)
        committed_at = self._require_text(envelope.get("committed_at"), "committed_at")
        if outbox_topic is not None:
            outbox_topic = self._require_text(outbox_topic, "outbox_topic")
        aggregate_preconditions = self._validated_aggregate_preconditions(
            aggregate_preconditions
        )
        if expected_journal_sequence is not None and (
            type(expected_journal_sequence) is not int
            or expected_journal_sequence < 0
        ):
            raise ValueError(
                "expected_journal_sequence must be a non-negative integer"
            )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            writer_metadata = self._resolve_event_writer(
                connection,
                aggregate_type=aggregate_type,
                capability=protected_writer,
            )
            selected_writer_authority = (
                None
                if writer_metadata is None
                else writer_metadata["writer_authority_hash"]
            )
            connection.create_function(
                "autotrade_protected_writer_authority",
                1,
                lambda candidate, expected=aggregate_type,
                       authority=selected_writer_authority: (
                    authority if candidate == expected else None
                ),
            )
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
                    and (
                        self.SCHEMA_VERSION < 5
                        or (
                            existing["envelope_json"] == envelope_json
                            and existing["envelope_hash"] == envelope_hash
                        )
                    )
                    and (
                        self.SCHEMA_VERSION < 11
                        or self._writer_row_matches(
                            existing,
                            metadata=writer_metadata,
                        )
                    )
                )
                existing_outbox = connection.execute(
                    "SELECT topic, payload_json, envelope_hash "
                    "FROM outbox WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                expected_outbox_payload = (
                    envelope_json if outbox_topic is not None else None
                )
                expected_outbox_hash = (
                    _outbox_envelope_digest(
                        outbox_topic,
                        expected_outbox_payload,
                    )
                    if expected_outbox_payload is not None
                    else None
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
                        and existing_outbox["envelope_hash"] == expected_outbox_hash
                    )
                )
                if not exact or not outbox_exact:
                    connection.rollback()
                    raise ValueError(
                        "event_id conflicts with an existing event or publication intent"
                    )
                # Idempotent replay is still an authority read. Revalidate the
                # durable aggregate/global cuts so corruption cannot be hidden
                # merely because the event payload itself matches.
                self._aggregate_version_value(
                    connection,
                    aggregate_type,
                    aggregate_id,
                )
                if self.SCHEMA_VERSION >= 6:
                    self._journal_sequence_value(connection)
                connection.commit()
                return AppendResult(event_id, aggregate_version, False)

            if expected_journal_sequence is not None:
                current_journal_sequence = self._journal_sequence_value(connection)
                if current_journal_sequence != expected_journal_sequence:
                    connection.rollback()
                    raise JournalSequencePreconditionFailed(
                        "journal sequence changed before append: "
                        f"expected {expected_journal_sequence}, "
                        f"found {current_journal_sequence}"
                    )
            self._require_aggregate_preconditions(
                connection,
                aggregate_preconditions,
            )
            current = self._aggregate_version_value(
                connection,
                aggregate_type,
                aggregate_id,
            )
            expected_version = current + 1
            if aggregate_version != expected_version:
                connection.rollback()
                raise ValueError(
                    f"aggregate_version must be {expected_version} for {aggregate_type}/{aggregate_id}"
                )

            if self.SCHEMA_VERSION >= 11:
                journal_sequence = self._journal_sequence_value(connection) + 1
                if writer_metadata is None:
                    writer_namespace = None
                    writer_authority_id = None
                    writer_authority_hash = None
                    writer_provenance_hash = None
                else:
                    writer_namespace = writer_metadata["namespace"]
                    writer_authority_id = writer_metadata["writer_authority_id"]
                    writer_authority_hash = writer_metadata["writer_authority_hash"]
                    writer_provenance_hash = _event_writer_provenance_digest(
                        event_id=event_id,
                        aggregate_type=aggregate_type,
                        aggregate_id=aggregate_id,
                        aggregate_version=aggregate_version,
                        envelope_hash=envelope_hash,
                        journal_sequence=journal_sequence,
                        namespace=writer_namespace,
                        writer_authority_id=writer_authority_id,
                        writer_authority_hash=writer_authority_hash,
                    )
                connection.execute(
                    """
                    INSERT INTO events(
                        event_id, event_type, aggregate_type, aggregate_id,
                        aggregate_version, payload_json, payload_hash, committed_at,
                        envelope_json, envelope_hash, journal_sequence,
                        writer_namespace, writer_authority_id,
                        writer_authority_hash, writer_provenance_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        envelope_json,
                        envelope_hash,
                        journal_sequence,
                        writer_namespace,
                        writer_authority_id,
                        writer_authority_hash,
                        writer_provenance_hash,
                    ),
                )
            elif self.SCHEMA_VERSION >= 6:
                journal_sequence = self._journal_sequence_value(connection) + 1
                connection.execute(
                    """
                    INSERT INTO events(
                        event_id, event_type, aggregate_type, aggregate_id,
                        aggregate_version, payload_json, payload_hash, committed_at,
                        envelope_json, envelope_hash, journal_sequence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        envelope_json,
                        envelope_hash,
                        journal_sequence,
                    ),
                )
            elif self.SCHEMA_VERSION >= 5:
                connection.execute(
                    """
                    INSERT INTO events(
                        event_id, event_type, aggregate_type, aggregate_id,
                        aggregate_version, payload_json, payload_hash, committed_at,
                        envelope_json, envelope_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        envelope_json,
                        envelope_hash,
                    ),
                )
            else:
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
                outbox_payload = envelope_json
                outbox_id = "outbox-" + sha256(event_id.encode("utf-8")).hexdigest()[:32]
                if self.SCHEMA_VERSION >= 4:
                    outbox_hash = _outbox_envelope_digest(
                        outbox_topic,
                        outbox_payload,
                    )
                    connection.execute(
                        """
                        INSERT INTO outbox(
                            outbox_id, event_id, topic, payload_json,
                            created_at, envelope_hash
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            outbox_id,
                            event_id,
                            outbox_topic,
                            outbox_payload,
                            self._now(),
                            outbox_hash,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO outbox(
                            outbox_id, event_id, topic, payload_json, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            outbox_id,
                            event_id,
                            outbox_topic,
                            outbox_payload,
                            self._now(),
                        ),
                    )
            connection.commit()
        return AppendResult(event_id, aggregate_version, True)

    def load_protected_events(
        self,
        capability: ProtectedWriterCapability,
        aggregate_id: str,
    ) -> list[dict[str, Any]]:
        """Load issuer-qualified rows only through the selected writer."""

        metadata = self._protected_capability_metadata(capability)
        aggregate_id = self._require_text(aggregate_id, "aggregate_id")
        with self._connect() as connection:
            connection.execute("BEGIN")
            registered = self._resolve_event_writer(
                connection,
                aggregate_type=metadata["aggregate_type"],
                capability=capability,
            )
            assert registered is not None
            rows = connection.execute(
                """
                SELECT event_id, event_type, aggregate_type, aggregate_id,
                       aggregate_version, payload_json, payload_hash, committed_at,
                       envelope_json, envelope_hash, journal_sequence,
                       writer_namespace, writer_authority_id,
                       writer_authority_hash, writer_provenance_hash
                FROM events
                WHERE aggregate_type = ? AND aggregate_id = ?
                ORDER BY aggregate_version
                """,
                (metadata["aggregate_type"], aggregate_id),
            ).fetchall()
            decoded: list[dict[str, Any]] = []
            for row in rows:
                if not self._writer_row_matches(row, metadata=registered):
                    raise ValueError(
                        "protected journal event lacks selected writer provenance"
                    )
                decoded.append(self._decode_event_row(row))
            expected_version = 0
            last_sequence = 0
            for event in decoded:
                expected_version += 1
                if event["aggregate_version"] != expected_version:
                    connection.rollback()
                    raise ValueError("protected journal aggregate version gap")
                sequence = event.get("journal_sequence")
                if type(sequence) is not int or sequence <= last_sequence:
                    connection.rollback()
                    raise ValueError(
                        "protected journal sequence authority is invalid"
                    )
                last_sequence = sequence
            self._journal_sequence_value(connection)
            connection.commit()
            return decoded

    def load_events(self, aggregate_type: str, aggregate_id: str) -> list[dict[str, Any]]:
        aggregate_type = self._require_text(aggregate_type, "aggregate_type")
        aggregate_id = self._require_text(aggregate_id, "aggregate_id")
        envelope_columns = (
            ", envelope_json, envelope_hash, journal_sequence"
            if self.SCHEMA_VERSION >= 6
            else (
                ", envelope_json, envelope_hash"
                if self.SCHEMA_VERSION >= 5
                else ""
            )
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT event_id, event_type, aggregate_type, aggregate_id,
                       aggregate_version, payload_json, payload_hash, committed_at
                       {envelope_columns}
                FROM events
                WHERE aggregate_type = ? AND aggregate_id = ?
                ORDER BY aggregate_version
                """,
                (aggregate_type, aggregate_id),
            ).fetchall()
        return [self._decode_event_row(row) for row in rows]

    def load_events_by_aggregate_type(
        self, aggregate_type: str
    ) -> list[dict[str, Any]]:
        """Load one aggregate type in durable append order.

        This is a read-only journal primitive for authorities that must detect
        superseding facts across multiple aggregate identities. Event integrity
        is still verified by _decode_event_row(); callers remain responsible for
        validating semantic scope before treating any row as authority.
        """

        aggregate_type = self._require_text(aggregate_type, "aggregate_type")
        order_column = (
            "journal_sequence" if self.SCHEMA_VERSION >= 6 else "rowid"
        )
        envelope_columns = (
            ", envelope_json, envelope_hash, journal_sequence"
            if self.SCHEMA_VERSION >= 6
            else (
                ", envelope_json, envelope_hash"
                if self.SCHEMA_VERSION >= 5
                else ""
            )
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT event_id, event_type, aggregate_type, aggregate_id,
                       aggregate_version, payload_json, payload_hash, committed_at
                       {envelope_columns}
                FROM events
                WHERE aggregate_type = ?
                ORDER BY {order_column}
                """,
                (aggregate_type,),
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
        state_hash = (
            _projection_checkpoint_digest(
                projection_name=projection_name,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                aggregate_version=aggregate_version,
                state=state,
            )
            if self.SCHEMA_VERSION >= 8
            else payload_digest(state)
        )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
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
                    existing_version = existing["aggregate_version"]
                    if type(existing_version) is not int or existing_version < 0:
                        raise ValueError(
                            "projection checkpoint aggregate_version is not a canonical integer"
                        )
                    try:
                        existing_state = json.loads(existing["state_json"])
                    except (json.JSONDecodeError, TypeError) as error:
                        raise ValueError(
                            "projection checkpoint state is not valid JSON"
                        ) from error
                    if canonical_json(existing_state) != existing["state_json"]:
                        raise ValueError(
                            "projection checkpoint state is not canonical JSON"
                        )
                    existing_hash = (
                        _projection_checkpoint_digest(
                            projection_name=projection_name,
                            aggregate_type=aggregate_type,
                            aggregate_id=aggregate_id,
                            aggregate_version=existing_version,
                            state=existing_state,
                        )
                        if self.SCHEMA_VERSION >= 8
                        else payload_digest(existing_state)
                    )
                    if existing_hash != existing["state_hash"]:
                        raise ValueError(
                            "projection checkpoint hash does not match identity, version, and state"
                        )

                journal_version = self._aggregate_version_value(
                    connection,
                    aggregate_type,
                    aggregate_id,
                )
                if aggregate_version > journal_version:
                    raise ValueError("projection checkpoint cannot outrun the journal")

                if existing is not None:
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
            journal_version = self._aggregate_version_value(
                connection,
                aggregate_type,
                aggregate_id,
            )

        try:
            state = json.loads(row["state_json"])
        except (json.JSONDecodeError, TypeError) as error:
            raise ValueError(
                "projection checkpoint state is not valid JSON"
            ) from error
        if canonical_json(state) != row["state_json"]:
            raise ValueError(
                "projection checkpoint state is not canonical JSON"
            )
        aggregate_version = row["aggregate_version"]
        if type(aggregate_version) is not int or aggregate_version < 0:
            raise ValueError(
                "projection checkpoint aggregate_version is not a canonical integer"
            )
        expected_hash = (
            _projection_checkpoint_digest(
                projection_name=projection_name,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                aggregate_version=aggregate_version,
                state=state,
            )
            if self.SCHEMA_VERSION >= 8
            else payload_digest(state)
        )
        if expected_hash != row["state_hash"]:
            raise ValueError(
                "projection checkpoint hash does not match identity, version, and state"
            )
        if aggregate_version > journal_version:
            raise ValueError("projection checkpoint is ahead of the journal")
        return {
            "projection_name": projection_name,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "aggregate_version": aggregate_version,
            "state": state,
            "state_hash": row["state_hash"],
            "updated_at": row["updated_at"],
        }

    def save_global_projection_checkpoint(
        self,
        *,
        projection_name: str,
        journal_sequence: int,
        state: Any,
    ) -> bool:
        """Persist derived multi-aggregate state at one exact durable journal cut."""

        projection_name = self._require_text(projection_name, "projection_name")
        if type(journal_sequence) is not int or journal_sequence < 0:
            raise ValueError("journal_sequence must be a non-negative integer")
        state_json = canonical_json(state)
        state_hash = payload_digest(
            {
                "projection_name": projection_name,
                "journal_sequence": journal_sequence,
                "state": state,
            }
        )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._journal_sequence_value(connection)
                if journal_sequence > current:
                    raise ValueError(
                        "global projection checkpoint cannot outrun the journal"
                    )
                existing = connection.execute(
                    """
                    SELECT journal_sequence, state_json, state_hash
                    FROM global_projection_checkpoints
                    WHERE projection_name = ?
                    """,
                    (projection_name,),
                ).fetchone()
                if existing is not None:
                    existing_sequence = existing["journal_sequence"]
                    if type(existing_sequence) is not int or existing_sequence < 0:
                        raise ValueError(
                            "global projection checkpoint journal_sequence "
                            "is not a canonical integer"
                        )
                    try:
                        existing_state = json.loads(existing["state_json"])
                    except (json.JSONDecodeError, TypeError) as error:
                        raise ValueError(
                            "global projection checkpoint state is not valid JSON"
                        ) from error
                    if canonical_json(existing_state) != existing["state_json"]:
                        raise ValueError(
                            "global projection checkpoint state is not canonical JSON"
                        )
                    existing_hash = payload_digest(
                        {
                            "projection_name": projection_name,
                            "journal_sequence": existing_sequence,
                            "state": existing_state,
                        }
                    )
                    if existing_hash != existing["state_hash"]:
                        raise ValueError(
                            "global projection checkpoint hash does not match "
                            "identity, cut, and state"
                        )
                    exact = (
                        existing_sequence == journal_sequence
                        and existing["state_json"] == state_json
                        and existing["state_hash"] == state_hash
                    )
                    if exact:
                        connection.commit()
                        return False
                    if journal_sequence <= existing_sequence:
                        raise ValueError(
                            "global projection checkpoint cannot regress or change at the same journal cut"
                        )

                connection.execute(
                    """
                    INSERT INTO global_projection_checkpoints(
                        projection_name, journal_sequence,
                        state_json, state_hash, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(projection_name)
                    DO UPDATE SET
                        journal_sequence = excluded.journal_sequence,
                        state_json = excluded.state_json,
                        state_hash = excluded.state_hash,
                        updated_at = excluded.updated_at
                    """,
                    (
                        projection_name,
                        journal_sequence,
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

    def load_global_projection_checkpoint(
        self,
        *,
        projection_name: str,
    ) -> dict[str, Any] | None:
        """Load and integrity-check a multi-aggregate projection checkpoint."""

        projection_name = self._require_text(projection_name, "projection_name")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT journal_sequence, state_json, state_hash, updated_at
                FROM global_projection_checkpoints
                WHERE projection_name = ?
                """,
                (projection_name,),
            ).fetchone()
            if row is None:
                return None
            current = self._journal_sequence_value(connection)

        try:
            state = json.loads(row["state_json"])
        except (json.JSONDecodeError, TypeError) as error:
            raise ValueError(
                "global projection checkpoint state is not valid JSON"
            ) from error
        if canonical_json(state) != row["state_json"]:
            raise ValueError(
                "global projection checkpoint state is not canonical JSON"
            )
        journal_sequence = row["journal_sequence"]
        if type(journal_sequence) is not int or journal_sequence < 0:
            raise ValueError(
                "global projection checkpoint journal_sequence "
                "is not a canonical integer"
            )
        expected_hash = payload_digest(
            {
                "projection_name": projection_name,
                "journal_sequence": journal_sequence,
                "state": state,
            }
        )
        if expected_hash != row["state_hash"]:
            raise ValueError(
                "global projection checkpoint hash does not match identity, cut, and state"
            )
        if journal_sequence > current:
            raise ValueError(
                "global projection checkpoint is ahead of the journal"
            )
        return {
            "projection_name": projection_name,
            "journal_sequence": journal_sequence,
            "state": state,
            "state_hash": row["state_hash"],
            "updated_at": row["updated_at"],
        }

    def pending_outbox_count(self) -> int:
        """Return the complete durable undelivered outbox cardinality.

        This read-only count is intentionally unbounded so recovery/performance
        qualification cannot mistake a page limit for the actual reconnect
        backlog.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS pending_count FROM outbox WHERE delivered_at IS NULL"
            ).fetchone()
        if row is None:
            raise RuntimeError("pending outbox count query returned no row")
        return int(row["pending_count"])

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
                    outbox.envelope_hash,
                    events.event_type,
                    events.aggregate_type,
                    events.aggregate_id,
                    events.aggregate_version,
                    events.payload_json AS event_payload_json,
                    events.payload_hash,
                    events.committed_at,
                    events.envelope_json AS event_envelope_json,
                    events.envelope_hash AS event_envelope_hash
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
            raw_outbox_payload = str(row["outbox_payload_json"])
            actual_outbox_hash = _outbox_envelope_digest(
                str(row["topic"]),
                raw_outbox_payload,
            )
            if row["envelope_hash"] != actual_outbox_hash:
                raise ValueError(
                    "outbox envelope hash does not match stored payload"
                )
            event_row = {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "aggregate_type": row["aggregate_type"],
                "aggregate_id": row["aggregate_id"],
                "aggregate_version": row["aggregate_version"],
                "payload_json": row["event_payload_json"],
                "payload_hash": row["payload_hash"],
                "committed_at": row["committed_at"],
                "envelope_json": row["event_envelope_json"],
                "envelope_hash": row["event_envelope_hash"],
            }
            event = self._decode_event_row(event_row)
            try:
                outbox_payload = json.loads(raw_outbox_payload)
            except (json.JSONDecodeError, TypeError) as error:
                raise ValueError("outbox payload is not valid JSON") from error
            if canonical_json(outbox_payload) != raw_outbox_payload:
                raise ValueError("outbox payload is not canonical JSON")
            raw_authoritative_envelope = row["event_envelope_json"]
            if not isinstance(raw_authoritative_envelope, str):
                raise ValueError("journal event envelope authority is missing")
            try:
                authoritative_envelope = json.loads(raw_authoritative_envelope)
            except (json.JSONDecodeError, TypeError) as error:
                raise ValueError("journal event envelope is not valid JSON") from error
            if canonical_json(authoritative_envelope) != raw_authoritative_envelope:
                raise ValueError("journal event envelope is not canonical JSON")
            core_envelope = {
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "aggregate_type": event["aggregate_type"],
                "aggregate_id": event["aggregate_id"],
                "aggregate_version": str(event["aggregate_version"]),
                "payload": event["payload"],
                "payload_hash": event["payload_hash"],
                "committed_at": event["committed_at"],
            }
            for key, expected in core_envelope.items():
                if authoritative_envelope.get(key) != expected:
                    raise ValueError(
                        "journal event envelope conflicts with core journal event"
                    )
            if raw_outbox_payload != raw_authoritative_envelope:
                raise ValueError(
                    "outbox payload does not match authoritative journal event envelope"
                )
            pending.append(
                {
                    "outbox_id": row["outbox_id"],
                    "event_id": row["event_id"],
                    "topic": row["topic"],
                    "payload": outbox_payload,
                    "created_at": row["created_at"],
                    "envelope_hash": row["envelope_hash"],
                }
            )
        return pending

    def mark_outbox_delivered(
        self,
        outbox_id: str,
        *,
        expected_envelope_hash: str,
    ) -> bool:
        """Acknowledge exactly the verified outbox envelope that was delivered.

        The caller must echo the envelope hash returned by pending_outbox().
        Revalidate both the outbox bytes and their authoritative journal event
        under the same write transaction before marking delivery. A stale
        worker therefore cannot acknowledge a row that changed after it read
        the pending publication intent.
        """

        outbox_id = self._require_text(outbox_id, "outbox_id")
        expected_envelope_hash = self._require_text(
            expected_envelope_hash,
            "expected_envelope_hash",
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT
                        outbox.event_id,
                        outbox.topic,
                        outbox.payload_json AS outbox_payload_json,
                        outbox.envelope_hash,
                        outbox.delivered_at,
                        events.event_type,
                        events.aggregate_type,
                        events.aggregate_id,
                        events.aggregate_version,
                        events.payload_json AS event_payload_json,
                        events.payload_hash,
                        events.committed_at,
                        events.envelope_json AS event_envelope_json,
                        events.envelope_hash AS event_envelope_hash
                    FROM outbox
                    JOIN events ON events.event_id = outbox.event_id
                    WHERE outbox.outbox_id = ?
                    """,
                    (outbox_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(outbox_id)

                raw_outbox_payload = str(row["outbox_payload_json"])
                actual_outbox_hash = _outbox_envelope_digest(
                    str(row["topic"]),
                    raw_outbox_payload,
                )
                if row["envelope_hash"] != actual_outbox_hash:
                    raise ValueError(
                        "outbox envelope hash does not match stored payload"
                    )
                if row["envelope_hash"] != expected_envelope_hash:
                    raise ValueError(
                        "outbox delivery acknowledgement is stale"
                    )

                event_row = {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "aggregate_type": row["aggregate_type"],
                    "aggregate_id": row["aggregate_id"],
                    "aggregate_version": row["aggregate_version"],
                    "payload_json": row["event_payload_json"],
                    "payload_hash": row["payload_hash"],
                    "committed_at": row["committed_at"],
                    "envelope_json": row["event_envelope_json"],
                    "envelope_hash": row["event_envelope_hash"],
                }
                event = self._decode_event_row(event_row)
                try:
                    outbox_payload = json.loads(raw_outbox_payload)
                except (json.JSONDecodeError, TypeError) as error:
                    raise ValueError("outbox payload is not valid JSON") from error
                if canonical_json(outbox_payload) != raw_outbox_payload:
                    raise ValueError("outbox payload is not canonical JSON")
                raw_authoritative_envelope = row["event_envelope_json"]
                if not isinstance(raw_authoritative_envelope, str):
                    raise ValueError("journal event envelope authority is missing")
                try:
                    authoritative_envelope = json.loads(raw_authoritative_envelope)
                except (json.JSONDecodeError, TypeError) as error:
                    raise ValueError("journal event envelope is not valid JSON") from error
                if canonical_json(authoritative_envelope) != raw_authoritative_envelope:
                    raise ValueError("journal event envelope is not canonical JSON")
                core_envelope = {
                    "event_id": event["event_id"],
                    "event_type": event["event_type"],
                    "aggregate_type": event["aggregate_type"],
                    "aggregate_id": event["aggregate_id"],
                    "aggregate_version": str(event["aggregate_version"]),
                    "payload": event["payload"],
                    "payload_hash": event["payload_hash"],
                    "committed_at": event["committed_at"],
                }
                for key, expected in core_envelope.items():
                    if authoritative_envelope.get(key) != expected:
                        raise ValueError(
                            "journal event envelope conflicts with core journal event"
                        )
                if raw_outbox_payload != raw_authoritative_envelope:
                    raise ValueError(
                        "outbox payload does not match authoritative journal event envelope"
                    )

                if row["delivered_at"] is not None:
                    connection.commit()
                    return False

                updated = connection.execute(
                    """
                    UPDATE outbox
                    SET delivered_at = ?
                    WHERE outbox_id = ?
                      AND delivered_at IS NULL
                      AND envelope_hash = ?
                    """,
                    (self._now(), outbox_id, expected_envelope_hash),
                )
                if updated.rowcount != 1:
                    raise ValueError(
                        "outbox delivery state changed before acknowledgement"
                    )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

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

    @staticmethod
    def _decode_command_result(row: sqlite3.Row) -> Any:
        result_json = str(row["result_json"])
        expected_hash = (
            "sha256:" + sha256(result_json.encode("utf-8")).hexdigest()
        )
        if row["result_hash"] != expected_hash:
            raise ValueError("command result hash does not match stored result")
        try:
            decoded = json.loads(result_json)
        except (json.JSONDecodeError, TypeError) as error:
            raise ValueError("command result is not valid JSON") from error
        if canonical_json(decoded) != result_json:
            raise ValueError("command result is not canonical JSON")
        return decoded

    @staticmethod
    def _require_command_effect_kind(row: sqlite3.Row, expected: str) -> None:
        kind = row["effect_kind"]
        effect_json = row["effect_json"]
        effect_hash = row["effect_hash"]
        if kind == "LEGACY_UNKNOWN":
            if effect_json is not None or effect_hash is not None:
                raise ValueError("legacy command effect carries invented effect authority")
            raise ValueError(
                "legacy command history has ambiguous transactional effect; "
                "use a new idempotency key"
            )
        if kind == "RESULT_ONLY":
            if effect_json is not None or effect_hash is not None:
                raise ValueError("result-only command carries unexpected effect authority")
        elif kind == "EVENT_BATCH":
            if not isinstance(effect_json, str) or not isinstance(effect_hash, str):
                raise ValueError("command event-batch effect authority is missing")
        else:
            raise ValueError("command effect kind is invalid")
        if kind != expected:
            raise ValueError(
                "idempotency_key belongs to a different command effect kind"
            )

    @staticmethod
    def _event_batch_effect(
        prepared: list[dict[str, Any]],
        journal_sequences: list[int],
    ) -> tuple[str, str]:
        if len(journal_sequences) != len(prepared):
            raise ValueError("command event-batch journal sequence cardinality mismatch")
        effect = {
            "events": [
                {
                    "event_id": item["event_id"],
                    "aggregate_type": item["aggregate_type"],
                    "aggregate_id": item["aggregate_id"],
                    "aggregate_version": item["aggregate_version"],
                    "journal_sequence": journal_sequence,
                    "envelope_hash": item["envelope_hash"],
                    "outbox_topic": item["outbox_topic"],
                    "outbox_hash": (
                        _outbox_envelope_digest(
                            item["outbox_topic"],
                            item["outbox_payload"],
                        )
                        if item["outbox_topic"] is not None
                        else None
                    ),
                }
                for item, journal_sequence in zip(
                    prepared,
                    journal_sequences,
                    strict=True,
                )
            ]
        }
        effect_json = canonical_json(effect)
        return effect_json, (
            "sha256:" + sha256(effect_json.encode("utf-8")).hexdigest()
        )

    @staticmethod
    def _stored_effect_journal_sequences(
        effect_json: str,
        effect_hash: str,
    ) -> list[int]:
        expected_hash = "sha256:" + sha256(effect_json.encode("utf-8")).hexdigest()
        if effect_hash != expected_hash:
            raise ValueError("command effect hash does not match stored effect")
        try:
            effect = json.loads(effect_json)
        except (json.JSONDecodeError, TypeError) as error:
            raise ValueError("command effect is not valid JSON") from error
        if canonical_json(effect) != effect_json:
            raise ValueError("command effect is not canonical JSON")
        rows = effect.get("events") if isinstance(effect, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ValueError("command event-batch effect is empty or invalid")
        sequences = [row.get("journal_sequence") if isinstance(row, dict) else None for row in rows]
        if any(type(value) is not int or value < 1 for value in sequences):
            raise ValueError("command event-batch journal sequence is missing or invalid")
        if len(set(sequences)) != len(sequences):
            raise ValueError("command event-batch journal sequences are duplicated")
        return sequences

    def _verify_stored_event_batch_effect(
        self,
        connection: sqlite3.Connection,
        *,
        effect_json: str,
        effect_hash: str,
    ) -> None:
        expected_hash = "sha256:" + sha256(effect_json.encode("utf-8")).hexdigest()
        if effect_hash != expected_hash:
            raise ValueError("command effect hash does not match stored effect")
        try:
            effect = json.loads(effect_json)
        except (json.JSONDecodeError, TypeError) as error:
            raise ValueError("command effect is not valid JSON") from error
        if canonical_json(effect) != effect_json:
            raise ValueError("command effect is not canonical JSON")
        if not isinstance(effect, dict) or set(effect) != {"events"}:
            raise ValueError("command event-batch effect shape is invalid")
        rows = effect["events"]
        if not isinstance(rows, list) or not rows:
            raise ValueError("command event-batch effect is empty or invalid")
        for descriptor in rows:
            if not isinstance(descriptor, dict) or set(descriptor) != {
                "event_id", "aggregate_type", "aggregate_id", "aggregate_version",
                "journal_sequence", "envelope_hash", "outbox_topic", "outbox_hash",
            }:
                raise ValueError("command event-batch descriptor is invalid")
            event_row = connection.execute(
                """
                SELECT event_id, event_type, aggregate_type, aggregate_id,
                       aggregate_version, payload_json, payload_hash, committed_at,
                       envelope_json, envelope_hash, journal_sequence
                FROM events
                WHERE event_id = ?
                """,
                (descriptor["event_id"],),
            ).fetchone()
            if event_row is None:
                raise ValueError("command event-batch event is missing")
            # Verify the complete canonical event/core binding before trusting
            # the narrower command-effect descriptor. This catches corruption
            # of event type, payload/hash, committed time, envelope canonicality,
            # or global sequence even when aggregate identity still looks valid.
            self._decode_event_row(event_row)
            if (
                event_row["aggregate_type"] != descriptor["aggregate_type"]
                or event_row["aggregate_id"] != descriptor["aggregate_id"]
                or event_row["aggregate_version"] != descriptor["aggregate_version"]
                or event_row["journal_sequence"] != descriptor["journal_sequence"]
                or event_row["envelope_hash"] != descriptor["envelope_hash"]
            ):
                raise ValueError("command event-batch event identity changed")
            raw_envelope = event_row["envelope_json"]
            if not isinstance(raw_envelope, str):
                raise ValueError("command event-batch envelope is missing")
            if _event_envelope_digest(raw_envelope) != descriptor["envelope_hash"]:
                raise ValueError("command event-batch envelope hash changed")
            outbox = connection.execute(
                "SELECT topic, payload_json, envelope_hash FROM outbox WHERE event_id = ?",
                (descriptor["event_id"],),
            ).fetchone()
            if descriptor["outbox_topic"] is None:
                if outbox is not None:
                    raise ValueError("command event-batch publication intent changed")
            else:
                if outbox is None:
                    raise ValueError("command event-batch publication intent is missing")
                if (
                    outbox["topic"] != descriptor["outbox_topic"]
                    or outbox["payload_json"] != raw_envelope
                    or outbox["envelope_hash"] != descriptor["outbox_hash"]
                    or _outbox_envelope_digest(
                        str(outbox["topic"]), str(outbox["payload_json"])
                    ) != descriptor["outbox_hash"]
                ):
                    raise ValueError("command event-batch publication intent changed")

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
        result_hash = (
            "sha256:" + sha256(result_json.encode("utf-8")).hexdigest()
        )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._reject_legacy_unscoped_key(connection, idempotency_key)
            existing = connection.execute(
                "SELECT * FROM command_dedupe "
                "WHERE actor = ? AND environment = ? AND idempotency_key = ?",
                (actor, environment, idempotency_key),
            ).fetchone()
            if existing is not None:
                saved_result = self._decode_command_result(existing)
                if existing["request_hash"] != request_hash:
                    connection.rollback()
                    raise ValueError("idempotency_key was already used for a different request")
                self._require_command_effect_kind(existing, "RESULT_ONLY")
                connection.commit()
                return saved_result, False
            if connection.execute(
                "SELECT 1 FROM command_dedupe WHERE command_id = ?", (command_id,)
            ).fetchone() is not None:
                connection.rollback()
                raise ValueError("command_id already exists with another idempotency key")
            connection.execute(
                """
                INSERT INTO command_dedupe(
                    command_id, actor, environment, idempotency_key,
                    request_hash, result_json, result_hash, state_version, created_at,
                    effect_kind, effect_json, effect_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command_id, actor, environment, idempotency_key,
                    request_hash, result_json, result_hash, state_version, self._now(),
                    "RESULT_ONLY", None, None,
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
        expected_journal_sequence: int | None = None,
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
        if (
            expected_journal_sequence is not None
            and (
                type(expected_journal_sequence) is not int
                or expected_journal_sequence < 0
            )
        ):
            raise ValueError(
                "expected_journal_sequence must be a non-negative integer"
            )

        request_hash = payload_digest(request)
        result_json = canonical_json(result)
        result_hash = (
            "sha256:" + sha256(result_json.encode("utf-8")).hexdigest()
        )
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
                    "envelope_json": canonical_json(envelope),
                    "envelope_hash": _event_envelope_digest(canonical_json(envelope)),
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
                    saved_result = self._decode_command_result(existing)
                    if existing["request_hash"] != request_hash:
                        raise ValueError(
                            "idempotency_key was already used for a different request"
                        )
                    self._require_command_effect_kind(existing, "EVENT_BATCH")
                    stored_effect_json = existing["effect_json"]
                    stored_effect_hash = existing["effect_hash"]
                    if not isinstance(stored_effect_json, str) or not isinstance(
                        stored_effect_hash, str
                    ):
                        raise ValueError("command event-batch effect authority is missing")
                    stored_sequences = self._stored_effect_journal_sequences(
                        stored_effect_json,
                        stored_effect_hash,
                    )
                    retry_effect_json, retry_effect_hash = self._event_batch_effect(
                        prepared,
                        stored_sequences,
                    )
                    if (
                        stored_effect_json != retry_effect_json
                        or stored_effect_hash != retry_effect_hash
                    ):
                        raise ValueError(
                            "idempotency_key retry event batch differs from original effect"
                        )
                    self._verify_stored_event_batch_effect(
                        connection,
                        effect_json=stored_effect_json,
                        effect_hash=stored_effect_hash,
                    )
                    if self.SCHEMA_VERSION >= 6:
                        self._journal_sequence_value(connection)
                    validated_aggregates: set[tuple[str, str]] = set()
                    for item in prepared:
                        aggregate_key = (
                            item["aggregate_type"],
                            item["aggregate_id"],
                        )
                        if aggregate_key in validated_aggregates:
                            continue
                        self._aggregate_version_value(
                            connection,
                            aggregate_key[0],
                            aggregate_key[1],
                        )
                        validated_aggregates.add(aggregate_key)
                    connection.commit()
                    return saved_result, False, ()

                if connection.execute(
                    "SELECT 1 FROM command_dedupe WHERE command_id = ?",
                    (command_id,),
                ).fetchone() is not None:
                    raise ValueError("command_id already exists with another idempotency key")

                journal_sequence_cut = self._journal_sequence_value(connection)
                if (
                    expected_journal_sequence is not None
                    and journal_sequence_cut != expected_journal_sequence
                ):
                    raise ValueError(
                        "journal sequence changed after financial evidence validation"
                    )
                batch_journal_sequences = list(
                    range(
                        journal_sequence_cut + 1,
                        journal_sequence_cut + len(prepared) + 1,
                    )
                )
                effect_json, effect_hash = self._event_batch_effect(
                    prepared,
                    batch_journal_sequences,
                )

                next_versions: dict[tuple[str, str], int] = {}
                for item in prepared:
                    if connection.execute(
                        "SELECT 1 FROM events WHERE event_id = ?",
                        (item["event_id"],),
                    ).fetchone() is not None:
                        raise ValueError("event_id already exists for another command")
                    key = (item["aggregate_type"], item["aggregate_id"])
                    if key not in next_versions:
                        current = self._aggregate_version_value(
                            connection,
                            key[0],
                            key[1],
                        )
                        next_versions[key] = current + 1
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
                        request_hash, result_json, result_hash,
                        state_version, created_at, effect_kind, effect_json, effect_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        command_id,
                        actor,
                        environment,
                        idempotency_key,
                        request_hash,
                        result_json,
                        result_hash,
                        state_version,
                        self._now(),
                        "EVENT_BATCH",
                        effect_json,
                        effect_hash,
                    ),
                )

                appended: list[AppendResult] = []
                next_journal_sequence = (
                    journal_sequence_cut + 1
                    if self.SCHEMA_VERSION >= 6
                    else None
                )
                for item in prepared:
                    if self.SCHEMA_VERSION >= 6:
                        connection.execute(
                            """
                            INSERT INTO events(
                                event_id, event_type, aggregate_type, aggregate_id,
                                aggregate_version, payload_json, payload_hash,
                                committed_at, envelope_json, envelope_hash,
                                journal_sequence
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                                item["envelope_json"],
                                item["envelope_hash"],
                                next_journal_sequence,
                            ),
                        )
                        assert next_journal_sequence is not None
                        next_journal_sequence += 1
                    elif self.SCHEMA_VERSION >= 5:
                        connection.execute(
                            """
                            INSERT INTO events(
                                event_id, event_type, aggregate_type, aggregate_id,
                                aggregate_version, payload_json, payload_hash,
                                committed_at, envelope_json, envelope_hash
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                                item["envelope_json"],
                                item["envelope_hash"],
                            ),
                        )
                    else:
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
                        outbox_hash = _outbox_envelope_digest(
                            item["outbox_topic"],
                            item["outbox_payload"],
                        )
                        connection.execute(
                            """
                            INSERT INTO outbox(
                                outbox_id, event_id, topic, payload_json,
                                created_at, envelope_hash
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                outbox_id,
                                item["event_id"],
                                item["outbox_topic"],
                                item["outbox_payload"],
                                self._now(),
                                outbox_hash,
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
