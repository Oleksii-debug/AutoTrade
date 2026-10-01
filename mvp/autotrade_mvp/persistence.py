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
    connection_main_path,
    establish_database_anchor,
    freeze_database_path,
    guard_windows_database_authority,
    require_database_identity,
    require_exact_journal_store_identity,
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
        try:
            require_qualified_local_filesystem_path(path)
        except LocalFilesystemQualificationError as error:
            raise RuntimeError(
                "journal database path must be on a qualified local filesystem"
            ) from error
        self.path = freeze_database_path(path)
        self._store_identity: JournalStoreIdentity | None = None
        self._protected_writer_capabilities: dict[int, tuple[object, str, str, str, str, int]] = {}
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
            if expected is None:
                # The first retained Windows authority may canonicalize an
                # alias spelling (for example an 8.3 temp path) only after the
                # no-reparse parent chain and exact final file are pinned.
                # Persist that trusted spelling before SQLite/path identity
                # comparisons so later operations use one canonical path.
                path = Path(guarded.canonical_path)
                self.path = path
            elif guarded != expected:
                raise RuntimeError("journal backing file identity changed")
            connection = sqlite3.connect(path, timeout=30, isolation_level=None)
            try:
                connection.row_factory = sqlite3.Row
                connection.create_function(
                    "autotrade_protected_writer_authority",
                    1,
                    lambda _aggregate_type: None,
                )
                opened_path = connection_main_path(connection)
                if opened_path != path:
                    raise RuntimeError(
                        "SQLite main database path does not match canonical journal authority"
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
                        if connection_main_path(connection) != path:
                            raise RuntimeError(
                                "SQLite main database path changed while connection was active"
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
            connection.create_function(
                "autotrade_protected_writer_authority",
                1,
                lambda _aggregate_type: None,
            )
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

