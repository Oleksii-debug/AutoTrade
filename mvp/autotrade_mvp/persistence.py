from __future__ import annotations

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

from autotrade_foundation.local_filesystem import (
    LocalFilesystemQualificationError,
    freeze_local_filesystem_path,
    require_qualified_local_filesystem_path,
)

# Keep the established persistence implementation byte-for-byte behind this
# compatibility facade. JournalStore alone adds filesystem authority fencing;
# all existing public persistence helpers/classes continue to resolve here.
from . import _persistence_impl as _impl
from ._persistence_impl import *  # noqa: F401,F403
from ._persistence_impl import JournalStore as _JournalStoreImpl
from .store_identity import (
    JournalStoreIdentity,
    connection_main_identity,
    establish_database_anchor,
    freeze_database_path,
    guard_windows_database_authority,
    require_database_identity,
    require_exact_journal_store_identity,
    same_journal_backing_object,
)

_JOURNAL_OPERATION_AUTHORITY: ContextVar[
    tuple[int, JournalStoreIdentity] | None
] = ContextVar("autotrade_journal_operation_authority", default=None)



def __getattr__(name: str):
    """Preserve legacy module-level access, including private test helpers."""

    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))


class JournalStore(_JournalStoreImpl):
    """JournalStore with immutable canonical backing-file authority.

    POSIX retains canonical path/device/inode rebinding checks. On the supported
    Windows product runtime every SQLite open is additionally enclosed by native
    ancestor-namespace and final-file handles held without FILE_SHARE_DELETE,
    and store identity comes from that opened file handle.
    """

    def __init__(self, path: str | Path):
        # Freeze caller-relative text before locality admission performs Win32
        # I/O. A concurrent process-wide chdir after admission must not retarget
        # durable financial state into an unclassified namespace.
        frozen_path = freeze_local_filesystem_path(Path(path).expanduser())
        try:
            require_qualified_local_filesystem_path(frozen_path)
        except LocalFilesystemQualificationError as error:
            raise RuntimeError(
                "journal database path must be on a qualified local filesystem"
            ) from error
        self.path = freeze_database_path(frozen_path)
        self._store_identity: JournalStoreIdentity | None = None
        self._initialize()
        if self._store_identity is None:
            raise RuntimeError("journal store identity was not established")

    @classmethod
    def _migration_statements(cls, version: int) -> tuple[str, ...]:
        """Preserve migration authority while invalidating all v7 checkpoints."""

        statements = super()._migration_statements(version)
        if version != 8:
            return statements
        global_invalidation = "DELETE FROM global_projection_checkpoints"
        if global_invalidation in statements:
            return statements
        return (*statements, global_invalidation)

    @property
    def store_identity(self) -> JournalStoreIdentity:
        state = _require_exact_journal_store_state(self, subject="canonical JournalStore")
        if "_store_identity" not in state:
            raise RuntimeError("journal store identity is not established")
        identity = state["_store_identity"]
        if identity is None:
            raise RuntimeError("journal store identity is not established")
        identity = require_exact_journal_store_identity(identity)
        _require_exact_journal_store_path(
            self, identity=identity, state=state, subject="canonical JournalStore"
        )
        return identity

    def claim_first_event(self, envelope: dict[str, Any]) -> _impl.AppendResult:
        """Atomically establish the first whole-store durable business authority.

        The claim succeeds only when the event journal, outbox, command dedupe,
        and both projection-checkpoint stores are empty in the same
        BEGIN IMMEDIATE transaction that writes global journal sequence 1.
        Schema metadata is excluded because migrations describe storage shape,
        not prior business authority.
        """

        identity = require_exact_journal_store_authority(
            self,
            subject="first-event claim store",
        )
        if type(envelope) is not dict:
            raise TypeError("envelope must be an exact object")

        event_id = JournalStore._require_text(
            self, envelope.get("event_id"), "event_id"
        )
        event_type = JournalStore._require_text(
            self, envelope.get("event_type"), "event_type"
        )
        aggregate_type = JournalStore._require_text(
            self, envelope.get("aggregate_type"), "aggregate_type"
        )
        aggregate_id = JournalStore._require_text(
            self, envelope.get("aggregate_id"), "aggregate_id"
        )
        try:
            raw_aggregate_version = envelope["aggregate_version"]
        except KeyError as error:
            raise ValueError(
                "aggregate_version must be a positive canonical integer sequence string"
            ) from error
        aggregate_version = _impl._sequence(
            raw_aggregate_version,
            name="aggregate_version",
            positive=True,
        )
        if aggregate_version != 1:
            raise ValueError("first-event claim requires aggregate_version 1")

        payload = envelope.get("payload")
        expected_payload_hash = _impl.payload_digest(payload)
        if envelope.get("payload_hash") != expected_payload_hash:
            raise ValueError("payload_hash does not match payload")
        payload_json = _impl.canonical_json(payload)
        envelope_json = _impl.canonical_json(envelope)
        envelope_hash = _impl._event_envelope_digest(envelope_json)
        committed_at = JournalStore._require_text(
            self, envelope.get("committed_at"), "committed_at"
        )

        with journal_store_authority_scope(self, identity):
            with JournalStore._connect(self) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    if JournalStore._journal_sequence_value(self, connection) != 0:
                        raise ValueError(
                            "journal store already contains durable business state"
                        )
                    for table in (
                        "outbox",
                        "command_dedupe",
                        "projection_checkpoints",
                        "global_projection_checkpoints",
                    ):
                        row = connection.execute(
                            f"SELECT COUNT(*) AS row_count FROM {table}"
                        ).fetchone()
                        if row is None or type(row["row_count"]) is not int:
                            raise RuntimeError(
                                f"whole-store authority count failed for {table}"
                            )
                        if row["row_count"] != 0:
                            raise ValueError(
                                "journal store already contains durable business state"
                            )
                    if JournalStore._aggregate_version_value(
                        self, connection, aggregate_type, aggregate_id
                    ) != 0:
                        raise ValueError(
                            "journal store already contains durable business state"
                        )

                    connection.execute(
                        """
                        INSERT INTO events(
                            event_id, event_type, aggregate_type, aggregate_id,
                            aggregate_version, payload_json, payload_hash, committed_at,
                            envelope_json, envelope_hash, journal_sequence
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            event_id,
                            event_type,
                            aggregate_type,
                            aggregate_id,
                            aggregate_version,
                            payload_json,
                            expected_payload_hash,
                            committed_at,
                            envelope_json,
                            envelope_hash,
                        ),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        return _impl.AppendResult(event_id, 1, True)

    def whole_store_state_cut(self) -> dict[str, Any]:
        """Return global journal cursor and business cardinalities from one snapshot."""

        identity = require_exact_journal_store_authority(
            self,
            subject="whole-store state cut",
        )
        with journal_store_authority_scope(self, identity):
            with JournalStore._connect(self) as connection:
                connection.execute("BEGIN")
                try:
                    result = {
                        "journal_sequence": JournalStore._journal_sequence_value(
                            connection
                        ),
                        "counts": _impl._whole_store_state_counts(connection),
                    }
                    connection.commit()
                    return result
                except Exception:
                    connection.rollback()
                    raise

    def whole_store_state_counts(self) -> dict[str, int]:
        """Return exact durable business-table cardinalities at one read cut."""

        return dict(self.whole_store_state_cut()["counts"])

    def outbox_delivery_state(
        self,
        event_id: str,
        *,
        topic: str,
    ) -> dict[str, Any]:
        """Read one event's exact outbox publication and delivery state.

        Recovery code must distinguish a legitimately delivered row from a
        missing/corrupt publication record.  This read keeps the authoritative
        event envelope and its outbox row in one SQLite snapshot and validates
        their exact byte binding before reporting delivery state.
        """

        identity = require_exact_journal_store_authority(
            self,
            subject="outbox delivery-state store",
        )
        event_id = JournalStore._require_text(self, event_id, "event_id")
        topic = JournalStore._require_text(self, topic, "topic")
        with journal_store_authority_scope(self, identity):
            with JournalStore._connect(self) as connection:
                connection.execute("BEGIN")
                try:
                    row = connection.execute(
                        """
                        SELECT
                            outbox.outbox_id,
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
                            events.envelope_hash AS event_envelope_hash,
                            events.journal_sequence
                        FROM outbox
                        JOIN events ON events.event_id = outbox.event_id
                        WHERE outbox.event_id = ? AND outbox.topic = ?
                        """,
                        (event_id, topic),
                    ).fetchone()
                    if row is None:
                        raise ValueError(
                            "durable outbox publication is missing for bootstrap event"
                        )

                    raw_outbox_payload = row["outbox_payload_json"]
                    if not isinstance(raw_outbox_payload, str):
                        raise ValueError("outbox payload authority is missing")
                    actual_outbox_hash = _impl._outbox_envelope_digest(
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
                        "journal_sequence": row["journal_sequence"],
                    }
                    event = JournalStore._decode_event_row(event_row)
                    authoritative_envelope = dict(event)
                    authoritative_envelope.pop("journal_sequence", None)
                    authoritative_envelope["aggregate_version"] = str(
                        authoritative_envelope["aggregate_version"]
                    )
                    if _impl.canonical_json(authoritative_envelope) != raw_outbox_payload:
                        raise ValueError(
                            "outbox payload does not match authoritative journal event envelope"
                        )

                    delivered_at = row["delivered_at"]
                    if delivered_at is not None:
                        if not isinstance(delivered_at, str) or not delivered_at:
                            raise ValueError("outbox delivered_at is invalid")
                    result = {
                        "outbox_id": str(row["outbox_id"]),
                        "event_id": event_id,
                        "topic": topic,
                        "envelope_hash": str(row["envelope_hash"]),
                        "delivered": delivered_at is not None,
                    }
                    connection.commit()
                    return result
                except Exception:
                    connection.rollback()
                    raise

    def load_command_event_batch(
        self,
        *,
        command_id: str,
        actor: str,
        environment: str,
        idempotency_key: str,
        request: Any,
    ) -> dict[str, Any] | None:
        """Read one authenticated EVENT_BATCH command and its events at one cut.

        The read transaction establishes one SQLite snapshot before command
        resolution and retains it while the command effect, event rows, global
        sequence and aggregate sequences are integrity-checked. Callers can
        therefore replay financial ownership without split get_event snapshots.
        """

        command_id = self._require_text(command_id, "command_id")
        actor, environment, idempotency_key = self._command_scope(
            actor=actor,
            environment=environment,
            idempotency_key=idempotency_key,
        )
        request_hash = _impl.payload_digest(request)

        with self._connect() as connection:
            connection.execute("BEGIN")
            try:
                # This SELECT establishes the deferred read snapshot before any
                # command/effect lookup. A concurrent writer may commit later,
                # but it cannot become half-visible within this authority read.
                self._reject_legacy_unscoped_key(connection, idempotency_key)
                existing = connection.execute(
                    "SELECT * FROM command_dedupe "
                    "WHERE actor = ? AND environment = ? AND idempotency_key = ?",
                    (actor, environment, idempotency_key),
                ).fetchone()
                if existing is None:
                    connection.commit()
                    return None

                if existing["command_id"] != command_id:
                    raise ValueError(
                        "idempotency_key belongs to a different command_id"
                    )
                if existing["request_hash"] != request_hash:
                    raise ValueError(
                        "idempotency_key was already used for a different request"
                    )
                self._require_command_effect_kind(existing, "EVENT_BATCH")
                saved_result = self._decode_command_result(existing)

                state_version = existing["state_version"]
                if type(state_version) is not int or state_version < 0:
                    raise ValueError(
                        "command state_version is not a canonical non-negative integer"
                    )
                effect_json = existing["effect_json"]
                effect_hash = existing["effect_hash"]
                if not isinstance(effect_json, str) or not isinstance(
                    effect_hash, str
                ):
                    raise ValueError(
                        "command event-batch effect authority is missing"
                    )

                self._verify_stored_event_batch_effect(
                    connection,
                    effect_json=effect_json,
                    effect_hash=effect_hash,
                )
                effect = json.loads(effect_json)
                descriptors = effect["events"]
                decoded_events: list[dict[str, Any]] = []
                validated_aggregates: set[tuple[str, str]] = set()
                for descriptor in descriptors:
                    event_row = connection.execute(
                        """
                        SELECT event_id, event_type, aggregate_type, aggregate_id,
                               aggregate_version, payload_json, payload_hash,
                               committed_at, envelope_json, envelope_hash,
                               journal_sequence
                        FROM events
                        WHERE event_id = ?
                        """,
                        (descriptor["event_id"],),
                    ).fetchone()
                    if event_row is None:
                        raise ValueError("command event-batch event is missing")
                    event = self._decode_event_row(event_row)
                    decoded_events.append(event)
                    aggregate_key = (
                        str(event["aggregate_type"]),
                        str(event["aggregate_id"]),
                    )
                    if aggregate_key not in validated_aggregates:
                        self._aggregate_version_value(
                            connection,
                            aggregate_key[0],
                            aggregate_key[1],
                        )
                        validated_aggregates.add(aggregate_key)

                if self.SCHEMA_VERSION >= 6:
                    self._journal_sequence_value(connection)

                connection.commit()
                return {
                    "command_id": command_id,
                    "actor": actor,
                    "environment": environment,
                    "idempotency_key": idempotency_key,
                    "state_version": state_version,
                    "result": saved_result,
                    "events": tuple(decoded_events),
                }
            except Exception:
                connection.rollback()
                raise

    @contextmanager
    def _connect_windows(self):
        state = _require_exact_journal_store_state(self, subject="canonical JournalStore")
        expected = state.get("_store_identity")
        if expected is not None:
            expected = require_exact_journal_store_identity(
                expected, subject="selected journal store identity"
            )
        operation_expected = _journal_operation_expected_identity(self)
        if operation_expected is not None and expected != operation_expected:
            raise RuntimeError(
                "journal operation authority changed before connection"
            )
        path = _require_exact_journal_store_path(
            self, identity=expected, state=state, subject="canonical JournalStore"
        )
        with ExitStack() as stack:
            try:
                guarded = stack.enter_context(
                    guard_windows_database_authority(path, create=expected is None)
                )
            except OSError as error:
                raise RuntimeError(
                    "journal backing file is missing or inaccessible"
                ) from error
            if expected is not None and guarded != expected:
                raise RuntimeError("journal backing file identity changed")
            connection = sqlite3.connect(path, timeout=30, isolation_level=None)
            try:
                connection.row_factory = sqlite3.Row
                opened_identity = connection_main_identity(connection)
                if not same_journal_backing_object(opened_identity, guarded):
                    raise RuntimeError(
                        "SQLite main database does not match canonical journal authority"
                    )
                if expected is None:
                    self._store_identity = guarded
                    expected = guarded
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                yield connection
            finally:
                try:
                    # A first-open failure may occur before a store identity is
                    # established.  Do not mask that primary fail-closed verdict
                    # with a cleanup-only "identity was lost" error.
                    if expected is not None:
                        exit_state = _require_exact_journal_store_state(
                            self, subject="canonical JournalStore"
                        )
                        exit_identity = exit_state.get("_store_identity")
                        if exit_identity is None:
                            raise RuntimeError(
                                "journal store identity was lost while connection was active"
                            )
                        exit_identity = require_exact_journal_store_identity(
                            exit_identity, subject="selected journal store identity"
                        )
                        exit_path = _require_exact_journal_store_path(
                            self,
                            identity=exit_identity,
                            state=exit_state,
                            subject="canonical JournalStore",
                        )
                        if exit_path != path or exit_identity != expected:
                            raise RuntimeError(
                                "journal authority changed while connection was active"
                            )
                        opened_identity = connection_main_identity(connection)
                        if not same_journal_backing_object(
                            opened_identity,
                            expected,
                        ):
                            raise RuntimeError(
                                "SQLite main database changed while connection was active"
                            )
                finally:
                    connection.close()

    @contextmanager
    def _connect(self):
        if sys.platform == "win32":
            with self._connect_windows() as connection:
                yield connection
            return

        state = _require_exact_journal_store_state(self, subject="canonical JournalStore")
        expected = state.get("_store_identity")
        if expected is not None:
            expected = require_exact_journal_store_identity(
                expected, subject="selected journal store identity"
            )
        operation_expected = _journal_operation_expected_identity(self)
        if operation_expected is not None and expected != operation_expected:
            raise RuntimeError(
                "journal operation authority changed before connection"
            )
        path = _require_exact_journal_store_path(
            self, identity=expected, state=state, subject="canonical JournalStore"
        )
        first_open_anchor: JournalStoreIdentity | None = None
        if expected is None:
            first_open_anchor = establish_database_anchor(path)
        else:
            require_database_identity(path, expected)

        connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            opened = connection_main_identity(connection)
            if opened.canonical_path != str(path):
                raise RuntimeError(
                    "SQLite main database path does not match canonical journal authority"
                )
            if expected is None:
                if opened != first_open_anchor:
                    raise RuntimeError(
                        "SQLite first open does not match the anchored journal backing file"
                    )
                self._store_identity = opened
                expected = opened
            elif opened != expected:
                raise RuntimeError("SQLite opened a different journal backing file")

            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            try:
                # Preserve the primary first-open/anchor error if establishment
                # failed before any canonical store identity existed.
                if expected is not None:
                    exit_state = _require_exact_journal_store_state(
                        self, subject="canonical JournalStore"
                    )
                    exit_identity = exit_state.get("_store_identity")
                    if exit_identity is None:
                        raise RuntimeError(
                            "journal store identity was lost while connection was active"
                        )
                    exit_identity = require_exact_journal_store_identity(
                        exit_identity, subject="selected journal store identity"
                    )
                    exit_path = _require_exact_journal_store_path(
                        self,
                        identity=exit_identity,
                        state=exit_state,
                        subject="canonical JournalStore",
                    )
                    if exit_path != path or exit_identity != expected:
                        raise RuntimeError(
                            "journal authority changed while connection was active"
                        )
                    if connection_main_identity(connection) != exit_identity:
                        raise RuntimeError(
                            "SQLite journal identity changed while connection was active"
                        )
                    require_database_identity(path, exit_identity)
            finally:
                connection.close()

def _require_exact_journal_store_state(
    value: object,
    *,
    subject: str,
) -> dict[object, object]:
    if not isinstance(value, JournalStore):
        raise TypeError(f"{subject} must be a JournalStore")
    state = vars(value)
    state_keys = tuple(state)
    if any(type(name) is not str for name in state_keys):
        raise TypeError("canonical JournalStore instance state keys must be exact str")
    return state


def _reject_journal_store_instance_shadows(
    state: dict[object, object],
) -> None:
    """Reject executable/class-member shadowing at external authority boundaries.

    Internal JournalStore I/O still seals raw state keys, canonical path and
    physical identity on every connection.  Shadow rejection belongs at the
    consumer boundary that is about to dispatch authority-bearing operations
    class-qualified; otherwise ordinary fault-injection wrappers around a bound
    JournalStore method make the original implementation unusable even when it
    is invoked explicitly.
    """

    class_owned_names = {
        name for base in JournalStore.__mro__ for name in base.__dict__
    }
    if class_owned_names.intersection(tuple(state)):
        raise TypeError("canonical JournalStore instance state is shadowed")


def _journal_operation_expected_identity(
    value: object,
) -> JournalStoreIdentity | None:
    binding = _JOURNAL_OPERATION_AUTHORITY.get()
    if binding is None:
        return None
    selected_store_id, selected_identity = binding
    if id(value) != selected_store_id:
        raise RuntimeError("journal operation authority store changed")
    return require_exact_journal_store_identity(
        selected_identity,
        subject="bound journal operation identity",
    )


def _require_exact_journal_store_path(
    value: object,
    *,
    identity: JournalStoreIdentity | None,
    state: dict[object, object] | None = None,
    subject: str,
) -> Path:
    if state is None:
        state = _require_exact_journal_store_state(value, subject=subject)
    if "path" not in state:
        raise TypeError(f"{subject} path state is unavailable")
    path = state["path"]
    if type(path) is not type(Path()):
        raise TypeError(f"{subject} path must be exact platform Path")
    if not path.is_absolute():
        raise TypeError(f"{subject} path must be absolute")
    if identity is not None:
        identity = require_exact_journal_store_identity(
            identity, subject=f"{subject} identity"
        )
        if identity.canonical_path != str(path):
            raise RuntimeError(f"{subject} path and identity disagree")
    return path


def require_exact_journal_store_authority(
    value: object,
    *,
    subject: str = "canonical JournalStore",
) -> JournalStoreIdentity:
    if type(value) is not JournalStore:
        raise TypeError(f"{subject} must be exact JournalStore")
    state = _require_exact_journal_store_state(value, subject=subject)
    _reject_journal_store_instance_shadows(state)
    if "_store_identity" not in state:
        raise RuntimeError(f"{subject} identity state is unavailable")
    identity = state["_store_identity"]
    if identity is None:
        raise RuntimeError(f"{subject} identity is not established")
    identity = require_exact_journal_store_identity(
        identity, subject=f"{subject} identity"
    )
    _require_exact_journal_store_path(
        value, identity=identity, state=state, subject=subject
    )
    return identity


@contextmanager
def journal_store_authority_scope(
    value: object,
    expected_identity: JournalStoreIdentity,
):
    """Carry one selected physical store generation through actual SQLite I/O."""

    expected = require_exact_journal_store_identity(
        expected_identity,
        subject="expected journal operation identity",
    )
    current = require_exact_journal_store_authority(
        value,
        subject="bound journal operation store",
    )
    if current != expected:
        raise RuntimeError("journal operation authority changed before binding")

    # ContextVar tokens form a strict stack: a deterministic concurrency
    # interleave may perform another exact store operation and then return to
    # this one. Each nested _connect() still validates its own expected identity.
    binding = (id(value), expected)
    token = _JOURNAL_OPERATION_AUTHORITY.set(binding)
    try:
        yield
    finally:
        _JOURNAL_OPERATION_AUTHORITY.reset(token)

