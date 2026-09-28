from __future__ import annotations

import ctypes

from . import _namespace_guard as _guard
from . import _windows_retained_publication as _windows

# Native FILE_INFORMATION_CLASS value used by NtSetInformationFile.
_FILE_RENAME_INFORMATION_CLASS = 10


class _FileRenameInformationHeader(ctypes.Structure):
    """Variable-length FILE_RENAME_INFORMATION prefix.

    FileName starts immediately after FileNameLength rather than after the
    structure's trailing alignment padding. ReplaceIfExists is a BOOLEAN.
    """

    _fields_ = [
        ("ReplaceIfExists", ctypes.c_ubyte),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32),
    ]


_FILE_NAME_OFFSET = (
    _FileRenameInformationHeader.FileNameLength.offset + ctypes.sizeof(ctypes.c_uint32)
)


def _rename_fd_native(
    descriptor: int,
    target_parent: int,
    target_name: str,
    *,
    replace: bool,
) -> None:
    """Rename an open file relative to the retained target directory handle.

    SetFileInformationByHandle(FileRenameInfo) is a Win32 facade whose
    relative RootDirectory behavior is not the retained NT namespace contract
    used by this module. NtSetInformationFile(FileRenameInformation) consumes
    FILE_RENAME_INFORMATION directly and therefore preserves the exact held
    directory HANDLE as rename authority.
    """

    import msvcrt

    _guard._validate_component(
        target_name,
        subject="artifact publication target",
    )
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    set_info = ntdll.NtSetInformationFile
    set_info.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_guard._IoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
    )
    set_info.restype = ctypes.c_long

    encoded = target_name.encode("utf-16-le")
    buffer = ctypes.create_string_buffer(_FILE_NAME_OFFSET + len(encoded))
    header = _FileRenameInformationHeader.from_buffer(buffer)
    header.ReplaceIfExists = int(replace)
    header.RootDirectory = ctypes.c_void_p(target_parent)
    header.FileNameLength = len(encoded)
    ctypes.memmove(
        ctypes.addressof(buffer) + _FILE_NAME_OFFSET,
        encoded,
        len(encoded),
    )

    io_status = _guard._IoStatusBlock()
    handle = msvcrt.get_osfhandle(descriptor)
    status = set_info(
        ctypes.c_void_p(handle),
        ctypes.byref(io_status),
        ctypes.byref(buffer),
        len(buffer),
        _FILE_RENAME_INFORMATION_CLASS,
    )
    if status < 0:
        _windows._raise_ntstatus(status)


def install_windows_retained_rename_fix() -> None:
    """Install the retained-HANDLE native rename implementation once."""

    if getattr(_windows, "_native_retained_rename_installed", False):
        return
    _windows._rename_fd = _rename_fd_native
    _windows._native_retained_rename_installed = True
