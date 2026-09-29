from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3

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
    require_database_identity,
)


def __getattr__(name: str):
    """Preserve legacy module-level access, including private test helpers."""

    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))


class JournalStore(_JournalStoreImpl):
    """JournalStore with immutable canonical backing-file authority.

    The configured path is resolved exactly once at construction. The first
    SQLite open is anchored to a pre-open filesystem identity, and every later
    open verifies the same identity before and after use so deletion,
    replacement, CWD drift, hard-link ambiguity, or path rebinding can never
    silently redirect an existing store object to another journal.
    """

    def __init__(self, path: str | Path):
        self.path = freeze_database_path(path)
        self._store_identity: JournalStoreIdentity | None = None
        self._initialize()
        if self._store_identity is None:
            raise RuntimeError("journal store identity was not established")

    @property
    def store_identity(self) -> JournalStoreIdentity:
        identity = self._store_identity
        if identity is None:
            raise RuntimeError("journal store identity is not established")
        return identity

    @contextmanager
    def _connect(self):
        expected = self._store_identity
        first_open_anchor: JournalStoreIdentity | None = None
        if expected is None:
            first_open_anchor = establish_database_anchor(self.path)
        else:
            # This check occurs before sqlite3.connect so a deleted/replaced
            # path cannot be silently recreated/adopted by an old store object.
            require_database_identity(self.path, expected)

        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
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
