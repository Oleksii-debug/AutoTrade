from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class JournalStoreIdentity:
    """Immutable process-lifetime identity for one SQLite journal authority.

    ``canonical_path`` is the durable configured-location identity. Device/inode
    are runtime rebinding evidence only: they deliberately prevent an existing
    JournalStore from silently adopting a replacement file at the same path.
    """

    canonical_path: str
    filesystem_device: int
    filesystem_inode: int


def freeze_database_path(path: str | Path) -> Path:
    """Freeze caller path text to one absolute canonical filesystem location."""

    candidate = Path(path).expanduser()
    # Parent creation is part of the existing JournalStore construction contract.
    # Do it before resolve() so existing symlink components are canonicalized while
    # a previously missing parent tree is still created relative to construction CWD.
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate.resolve(strict=False)


def observe_database_identity(path: str | Path) -> JournalStoreIdentity:
    """Observe one existing database file and reject unsafe hard-link aliases.

    SQLite WAL/SHM sidecars are pathname-derived. If the main database inode has
    multiple hard links, two different pathnames can address the same database
    while acquiring different sidecar namespaces and independent path fences.
    AutoTrade therefore treats a multi-link backing file as ambiguous authority
    and fails closed before WAL/journal mutation.
    """

    canonical = Path(path).resolve(strict=True)
    stat = canonical.stat()
    if int(stat.st_nlink) != 1:
        raise RuntimeError(
            "journal backing file must have exactly one hard-link pathname"
        )
    return JournalStoreIdentity(
        canonical_path=str(canonical),
        filesystem_device=int(stat.st_dev),
        filesystem_inode=int(stat.st_ino),
    )


def require_database_identity(
    path: str | Path,
    expected: JournalStoreIdentity,
) -> JournalStoreIdentity:
    """Fail closed if a frozen journal path no longer names the original file."""

    try:
        actual = observe_database_identity(path)
    except OSError as error:
        raise RuntimeError("journal backing file is missing or inaccessible") from error
    if actual != expected:
        raise RuntimeError("journal backing file identity changed")
    return actual


def connection_main_identity(connection: sqlite3.Connection) -> JournalStoreIdentity:
    """Return SQLite's current main-database pathname identity.

    The pathname reported by ``PRAGMA database_list`` is validated through the
    same single-link rule as every other journal identity observation. This is a
    bounded pathname/backing-file check, not a claim that SQLite exposes an
    atomic OS handle identity through Python's sqlite3 API.
    """

    main_path: str | None = None
    for row in connection.execute("PRAGMA database_list"):
        # sqlite3.Row and the default tuple row factory both support numeric access.
        if str(row[1]) == "main":
            main_path = str(row[2])
            break
    if not main_path:
        raise RuntimeError("SQLite main database has no durable filesystem path")
    return observe_database_identity(main_path)
