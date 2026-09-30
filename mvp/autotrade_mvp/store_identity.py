from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
import sys
from typing import Iterator


_WINDOWS_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_FILE_SHARE_WRITE = 0x00000002
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_OPEN_ALWAYS = 4
_WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_GENERIC_READ = 0x80000000
_WINDOWS_GENERIC_WRITE = 0x40000000

_NT_FILE_LIST_DIRECTORY = 0x00000001
_NT_FILE_READ_ATTRIBUTES = 0x00000080
_NT_SYNCHRONIZE = 0x00100000
_NT_FILE_OPEN = 0x00000001
_NT_FILE_DIRECTORY_FILE = 0x00000001
_NT_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_NT_FILE_OPEN_FOR_BACKUP_INTENT = 0x00004000
_NT_FILE_OPEN_REPARSE_POINT = 0x00200000
_NT_OBJ_CASE_INSENSITIVE = 0x00000040


class _WindowsByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTimeLow", ctypes.c_uint32),
        ("ftCreationTimeHigh", ctypes.c_uint32),
        ("ftLastAccessTimeLow", ctypes.c_uint32),
        ("ftLastAccessTimeHigh", ctypes.c_uint32),
        ("ftLastWriteTimeLow", ctypes.c_uint32),
        ("ftLastWriteTimeHigh", ctypes.c_uint32),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_wchar_p),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ulong),
        ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", ctypes.c_ulong),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IoStatusUnion(ctypes.Union):
    _fields_ = [
        ("Status", ctypes.c_long),
        ("Pointer", ctypes.c_void_p),
    ]


class _IoStatusBlock(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("u", _IoStatusUnion),
        ("Information", ctypes.c_size_t),
    ]


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


def freeze_database_path(path: str | Path) -> Path:
    """Freeze caller path text to one absolute canonical filesystem location."""

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
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


def _close_windows_handle(handle: int) -> None:
    if not handle or sys.platform != "win32":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    close_handle(ctypes.c_void_p(handle))


