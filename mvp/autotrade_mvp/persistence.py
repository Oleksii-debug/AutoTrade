from __future__ import annotations

from contextlib import ExitStack, contextmanager
from pathlib import Path
import sqlite3
import sys
from typing import Any

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
)


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
        self.path = freeze_database_path(path)
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
        identity = self._store_identity
        if identity is None:
            raise RuntimeError("journal store identity is not established")
        return identity

    def claim_first_event(self, envelope: dict[str, Any]) -> _impl.AppendResult:
        """Atomically establish the first whole-store durable business authority.

        The claim succeeds only when the event journal, outbox, command dedupe,
        and both projection checkpoint stores are all empty in the same
        ``BEGIN IMMEDIATE`` transaction that writes journal sequence 1. Schema
        metadata is deliberately excluded: migrations describe storage shape,
        not prior business authority.

        This is intentionally a persistence primitive rather than simulation
        SQL. Callers may use the first event as their ownership marker, while a
        concurrent or pre-existing business writer causes the claim to roll
        back without adopting that state.
        """

        if not isinstance(envelope, dict):
            raise TypeError("envelope must be an object")
        event_id = self._require_text(envelope.get("event_id"), "event_id")
        event_type = self._require_text(envelope.get("event_type"), "event_type")
        aggregate_type = self._require_text(
            envelope.get("aggregate_type"), "aggregate_type"
        )
        aggregate_id = self._require_text(envelope.get("aggregate_id"), "aggregate_id")
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
        committed_at = self._require_text(envelope.get("committed_at"), "committed_at")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if self._journal_sequence_value(connection) != 0:
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
                if self._aggregate_version_value(
                    connection, aggregate_type, aggregate_id
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

    @contextmanager
    def _connect_windows(self):
        expected = self._store_identity
        with ExitStack() as stack:
            try:
                guarded = stack.enter_context(
                    guard_windows_database_authority(
                        self.path,
                        create=expected is None,
                    )
                )
            except OSError as error:
                raise RuntimeError(
                    "journal backing file is missing or inaccessible"
                ) from error

            if expected is not None and guarded != expected:
                raise RuntimeError("journal backing file identity changed")

            connection = sqlite3.connect(
                self.path,
                timeout=30,
                isolation_level=None,
            )
            try:
                connection.row_factory = sqlite3.Row
                opened_path = connection_main_path(connection)
                if opened_path != self.path:
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
                    opened_path = connection_main_path(connection)
                    if opened_path != self.path:
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

        expected = self._store_identity
        first_open_anchor: JournalStoreIdentity | None = None
        if expected is None:
            first_open_anchor = establish_database_anchor(self.path)
        else:
            require_database_identity(self.path, expected)

        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            opened = connection_main_identity(connection)
            if opened.canonical_path != str(self.path):
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
                identity = self._store_identity
                if identity is not None:
                    opened = connection_main_identity(connection)
                    if opened != identity:
                        raise RuntimeError(
                            "SQLite journal identity changed while connection was active"
                        )
                    require_database_identity(self.path, identity)
            finally:
                connection.close()
