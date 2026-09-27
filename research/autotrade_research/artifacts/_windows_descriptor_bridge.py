from __future__ import annotations

import importlib
import os
from pathlib import Path
import stat
import sys

from . import _namespace_guard as _guard

_store = importlib.import_module(f"{__package__}.store")


def _windows_compatible_open_object_descriptor(
    self,
    object_path: Path,
    *,
    expected_bytes: int,
):
    if sys.platform != "win32":
        return _guard._hardened_open_object_descriptor(
            self,
            object_path,
            expected_bytes=expected_bytes,
        )

    before = self._validate_object_entry(object_path)
    if before.st_size != expected_bytes:
        raise _store.ArtifactIntegrityError("artifact object size mismatch")

    bound_descriptor, bound = _guard._open_bound_file(
        self,
        _guard._object_parts(self, object_path),
        subject="artifact object",
    )
    descriptor = None
    try:
        descriptor = _store._open_read_only_descriptor(object_path)
        opened = os.fstat(descriptor)
        self._reject_reparse_point(
            opened,
            subject="artifact object descriptor",
        )
        if not stat.S_ISREG(opened.st_mode):
            raise _store.ArtifactIntegrityError(
                "artifact object descriptor must be a regular file"
            )
        if opened.st_nlink != 1:
            raise _store.ArtifactIntegrityError(
                "artifact object descriptor must not have hard-link aliases"
            )
        if opened.st_size != expected_bytes:
            raise _store.ArtifactIntegrityError("artifact object size mismatch")

        current = self._validate_object_entry(object_path)
        if (
            not self._same_filesystem_entry(before, bound)
            or not self._same_filesystem_entry(bound, opened)
            or not self._same_filesystem_entry(opened, current)
        ):
            raise _store.ArtifactIntegrityError(
                "artifact object changed before descriptor read"
            )
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        raise
    finally:
        os.close(bound_descriptor)
    return descriptor, opened


def install_windows_object_descriptor_bridge() -> None:
    _store.ArtifactStore._open_object_descriptor = (
        _windows_compatible_open_object_descriptor
    )