def _windows_handle_information(
    handle: int,
    *,
    subject: str,
) -> _WindowsByHandleFileInformation:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_WindowsByHandleFileInformation),
    )
    get_info.restype = ctypes.c_int
    information = _WindowsByHandleFileInformation()
    if not get_info(ctypes.c_void_p(handle), ctypes.byref(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    if information.dwFileAttributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
        raise RuntimeError(f"{subject} must not be a reparse point")
    return information


def _validate_windows_component(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or ":" in name
    ):
        raise RuntimeError("journal path contains an unsafe namespace component")
    return name


def _open_windows_root_directory(path: Path) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    invalid = ctypes.c_void_p(-1).value
    handle = create_file(
        str(path),
        _NT_FILE_LIST_DIRECTORY | _NT_FILE_READ_ATTRIBUTES | _NT_SYNCHRONIZE,
        _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE,
        None,
        _WINDOWS_OPEN_EXISTING,
        _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS
        | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == invalid or handle is None:
        raise ctypes.WinError(ctypes.get_last_error())
    value = int(handle)
    try:
        information = _windows_handle_information(
            value,
            subject="journal namespace root",
        )
        if not information.dwFileAttributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY:
            raise RuntimeError("journal namespace root must be a directory")
    except BaseException:
        _close_windows_handle(value)
        raise
    return value


def _nt_open_relative_directory(parent_handle: int, name: str) -> int:
    name = _validate_windows_component(name)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    nt_create_file = ntdll.NtCreateFile
    nt_create_file.argtypes = (
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_ulong,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
    )
    nt_create_file.restype = ctypes.c_long

    name_buffer = ctypes.create_unicode_buffer(name)
    encoded_length = len(name.encode("utf-16-le"))
    unicode_name = _UnicodeString(
        Length=encoded_length,
        MaximumLength=encoded_length + 2,
        Buffer=ctypes.cast(name_buffer, ctypes.c_wchar_p),
    )
    attributes = _ObjectAttributes(
        Length=ctypes.sizeof(_ObjectAttributes),
        RootDirectory=ctypes.c_void_p(parent_handle),
        ObjectName=ctypes.pointer(unicode_name),
        Attributes=_NT_OBJ_CASE_INSENSITIVE,
        SecurityDescriptor=None,
        SecurityQualityOfService=None,
    )
    io_status = _IoStatusBlock()
    result = ctypes.c_void_p()
    status = nt_create_file(
        ctypes.byref(result),
        _NT_FILE_LIST_DIRECTORY | _NT_FILE_READ_ATTRIBUTES | _NT_SYNCHRONIZE,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        0,
        _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE,
        _NT_FILE_OPEN,
        _NT_FILE_DIRECTORY_FILE
        | _NT_FILE_SYNCHRONOUS_IO_NONALERT
        | _NT_FILE_OPEN_REPARSE_POINT
        | _NT_FILE_OPEN_FOR_BACKUP_INTENT,
        None,
        0,
    )
    if status < 0:
        rtl_error = ntdll.RtlNtStatusToDosError
        rtl_error.argtypes = (ctypes.c_long,)
        rtl_error.restype = ctypes.c_ulong
        raise ctypes.WinError(rtl_error(status))
    handle = int(result.value)
    try:
        information = _windows_handle_information(
            handle,
            subject="journal namespace directory",
        )
        if not information.dwFileAttributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY:
            raise RuntimeError("journal namespace component must be a directory")
    except BaseException:
        _close_windows_handle(handle)
        raise
    return handle


def _open_windows_database_file(path: Path, *, create: bool) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    invalid = ctypes.c_void_p(-1).value
    handle = create_file(
        str(path),
        _WINDOWS_GENERIC_READ | _WINDOWS_GENERIC_WRITE,
        _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE,
        None,
        _WINDOWS_OPEN_ALWAYS if create else _WINDOWS_OPEN_EXISTING,
        _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == invalid or handle is None:
        raise ctypes.WinError(ctypes.get_last_error())
    value = int(handle)
    try:
        information = _windows_handle_information(
            value,
            subject="journal backing file",
        )
        if information.dwFileAttributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY:
            raise RuntimeError("journal backing file must be a regular file")
    except BaseException:
        _close_windows_handle(value)
        raise
    return value


def _windows_identity_from_handle(
    canonical: Path,
    handle: int,
) -> JournalStoreIdentity:
    information = _windows_handle_information(
        handle,
        subject="journal backing file",
    )
    if information.dwFileAttributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY:
        raise RuntimeError("journal backing file must be a regular file")
    if int(information.nNumberOfLinks) != 1:
        raise RuntimeError(
            "journal backing file must have exactly one hard-link pathname"
        )
    volume = int(information.dwVolumeSerialNumber)
    high = int(information.nFileIndexHigh)
    low = int(information.nFileIndexLow)
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


def _open_windows_namespace_chain(canonical: Path) -> list[int]:
    anchor = canonical.anchor
    if not anchor:
        raise RuntimeError("Windows journal path has no absolute namespace root")
    root = Path(anchor)
    try:
        components = canonical.parent.relative_to(root).parts
    except ValueError as error:
        raise RuntimeError("Windows journal path escaped its namespace root") from error

    handles: list[int] = []
    try:
        current = _open_windows_root_directory(root)
        handles.append(current)
        for component in components:
            current = _nt_open_relative_directory(current, component)
            handles.append(current)
        return handles
    except BaseException:
        for handle in reversed(handles):
            _close_windows_handle(handle)
        raise


@contextmanager
def guard_windows_database_authority(
    path: str | Path,
    *,
    create: bool,
) -> Iterator[JournalStoreIdentity]:
    """Pin the Windows pathname namespace and final DB object through SQLite use.

    Every mutable ancestor plus the database file is held without
    FILE_SHARE_DELETE. The final identity is obtained from the retained file
    handle, not from pathlib/stat pathname re-observation.
    """

    if sys.platform != "win32":
        raise RuntimeError("Windows journal authority guard is Windows-only")
    canonical = Path(path).resolve(strict=False)
    handles = _open_windows_namespace_chain(canonical)
    database_handle = 0
    try:
        database_handle = _open_windows_database_file(canonical, create=create)
        # Transfer ownership to the cleanup stack immediately after a successful
        # open. Native identity/link validation below is deliberately fail-closed
        # and may raise; that failure must never leak a no-FILE_SHARE_DELETE handle.
        handles.append(database_handle)
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
            current = _windows_identity_from_handle(canonical, database_handle)
            if current != identity:
                raise RuntimeError(
                    "Windows journal backing file identity changed while guarded"
                )
    finally:
        for handle in reversed(handles):
            _close_windows_handle(handle)


def observe_database_identity(path: str | Path) -> JournalStoreIdentity:
    """Observe one existing DB and reject hard-link/namespace ambiguity."""

    canonical = Path(path).resolve(strict=True)
    if sys.platform == "win32":
        with guard_windows_database_authority(
            canonical,
            create=False,
        ) as identity:
            return identity
    return _identity_from_stat(canonical, canonical.stat())


def establish_database_anchor(path: str | Path) -> JournalStoreIdentity:
    """Establish pre-open identity for POSIX and guarded identity for Windows."""

    canonical = Path(path).resolve(strict=False)
    if sys.platform == "win32":
        with guard_windows_database_authority(
            canonical,
            create=True,
        ) as identity:
            return identity

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
