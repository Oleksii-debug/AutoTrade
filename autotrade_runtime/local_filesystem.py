"""Production local-filesystem qualification shared by durable authorities.

This module is intentionally dependency-light and production-packaged.  It owns
only filesystem-locality qualification; it does not authorize trading or define
financial state.  Windows callers must reject network/unknown drive classes
before creating durable WAL state or local coordination files.
"""

from __future__ import annotations

import os
from pathlib import Path


class LocalFilesystemQualificationError(RuntimeError):
    """Raised when a durable path is outside the qualified local substrate."""


def _windows_drive_type(root: str) -> int:
    import ctypes
    from ctypes import wintypes

    get_drive_type = ctypes.WinDLL("kernel32", use_last_error=True).GetDriveTypeW
    get_drive_type.argtypes = (wintypes.LPCWSTR,)
    get_drive_type.restype = wintypes.UINT
    return int(get_drive_type(root))


def require_qualified_local_filesystem_path(
    path: str | os.PathLike[str],
) -> None:
    """Reject Windows UNC and unqualified/remote drive paths before mutation.

    POSIX behavior is deliberately unchanged: pathname/stat identity is not a
    network-filesystem classifier, so this helper makes no unsupported claim
    there.  Windows accepts the same local drive classes previously qualified
    by ResourceLock: removable, fixed, CD-ROM and RAM disk. UNKNOWN,
    NO_ROOT_DIR and REMOTE fail closed.
    """

    if os.name != "nt":
        return

    raw_path = os.fspath(Path(path))
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

    drive_type = _windows_drive_type(drive + "\\")
    if drive_type not in {2, 3, 5, 6}:
        raise LocalFilesystemQualificationError(
            "path must be on a qualified local filesystem"
        )
