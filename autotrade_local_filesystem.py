from __future__ import annotations

import os
from pathlib import Path
import sys


class LocalFilesystemQualificationError(RuntimeError):
    """Raised when a durable local-filesystem path is not safely qualified."""


def require_qualified_local_filesystem_path(path: str | os.PathLike[str]) -> None:
    """Reuse the neutral resource-lock locality policy for durable local state.

    The current Windows policy rejects UNC paths plus UNKNOWN/NO_ROOT/REMOTE
    drive types before any caller creates or opens the target. POSIX callers
    preserve their existing behavior; this function does not pretend that
    pathname identity proves network-filesystem safety there.
    """

    if sys.platform != "win32":
        return

    try:
        from autotrade_research.artifacts.resource_lock import (
            ResourceLockError,
            _reject_known_remote_lock_path,
        )
    except ModuleNotFoundError:
        try:
            from research.autotrade_research.artifacts.resource_lock import (
                ResourceLockError,
                _reject_known_remote_lock_path,
            )
        except ModuleNotFoundError as error:
            raise LocalFilesystemQualificationError(
                "qualified Windows local-filesystem policy is unavailable"
            ) from error

    try:
        _reject_known_remote_lock_path(Path(path))
    except ResourceLockError as error:
        raise LocalFilesystemQualificationError(
            "path must be on a qualified local filesystem"
        ) from error
