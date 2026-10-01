from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
import sys
from typing import Iterator

from autotrade_foundation.windows_namespace import (
    close_windows_handle,
    open_windows_regular_file,
    require_windows_namespace_component,
    retain_windows_parent_namespace,
    windows_handle_information,
)


@dataclass(frozen=True)
class JournalStoreIdentity:
    """Immutable process-lifetime identity for one SQLite journal authority.

    POSIX retains canonical-path + device/inode rebinding evidence. Supported
    Windows uses native opened-handle identity and deliberately leaves the POSIX
    fields unset rather than representing an unavailable st_ino as authority.
    """

    canonical_path: str
    filesystem_device: int | None
    filesystem_inode: int | None
    identity_source: str = "posix_stat"
    windows_volume_serial: int | None = None
    windows_file_index_high: int | None = None
    windows_file_index_low: int | None = None


def require_exact_journal_store_identity(
    value: object,
    *,
    subject: str = "journal store identity",
) -> JournalStoreIdentity:
    """Reject polymorphic state before any named lookup or equality dispatch."""

    if type(value) is not JournalStoreIdentity:
        raise TypeError(f"{subject} must be exact JournalStoreIdentity")
    state = vars(value)
    state_names = tuple(state)
    expected_names = frozenset(
        {
            "canonical_path",
            "filesystem_device",
            "filesystem_inode",
            "identity_source",
            "windows_volume_serial",
            "windows_file_index_high",
            "windows_file_index_low",
        }
    )
    if any(type(name) is not str for name in state_names):
        raise TypeError(f"{subject} state keys must be exact str")
    if frozenset(state_names) != expected_names:
        raise TypeError(f"{subject} state shape is non-canonical")

    canonical_path = state["canonical_path"]
    filesystem_device = state["filesystem_device"]
    filesystem_inode = state["filesystem_inode"]
    identity_source = state["identity_source"]
    windows_volume_serial = state["windows_volume_serial"]
    windows_file_index_high = state["windows_file_index_high"]
    windows_file_index_low = state["windows_file_index_low"]

    if type(canonical_path) is not str or not canonical_path:
        raise TypeError(f"{subject} canonical_path must be exact non-empty str")
    if type(identity_source) is not str:
        raise TypeError(f"{subject} identity_source must be exact str")

    if identity_source == "posix_stat":
        if (
            type(filesystem_device) is not int
            or type(filesystem_inode) is not int
            or windows_volume_serial is not None
            or windows_file_index_high is not None
            or windows_file_index_low is not None
        ):
            raise TypeError(f"{subject} has non-canonical POSIX identity fields")
    elif identity_source == "windows_by_handle":
        if (
            filesystem_device is not None
            or filesystem_inode is not None
            or type(windows_volume_serial) is not int
            or type(windows_file_index_high) is not int
            or type(windows_file_index_low) is not int
        ):
            raise TypeError(f"{subject} has non-canonical Windows identity fields")
        if windows_volume_serial == 0 or (
            windows_file_index_high == 0
            and windows_file_index_low == 0
        ):
            raise ValueError(f"{subject} has no strong Windows file identity")
    else:
        raise ValueError(f"{subject} identity_source is unsupported")

    return JournalStoreIdentity(
        canonical_path=canonical_path,
        filesystem_device=filesystem_device,
        filesystem_inode=filesystem_inode,
        identity_source=identity_source,
        windows_volume_serial=windows_volume_serial,
        windows_file_index_high=windows_file_index_high,
        windows_file_index_low=windows_file_index_low,
    )


def same_journal_backing_object(
    left: JournalStoreIdentity,
    right: JournalStoreIdentity,
) -> bool:
    """Compare the authoritative backing object without trusting path spelling.

    Windows path text is not a stable file identity: SQLite/Win32 may report a
    long-name/case-normalized spelling for the same file that was opened through
    a lexical absolute path. The retained HANDLE identity is authoritative.
    POSIX deliberately preserves the stricter canonical-path + dev/inode cut.
    """

    left = require_exact_journal_store_identity(
        left,
        subject="left journal store identity",
    )
    right = require_exact_journal_store_identity(
        right,
        subject="right journal store identity",
    )
    if left.identity_source != right.identity_source:
        return False
    if left.identity_source == "windows_by_handle":
        return (
            left.windows_volume_serial == right.windows_volume_serial
            and left.windows_file_index_high == right.windows_file_index_high
            and left.windows_file_index_low == right.windows_file_index_low
        )
    return left == right


def freeze_database_path(path: str | Path) -> Path:
    """Freeze caller path text without pre-authority Windows mutation.

    POSIX preserves the established canonical-path behavior. Windows performs
    only lexical absolute normalization here; parent creation and namespace
    traversal happen later inside the retained no-reparse authority guard.
    """

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if sys.platform == "win32":
        return Path(os.path.abspath(os.fspath(candidate)))
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate.resolve(strict=False)


