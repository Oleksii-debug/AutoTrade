from __future__ import annotations

from dataclasses import dataclass
import os
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
    """Freeze caller path text to one absolute canonical filesystem location.

    Relative input is bound to one construction-time CWD before any filesystem
    operation. A concurrent process-wide chdir during parent creation can
    therefore never retarget the durable journal authority.
    """

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    # Parent creation is part of the existing JournalStore construction contract.
    # The candidate is already absolute, so any later CWD change cannot retarget
    # which durable authority is created or selected.
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate.resolve(strict=False)


def _identity_from_stat(canonical: Path, stat: os.stat_result) -> JournalStoreIdentity:
    if int(stat.st_nlink) != 1:
        raise RuntimeError(
            "journal backing file must have exactly one hard-link pathname"
        )
    return JournalStoreIdentity(
        canonical_path=str(canonical),
        filesystem_device=int(stat.st_dev),
        filesystem_inode=int(stat.st_ino),
    )


def observe_database_identity(path: str | Path) -> JournalStoreIdentity:
    """Observe one existing database file and reject unsafe hard-link aliases.

    SQLite WAL/SHM sidecars are pathname-derived. If the main database inode has
    multiple hard links, two different pathnames can address the same database
    while acquiring different sidecar namespaces and independent path fences.
    AutoTrade therefore treats a multi-link backing file as ambiguous authority
    and fails closed before WAL/journal mutation.
    """

    canonical = Path(path).resolve(strict=True)
    return _identity_from_stat(canonical, canonical.stat())


def establish_database_anchor(path: str | Path) -> JournalStoreIdentity:
    """Establish a pre-open backing-file identity for the first SQLite open.

    For a new journal, create the empty backing pathname atomically with
    ``O_EXCL`` and capture its filesystem identity before SQLite opens it. For an
    existing journal, capture the current identity first. The caller must compare
    SQLite's opened-main identity to this anchor before enabling WAL or mutating
    journal state. This closes the simple first-open A->B pathname replacement
    race without pretending Python's sqlite3 exposes an atomic OS handle identity.
    """

    canonical = Path(path).resolve(strict=False)
    try:
        return observe_database_identity(canonical)
    except FileNotFoundError:
        pass

    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    try:
        fd = os.open(canonical, flags, 0o600)
    except FileExistsError:
        return observe_database_identity(canonical)
    try:
        anchor = _identity_from_stat(canonical, os.fstat(fd))
    finally:
        os.close(fd)

    # Detect replacement/hard-linking between creation and descriptor close.
    require_database_identity(canonical, anchor)
    return anchor


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
