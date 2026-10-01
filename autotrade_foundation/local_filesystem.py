from __future__ import annotations

import os
import sys


class LocalFilesystemQualificationError(RuntimeError):
    """Raised when a durable path is not on a qualified local filesystem."""


def freeze_local_filesystem_path(
    path: str | os.PathLike[str],
) -> str:
    """Freeze path text before locality admission can race a process CWD change.

    Windows path qualification and later retained-namespace mutation must refer
    to one caller-path interpretation. Returning the lexical absolute spelling
    before the drive-type check lets consumers qualify and then use that exact
    path without a second current-directory lookup. POSIX preserves the existing
    pathname semantics because this foundation does not claim local storage
    authority there.
    """

    raw_path = os.fspath(path)
    if sys.platform == "win32":
        return os.path.abspath(raw_path)
    return raw_path


def require_qualified_local_filesystem_path(
    path: str | os.PathLike[str],
) -> None:
    """Reject Windows UNC and known non-local drive types before filesystem I/O.

    This package is deliberately dependency-neutral so both product persistence
    and research ResourceLock can consume one installed classifier. POSIX
    remains unchanged: pathname identity alone is not claimed to prove local
    storage there.
    """

    if sys.platform != "win32":
        return

    raw_path = os.fspath(path)
    if raw_path.startswith(("\\\\", "//")):
        raise LocalFilesystemQualificationError(
            "path must be on a qualified local filesystem"
        )

    absolute = os.path.abspath(raw_path)
    drive, _ = os.path.splitdrive(absolute)
    if not drive:
        raise LocalFilesystemQualificationError(
            "path has no qualified local Windows drive"
        )

    import ctypes
    from ctypes import wintypes

    get_drive_type = ctypes.WinDLL("kernel32", use_last_error=True).GetDriveTypeW
    get_drive_type.argtypes = (wintypes.LPCWSTR,)
    get_drive_type.restype = wintypes.UINT
    drive_type = get_drive_type(drive + "\\")

    # DRIVE_REMOVABLE=2, DRIVE_FIXED=3, DRIVE_CDROM=5, DRIVE_RAMDISK=6.
    # UNKNOWN=0, NO_ROOT_DIR=1 and REMOTE=4 are not qualified.
    if drive_type not in {2, 3, 5, 6}:
        raise LocalFilesystemQualificationError(
            "path must be on a qualified local filesystem"
        )
