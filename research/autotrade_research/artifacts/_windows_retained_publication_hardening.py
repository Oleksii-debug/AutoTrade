from __future__ import annotations

import ctypes
import importlib
import os
from typing import Any

from . import _namespace_guard as _guard
from . import _retained_namespace as _retained
from . import _retained_publication as _publication
from . import _windows_retained_publication as _windows

_store = importlib.import_module(f"{__package__}.store")

_WINDOWS_ERROR_FILE_EXISTS = 80
_WINDOWS_ERROR_ALREADY_EXISTS = 183


class _FileDispositionInfo(ctypes.Structure):
    """Exact Win32 FILE_DISPOSITION_INFO ABI: one BOOLEAN byte."""

    _fields_ = [("DeleteFile", ctypes.c_ubyte)]


def _delete_fd_on_close(descriptor: int) -> None:
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

    disposition = _FileDispositionInfo(DeleteFile=1)
    handle = msvcrt.get_osfhandle(descriptor)
    if not set_info(
        ctypes.c_void_p(handle),
        _windows._FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _is_destination_collision(error: OSError) -> bool:
    return getattr(error, "winerror", None) in {
        _WINDOWS_ERROR_FILE_EXISTS,
        _WINDOWS_ERROR_ALREADY_EXISTS,
    }


def _publish_manifest_windows(
    self,
    *,
    manifest: dict[str, Any],
    replace_existing: bool,
) -> None:
    """Publish one manifest without double-closing failed temp descriptors."""

    manifests = _windows._open_mutation_directory(
        self,
        ("manifests",),
        retained_name="manifests",
    )
    descriptor = None
    try:
        payload = _publication._json_bytes(manifest)
        _name, descriptor = _windows._create_temp_fd(
            manifests,
            prefix=f"manifest-{manifest['artifact_id']}",
        )
        _publication._write_all(descriptor, payload)
        _publication._verify_staged_descriptor(
            descriptor,
            expected_digest=__import__("hashlib").sha256(payload).hexdigest(),
            expected_bytes=len(payload),
        )
        _retained._assert_directory_continuity(
            self,
            ("manifests",),
            "manifests",
        )
        try:
            _windows._publish_temp_fd(
                descriptor,
                target_parent=manifests,
                target_name=f"{manifest['artifact_id']}.json",
                replace=replace_existing,
            )
        except OSError as error:
            # _publish_temp_fd owns-and-closes the descriptor on both success
            # and failure. Clear the caller slot before propagating so the
            # finally block cannot close a recycled fd and mask the root cause.
            descriptor = None
            if not replace_existing and _is_destination_collision(error):
                raise _store.ArtifactConflict(
                    "artifact_id became committed during publication"
                ) from error
            raise
        else:
            descriptor = None
    finally:
        if descriptor is not None:
            try:
                _delete_fd_on_close(descriptor)
            except BaseException:
                pass
            os.close(descriptor)
        _guard._close_windows_handle(manifests)


def install_windows_retained_publication_hardening() -> None:
    artifact_store = _store.ArtifactStore
    if getattr(
        artifact_store,
        "_windows_retained_publication_hardening",
        False,
    ):
        return

    # These functions are looked up through the module globals at call time by
    # the installed publication authority, so tightening them here preserves
    # the existing public ArtifactStore API and the single canonical lineage.
    _windows._delete_fd_on_close = _delete_fd_on_close
    _windows._publish_manifest_windows = _publish_manifest_windows
    artifact_store._windows_retained_publication_hardening = True