def _identity_from_stat(
    canonical: Path,
    stat: os.stat_result,
) -> JournalStoreIdentity:
    if int(stat.st_nlink) != 1:
        raise RuntimeError(
            "journal backing file must have exactly one hard-link pathname"
        )
    if sys.platform == "win32" and int(stat.st_ino) == 0:
        raise RuntimeError(
            "Windows journal backing file has no strong native identity"
        )
    return JournalStoreIdentity(
        canonical_path=str(canonical),
        filesystem_device=int(stat.st_dev),
        filesystem_inode=int(stat.st_ino),
    )


def _windows_identity_from_handle(
    canonical: Path,
    handle: int,
) -> JournalStoreIdentity:
    information = windows_handle_information(
        handle,
        subject="journal backing file",
    )
    if information.number_of_links != 1:
        raise RuntimeError(
            "journal backing file must have exactly one hard-link pathname"
        )
    volume = information.volume_serial
    high = information.file_index_high
    low = information.file_index_low
    if volume == 0 or (high == 0 and low == 0):
        raise RuntimeError(
            "Windows journal backing file has no strong native identity"
        )
    return JournalStoreIdentity(
        canonical_path=str(canonical),
        filesystem_device=None,
        filesystem_inode=None,
        identity_source="windows_by_handle",
        windows_volume_serial=volume,
        windows_file_index_high=high,
        windows_file_index_low=low,
    )


@contextmanager
def guard_windows_database_authority(
    path: str | Path,
    *,
    create: bool,
) -> Iterator[JournalStoreIdentity]:
    """Pin parent namespace and final DB object through SQLite use."""

    if sys.platform != "win32":
        raise RuntimeError("Windows journal authority guard is Windows-only")
    canonical = freeze_database_path(path)
    require_windows_namespace_component(
        canonical.name,
        subject="journal backing file",
    )
    with retain_windows_parent_namespace(canonical, create=create):
        database_handle = open_windows_regular_file(
            canonical,
            create=create,
            subject="journal backing file",
        )
        active_error: BaseException | None = None
        try:
            try:
                # Parent generations and the final file are now retained with no
                # reparse traversal and no FILE_SHARE_DELETE. Canonicalize only at
                # this authority point: doing so earlier would follow caller path
                # aliases before the namespace had been proven safe.
                canonical = canonical.resolve(strict=True)
                identity = _windows_identity_from_handle(canonical, database_handle)
                try:
                    yield identity
                except BaseException as primary:
                    try:
                        current = _windows_identity_from_handle(
                            canonical,
                            database_handle,
                        )
                        if current != identity:
                            raise RuntimeError(
                                "Windows journal backing file identity changed while guarded"
                            )
                    except BaseException as authority_error:
                        raise authority_error from primary
                    raise
                else:
                    current = _windows_identity_from_handle(
                        canonical,
                        database_handle,
                    )
                    if current != identity:
                        raise RuntimeError(
                            "Windows journal backing file identity changed while guarded"
                        )
            except BaseException as error:
                active_error = error
                raise
        finally:
            try:
                close_windows_handle(database_handle)
            except BaseException as close_error:
                if active_error is None:
                    raise
                try:
                    active_error.add_note(
                        "Windows journal backing HANDLE cleanup also failed: "
                        f"{type(close_error).__name__}: {close_error}"
                    )
                except BaseException:
                    pass


def observe_database_identity(path: str | Path) -> JournalStoreIdentity:
    """Observe one existing DB and reject hard-link/namespace ambiguity."""

    if sys.platform == "win32":
        canonical = freeze_database_path(path)
        with guard_windows_database_authority(
            canonical,
            create=False,
        ) as identity:
            return identity
    canonical = Path(path).resolve(strict=True)
    return _identity_from_stat(canonical, canonical.stat())


def establish_database_anchor(path: str | Path) -> JournalStoreIdentity:
    """Establish pre-open identity for POSIX and guarded identity for Windows."""

    if sys.platform == "win32":
        canonical = freeze_database_path(path)
        with guard_windows_database_authority(
            canonical,
            create=True,
        ) as identity:
            return identity

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

    require_database_identity(canonical, anchor)
    return anchor


def require_database_identity(
    path: str | Path,
    expected: JournalStoreIdentity,
) -> JournalStoreIdentity:
    """Fail closed if a frozen journal path no longer names the expected file."""

    expected = require_exact_journal_store_identity(
        expected,
        subject="expected journal store identity",
    )
    try:
        actual = observe_database_identity(path)
    except OSError as error:
        raise RuntimeError(
            "journal backing file is missing or inaccessible"
        ) from error
    if actual != expected:
        raise RuntimeError("journal backing file identity changed")
    return actual


def connection_main_path(connection: sqlite3.Connection) -> Path:
    """Return the canonical filesystem path SQLite reports for main."""

    main_path: str | None = None
    for row in connection.execute("PRAGMA database_list"):
        if str(row[1]) == "main":
            main_path = str(row[2])
            break
    if not main_path:
        raise RuntimeError("SQLite main database has no durable filesystem path")
    return Path(main_path).resolve(strict=True)


def connection_main_identity(
    connection: sqlite3.Connection,
) -> JournalStoreIdentity:
    """Observe the current main path using the platform's canonical identity."""

    return observe_database_identity(connection_main_path(connection))
