"""Neutral retained Windows namespace authority for first-party durable I/O.

This module is dependency-neutral: persistence, staging and runtime consumers
share one Win32/NT directory-retention TCB rather than carrying private ctypes
copies. Handles intentionally omit FILE_SHARE_DELETE so a retained namespace
generation cannot be renamed/replaced by cooperating Windows processes while
the capability is held.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Iterator
from uuid import uuid4


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

_NT_FILE_READ_DATA = 0x00000001
_NT_FILE_LIST_DIRECTORY = 0x00000001
_NT_FILE_WRITE_DATA = 0x00000002
_NT_FILE_READ_ATTRIBUTES = 0x00000080
_NT_FILE_WRITE_ATTRIBUTES = 0x00000100
_NT_DELETE = 0x00010000
_NT_SYNCHRONIZE = 0x00100000
_NT_FILE_OPEN = 0x00000001
_NT_FILE_CREATE = 0x00000002
_NT_FILE_OPEN_IF = 0x00000003
_NT_FILE_DIRECTORY_FILE = 0x00000001
_NT_FILE_WRITE_THROUGH = 0x00000002
_NT_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_NT_FILE_NON_DIRECTORY_FILE = 0x00000040
_NT_FILE_OPEN_FOR_BACKUP_INTENT = 0x00004000
_NT_FILE_OPEN_REPARSE_POINT = 0x00200000
_NT_OBJ_CASE_INSENSITIVE = 0x00000040
_FILE_RENAME_INFORMATION_CLASS = 10
_FILE_DISPOSITION_INFO_CLASS = 4
_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
_WINDOWS_FORBIDDEN_WIN32_COMPONENT_CHARS = frozenset('<>:"/\\\\|?*')
_WINDOWS_RESERVED_DOS_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
    | {"COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"}
)


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    ]


class _FileRenameInformationHeader(ctypes.Structure):
    _fields_ = [
        ("ReplaceIfExists", ctypes.c_ubyte),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32),
    ]


_FILE_RENAME_NAME_OFFSET = (
    _FileRenameInformationHeader.FileNameLength.offset + ctypes.sizeof(ctypes.c_uint32)
)
_RETAINED_TERMINAL_HANDLES: dict[int, int] = {}


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
class WindowsHandleInformation:
    file_attributes: int
    volume_serial: int
    file_index_high: int
    file_index_low: int
    number_of_links: int


@dataclass(frozen=True)
class RetainedWindowsDirectory:
    canonical_path: str
    volume_serial: int
    file_index_high: int
    file_index_low: int


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("retained Windows namespace authority is Windows-only")


def close_windows_handle(handle: int) -> None:
    if not handle:
        return
    _require_windows()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    if not close_handle(ctypes.c_void_p(handle)):
        raise ctypes.WinError(ctypes.get_last_error())


def _add_cleanup_failure_note(
    primary: BaseException,
    *,
    context: str,
    failure: BaseException,
) -> None:
    primary.add_note(f"{context}: {type(failure).__name__}: {failure}")


def _close_windows_handles(
    handles,
    *,
    primary: BaseException | None = None,
) -> None:
    """Attempt every HANDLE close while preserving the primary failure."""

    first_failure: BaseException | None = None
    for handle in handles:
        try:
            close_windows_handle(handle)
        except BaseException as failure:
            if primary is not None:
                _add_cleanup_failure_note(
                    primary,
                    context="Windows HANDLE cleanup also failed",
                    failure=failure,
                )
            elif first_failure is None:
                first_failure = failure
            else:
                _add_cleanup_failure_note(
                    first_failure,
                    context="additional Windows HANDLE cleanup also failed",
                    failure=failure,
                )
    if primary is None and first_failure is not None:
        raise first_failure


def windows_handle_information(
    handle: int,
    *,
    subject: str,
) -> WindowsHandleInformation:
    _require_windows()
    if type(handle) is not int or handle <= 0:
        raise TypeError(f"{subject} handle must be a positive exact int")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_WindowsByHandleFileInformation),
    )
    get_info.restype = ctypes.c_int
    raw = _WindowsByHandleFileInformation()
    if not get_info(ctypes.c_void_p(handle), ctypes.byref(raw)):
        raise ctypes.WinError(ctypes.get_last_error())
    if raw.dwFileAttributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
        raise RuntimeError(f"{subject} must not be a reparse point")
    return WindowsHandleInformation(
        file_attributes=int(raw.dwFileAttributes),
        volume_serial=int(raw.dwVolumeSerialNumber),
        file_index_high=int(raw.nFileIndexHigh),
        file_index_low=int(raw.nFileIndexLow),
        number_of_links=int(raw.nNumberOfLinks),
    )


def _validate_component(name: str) -> str:
    if type(name) is not str or not name or name in {".", ".."}:
        raise RuntimeError("path contains an unsafe Windows namespace component")
    if name.endswith((" ", ".")):
        raise RuntimeError("path contains a Win32-normalized namespace component")
    if any(
        ord(character) < 32
        or character in _WINDOWS_FORBIDDEN_WIN32_COMPONENT_CHARS
        for character in name
    ):
        raise RuntimeError("path contains an unsafe Windows namespace component")
    basename = name.split(".", 1)[0].upper()
    if basename in _WINDOWS_RESERVED_DOS_BASENAMES:
        raise RuntimeError("path contains a reserved Windows DOS device component")
    return name


def require_windows_namespace_component(
    name: str,
    *,
    subject: str = "Windows pathname component",
) -> str:
    """Require one Win32/NT-stable ordinary pathname component."""

    if type(subject) is not str or not subject:
        raise TypeError("subject must be a non-empty exact str")
    try:
        return _validate_component(name)
    except RuntimeError as error:
        raise RuntimeError(
            f"{subject} is not a canonical Win32 pathname component"
        ) from error


def require_windows_namespace_path(
    path: str | Path,
    *,
    subject: str = "Windows pathname",
) -> Path:
    """Validate every lexical component before Win32 can normalize pathname text.

    Win32 absolute-path expansion may collapse trailing spaces/dots.  Durable
    consumers must reject those aliases before calling APIs that can erase the
    caller spelling and redirect creation into a different visible namespace.
    """

    _require_windows()
    if type(subject) is not str or not subject:
        raise TypeError("subject must be a non-empty exact str")
    candidate = Path(path)
    parts = candidate.parts
    if candidate.anchor and parts and parts[0] == candidate.anchor:
        parts = parts[1:]
    try:
        for component in parts:
            _validate_component(component)
    except RuntimeError as error:
        raise RuntimeError(
            f"{subject} contains a non-canonical Win32 pathname component"
        ) from error
    return candidate


def _open_root_directory(path: Path) -> int:
    _require_windows()
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
        info = windows_handle_information(value, subject="Windows namespace root")
        if not info.file_attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY:
            raise RuntimeError("Windows namespace root must be a directory")
    except BaseException as primary:
        _close_windows_handles((value,), primary=primary)
        raise
    return value


def _open_relative_directory(
    parent_handle: int,
    name: str,
    *,
    create: bool = False,
) -> int:
    _require_windows()
    if type(create) is not bool:
        raise TypeError("create must be boolean")
    name = _validate_component(name)
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
        _NT_FILE_OPEN_IF if create else _NT_FILE_OPEN,
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
        info = windows_handle_information(
            handle,
            subject="Windows namespace directory",
        )
        if not info.file_attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY:
            raise RuntimeError("Windows namespace component must be a directory")
    except BaseException as primary:
        _close_windows_handles((handle,), primary=primary)
        raise
    return handle


def _absolute_parts(path: Path) -> tuple[Path, tuple[str, ...]]:
    if not path.is_absolute():
        raise RuntimeError("Windows namespace path must be absolute")
    anchor = path.anchor
    if not anchor:
        raise RuntimeError("Windows namespace path has no absolute root")
    root = Path(anchor)
    try:
        parts = path.relative_to(root).parts
    except ValueError as error:
        raise RuntimeError("Windows namespace path escaped its root") from error
    return root, tuple(_validate_component(part) for part in parts)


def _open_directory_chain(
    path: Path,
    *,
    create_missing: bool = False,
) -> list[int]:
    if type(create_missing) is not bool:
        raise TypeError("create_missing must be boolean")
    root, parts = _absolute_parts(path)
    handles: list[int] = []
    try:
        current = _open_root_directory(root)
        handles.append(current)
        for component in parts:
            current = _open_relative_directory(
                current,
                component,
                create=create_missing,
            )
            handles.append(current)
        return handles
    except BaseException as primary:
        _close_windows_handles(reversed(handles), primary=primary)
        raise


@contextmanager
def retain_windows_directory_namespace(
    path: str | Path,
    *,
    create: bool = False,
) -> Iterator[RetainedWindowsDirectory]:
    """Retain one directory chain, optionally creating missing components safely.

    Missing directories are created one component at a time relative to the
    already-retained parent handle. Every existing or created component is
    opened with FILE_OPEN_REPARSE_POINT, validated as an ordinary directory,
    and retained without FILE_SHARE_DELETE for the context lifetime.
    """

    _require_windows()
    if type(create) is not bool:
        raise TypeError("create must be boolean")
    candidate = Path(path)
    handles = _open_directory_chain(candidate, create_missing=create)
    try:
        if not handles:
            raise RuntimeError("Windows directory namespace has no retained handle")
        info = windows_handle_information(
            handles[-1],
            subject="retained Windows directory",
        )
        if not info.file_attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY:
            raise RuntimeError("retained Windows namespace is not a directory")
        if info.volume_serial == 0 or (
            info.file_index_high == 0 and info.file_index_low == 0
        ):
            raise RuntimeError("retained Windows directory has no strong identity")
        authority = RetainedWindowsDirectory(
            canonical_path=str(candidate),
            volume_serial=info.volume_serial,
            file_index_high=info.file_index_high,
            file_index_low=info.file_index_low,
        )
        _RETAINED_TERMINAL_HANDLES[id(authority)] = handles[-1]
        try:
            yield authority
        finally:
            _RETAINED_TERMINAL_HANDLES.pop(id(authority), None)
    except BaseException as primary:
        _close_windows_handles(reversed(handles), primary=primary)
        raise
    else:
        _close_windows_handles(reversed(handles))


@contextmanager
def retain_windows_parent_namespace(
    path: str | Path,
    *,
    create: bool = False,
) -> Iterator[RetainedWindowsDirectory]:
    """Retain the target parent chain, optionally creating missing parents."""

    if type(create) is not bool:
        raise TypeError("create must be boolean")
    target = Path(path)
    with retain_windows_directory_namespace(
        target.parent,
        create=create,
    ) as authority:
        yield authority


@contextmanager
def retain_windows_relative_directory_namespace(
    authority: RetainedWindowsDirectory,
    components: tuple[str, ...],
    *,
    create: bool = False,
) -> Iterator[RetainedWindowsDirectory]:
    """Retain a child directory chain rooted at one already-live authority.

    No visible pathname is used for traversal or creation. Every component is
    opened/created relative to the preceding retained HANDLE with
    FILE_OPEN_REPARSE_POINT and without FILE_SHARE_DELETE.
    """

    _require_windows()
    if type(components) is not tuple:
        raise TypeError("components must be an exact tuple")
    if type(create) is not bool:
        raise TypeError("create must be boolean")
    parent_handle = _retained_authority_handle(authority)
    if not components:
        yield authority
        return

    validated = tuple(_validate_component(component) for component in components)
    handles: list[int] = []
    current = parent_handle
    try:
        for component in validated:
            current = _open_relative_directory(
                current,
                component,
                create=create,
            )
            handles.append(current)
        info = windows_handle_information(
            handles[-1],
            subject="retained relative Windows directory",
        )
        retained = RetainedWindowsDirectory(
            canonical_path=str(Path(authority.canonical_path).joinpath(*validated)),
            volume_serial=info.volume_serial,
            file_index_high=info.file_index_high,
            file_index_low=info.file_index_low,
        )
        _RETAINED_TERMINAL_HANDLES[id(retained)] = handles[-1]
        try:
            yield retained
        finally:
            _RETAINED_TERMINAL_HANDLES.pop(id(retained), None)
    except BaseException as primary:
        _close_windows_handles(reversed(handles), primary=primary)
        raise
    else:
        _close_windows_handles(reversed(handles))


def open_windows_regular_file(
    path: str | Path,
    *,
    create: bool,
    subject: str,
) -> int:
    """Open one exact regular file without FILE_SHARE_DELETE."""

    _require_windows()
    if type(create) is not bool:
        raise TypeError("create must be boolean")
    candidate = Path(path)
    if not candidate.is_absolute():
        raise RuntimeError(f"{subject} path must be absolute")
    require_windows_namespace_component(candidate.name, subject=subject)

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
        str(candidate),
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
        info = windows_handle_information(value, subject=subject)
        if info.file_attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY:
            raise RuntimeError(f"{subject} must be a regular file")
    except BaseException as primary:
        _close_windows_handles((value,), primary=primary)
        raise
    return value


def _retained_authority_handle(authority: RetainedWindowsDirectory) -> int:
    """Resolve one live opaque retained-directory authority to its held HANDLE."""

    _require_windows()
    if type(authority) is not RetainedWindowsDirectory:
        raise TypeError("authority must be an exact RetainedWindowsDirectory")
    handle = _RETAINED_TERMINAL_HANDLES.get(id(authority))
    if type(handle) is not int or handle <= 0:
        raise RuntimeError("retained Windows directory authority is not live")
    info = windows_handle_information(handle, subject="retained Windows authority")
    if (
        info.volume_serial != authority.volume_serial
        or info.file_index_high != authority.file_index_high
        or info.file_index_low != authority.file_index_low
    ):
        raise RuntimeError("retained Windows directory authority changed")
    return handle


def _raise_ntstatus(status: int) -> None:
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    rtl_error = ntdll.RtlNtStatusToDosError
    rtl_error.argtypes = (ctypes.c_long,)
    rtl_error.restype = ctypes.c_ulong
    raise ctypes.WinError(rtl_error(status))


def _regular_file_create_options(*, write_through: bool) -> int:
    """Return exact NT regular-file create options for retained publication."""

    if type(write_through) is not bool:
        raise TypeError("write_through must be boolean")
    options = (
        _NT_FILE_NON_DIRECTORY_FILE
        | _NT_FILE_SYNCHRONOUS_IO_NONALERT
        | _NT_FILE_OPEN_REPARSE_POINT
    )
    if write_through:
        options |= _NT_FILE_WRITE_THROUGH
    return options


def _nt_create_relative_file(
    parent_handle: int,
    name: str,
    *,
    disposition: int,
    desired_access: int,
    share_access: int,
    subject: str,
    write_through: bool = False,
) -> int:
    """Open/create one no-reparse regular file relative to a retained directory."""

    _require_windows()
    if type(write_through) is not bool:
        raise TypeError("write_through must be boolean")
    name = _validate_component(name)
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
        desired_access,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        0,
        share_access,
        disposition,
        _regular_file_create_options(write_through=write_through),
        None,
        0,
    )
    if status < 0:
        _raise_ntstatus(status)
    handle = int(result.value)
    try:
        info = windows_handle_information(handle, subject=subject)
        if info.file_attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY:
            raise RuntimeError(f"{subject} must be a regular file")
        if info.number_of_links != 1:
            raise RuntimeError(f"{subject} must not have hard-link aliases")
    except BaseException as primary:
        _close_windows_handles((handle,), primary=primary)
        raise
    return handle


def _file_handle_to_descriptor(handle: int) -> int:
    import msvcrt

    try:
        return msvcrt.open_osfhandle(
            handle,
            os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
    except BaseException as primary:
        _close_windows_handles((handle,), primary=primary)
        raise


def _file_handle_to_read_descriptor(handle: int) -> int:
    import msvcrt

    try:
        return msvcrt.open_osfhandle(
            handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException as primary:
        _close_windows_handles((handle,), primary=primary)
        raise


def _mark_descriptor_delete_on_close(descriptor: int) -> None:
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    set_info.restype = ctypes.c_int

    class _Disposition(ctypes.Structure):
        _fields_ = [("DeleteFile", ctypes.c_int)]

    disposition = _Disposition(DeleteFile=1)
    handle = msvcrt.get_osfhandle(descriptor)
    if not set_info(
        ctypes.c_void_p(handle),
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _rename_descriptor_relative(
    descriptor: int,
    *,
    target_parent: int,
    target_name: str,
    replace: bool,
) -> None:
    """Rename through NtSetInformationFile relative to the retained parent HANDLE."""

    import msvcrt

    target_name = _validate_component(target_name)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    set_info = ntdll.NtSetInformationFile
    set_info.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
    )
    set_info.restype = ctypes.c_long

    encoded = target_name.encode("utf-16-le")
    buffer = ctypes.create_string_buffer(_FILE_RENAME_NAME_OFFSET + len(encoded))
    header = _FileRenameInformationHeader.from_buffer(buffer)
    header.ReplaceIfExists = int(replace)
    header.RootDirectory = ctypes.c_void_p(target_parent)
    header.FileNameLength = len(encoded)
    ctypes.memmove(
        ctypes.addressof(buffer) + _FILE_RENAME_NAME_OFFSET,
        encoded,
        len(encoded),
    )
    io_status = _IoStatusBlock()
    handle = msvcrt.get_osfhandle(descriptor)
    status = set_info(
        ctypes.c_void_p(handle),
        ctypes.byref(io_status),
        ctypes.byref(buffer),
        len(buffer),
        _FILE_RENAME_INFORMATION_CLASS,
    )
    if status < 0:
        _raise_ntstatus(status)


@contextmanager
def serialize_windows_directory_publication(
    authority: RetainedWindowsDirectory,
    *,
    lock_name: str = ".autotrade-composition.lock",
) -> Iterator[None]:
    """Serialize cooperating publishers across one retained directory generation.

    The lock file is opened relative to the held directory HANDLE and byte 0 is
    locked synchronously/exclusively. A second cooperating process waits in
    LockFileEx instead of reading/preflighting a stale composition concurrently.
    Cleanup always attempts both unlock and CloseHandle; a primary body failure
    remains primary and receives cleanup failures as exception notes.
    """

    parent = _retained_authority_handle(authority)
    lock_name = _validate_component(lock_name)
    handle = _nt_create_relative_file(
        parent,
        lock_name,
        disposition=_NT_FILE_OPEN_IF,
        desired_access=_WINDOWS_GENERIC_READ | _WINDOWS_GENERIC_WRITE | _NT_SYNCHRONIZE,
        share_access=_WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE,
        subject="Windows publication lock",
    )
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    lock_file = kernel32.LockFileEx
    unlock_file = kernel32.UnlockFileEx
    lock_file.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(_Overlapped),
    )
    lock_file.restype = ctypes.c_int
    unlock_file.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(_Overlapped),
    )
    unlock_file.restype = ctypes.c_int
    overlapped = _Overlapped()
    acquired = False
    primary: BaseException | None = None
    try:
        if not lock_file(
            ctypes.c_void_p(handle),
            _LOCKFILE_EXCLUSIVE_LOCK,
            0,
            1,
            0,
            ctypes.byref(overlapped),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = True
        yield
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup_failure: BaseException | None = None
        if acquired:
            try:
                if not unlock_file(
                    ctypes.c_void_p(handle),
                    0,
                    1,
                    0,
                    ctypes.byref(overlapped),
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
            except BaseException as error:
                if primary is not None:
                    _add_cleanup_failure_note(
                        primary,
                        context="Windows publication unlock also failed",
                        failure=error,
                    )
                else:
                    cleanup_failure = error
        try:
            close_windows_handle(handle)
        except BaseException as error:
            if primary is not None:
                _add_cleanup_failure_note(
                    primary,
                    context="Windows publication lock CloseHandle also failed",
                    failure=error,
                )
            elif cleanup_failure is None:
                cleanup_failure = error
            else:
                _add_cleanup_failure_note(
                    cleanup_failure,
                    context="Windows publication lock CloseHandle also failed",
                    failure=error,
                )
        if primary is None and cleanup_failure is not None:
            raise cleanup_failure


@contextmanager
def retain_windows_regular_file(
    authority: RetainedWindowsDirectory,
    *,
    target_name: str,
    subject: str,
) -> Iterator[int]:
    """Retain one existing regular leaf while denying concurrent WRITE/DELETE."""

    parent = _retained_authority_handle(authority)
    target_name = _validate_component(target_name)
    handle = _nt_create_relative_file(
        parent,
        target_name,
        disposition=_NT_FILE_OPEN,
        desired_access=_WINDOWS_GENERIC_READ | _NT_FILE_READ_ATTRIBUTES | _NT_SYNCHRONIZE,
        share_access=_WINDOWS_FILE_SHARE_READ,
        subject=subject,
    )
    descriptor = _file_handle_to_read_descriptor(handle)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def publish_windows_regular_bytes_retained(
    authority: RetainedWindowsDirectory,
    *,
    target_name: str,
    data: bytes,
    replace: bool,
) -> Iterator[int]:
    """Publish exact bytes and retain that exact leaf generation until exit.

    The temporary file is opened with no sharing and NT FILE_WRITE_THROUGH,
    fsynced and verified through the same descriptor, then renamed relative to
    the retained parent. The write-through handle makes the rename metadata part
    of the same durable file publication boundary on the qualified Windows
    filesystem. Keeping the descriptor open after rename prevents replacement,
    deletion, or a second write-capable open until the caller releases it.
    """

    if type(data) is not bytes:
        raise TypeError("data must be exact bytes")
    if type(replace) is not bool:
        raise TypeError("replace must be boolean")
    parent = _retained_authority_handle(authority)
    target_name = _validate_component(target_name)

    descriptor: int | None = None
    for _attempt in range(32):
        temp_name = f".{target_name}.{uuid4().hex}.tmp"
        try:
            handle = _nt_create_relative_file(
                parent,
                temp_name,
                disposition=_NT_FILE_CREATE,
                desired_access=(
                    _WINDOWS_GENERIC_READ
                    | _WINDOWS_GENERIC_WRITE
                    | _NT_DELETE
                    | _NT_SYNCHRONIZE
                ),
                share_access=0,
                subject="Windows publication temporary file",
                write_through=True,
            )
        except FileExistsError:
            continue
        descriptor = _file_handle_to_descriptor(handle)
        break
    if descriptor is None:
        raise RuntimeError("Windows publication could not allocate a unique temporary file")

    renamed = False
    try:
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("Windows publication write made no progress")
            written += count
        os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        observed = bytearray()
        while len(observed) < len(data):
            chunk = os.read(descriptor, len(data) - len(observed))
            if not chunk:
                break
            observed.extend(chunk)
        if bytes(observed) != data:
            raise OSError("Windows publication temporary-file verification failed")
        _rename_descriptor_relative(
            descriptor,
            target_parent=parent,
            target_name=target_name,
            replace=replace,
        )
        renamed = True
        os.lseek(descriptor, 0, os.SEEK_SET)
        yield descriptor
    finally:
        if not renamed:
            try:
                _mark_descriptor_delete_on_close(descriptor)
            except BaseException:
                pass
        os.close(descriptor)


def publish_windows_regular_bytes(
    authority: RetainedWindowsDirectory,
    *,
    target_name: str,
    data: bytes,
    replace: bool,
) -> None:
    """Durably publish exact bytes relative to one retained parent."""

    with publish_windows_regular_bytes_retained(
        authority,
        target_name=target_name,
        data=data,
        replace=replace,
    ):
        pass
