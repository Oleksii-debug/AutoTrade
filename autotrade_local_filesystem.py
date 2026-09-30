"""Compatibility import for the production-packaged filesystem authority.

New production code imports :mod:`autotrade_runtime.local_filesystem` directly.
This module remains only so older source consumers do not regain the former
research-tree dependency.
"""

from autotrade_runtime.local_filesystem import (  # noqa: F401
    LocalFilesystemQualificationError,
    require_qualified_local_filesystem_path,
)

__all__ = [
    "LocalFilesystemQualificationError",
    "require_qualified_local_filesystem_path",
]
