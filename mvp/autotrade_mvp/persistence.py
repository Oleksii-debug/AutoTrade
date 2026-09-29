from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import sys

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

    @contextmanager
    def _connect_windows(self):
        expected = self._store_identity
        with guard_windows_database_authority(
            self.path,
            create=expected is None,
        ) as guarded:
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
