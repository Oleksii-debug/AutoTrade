"""Compatibility import for the installed neutral filesystem authority."""

from autotrade_foundation.local_filesystem import (  # noqa: F401
    LocalFilesystemQualificationError,
    require_qualified_local_filesystem_path,
)

__all__ = [
    "LocalFilesystemQualificationError",
    "require_qualified_local_filesystem_path",
]
