from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import ctypes
import importlib
import os
import stat
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


def _assert_windows_prefix_identity(
    self,
    prefix_name: str,
    expected_identity: Any,
) -> None:
    """Require the current digest-prefix name to remain the retained generation."""

    _retained._assert_directory_continuity(
        self,
        ("objects", "sha256"),
        "objects",
    )
    parent = getattr(self, "_retained_objects_handle", None)
    if not parent:
        raise _store.ArtifactIntegrityError(
            "retained object namespace handle is unavailable"
        )
    current = _guard._nt_open_relative_handle(
        parent,
        prefix_name,
        directory=True,
        subject="artifact object prefix continuity",
    )
    try:
        current_identity = _guard._windows_handle_information(
            current,
            subject="artifact object prefix continuity",
        )
        if not _retained._same_windows_identity(
            expected_identity,
            current_identity,
        ):
            raise _store.ArtifactIntegrityError(
                "artifact object prefix changed during publication"
            )
    finally:
        _guard._close_windows_handle(current)


def _verify_bound_windows_object(
    self,
    prefix_handle: int,
    digest: str,
    *,
    expected_bytes: int,
) -> None:
    """Verify immutable object bytes through the original retained prefix HANDLE."""

    handle = _guard._nt_open_relative_handle(
        prefix_handle,
        digest,
        directory=False,
        subject="artifact object",
    )
    descriptor = None
    try:
        descriptor = _guard._windows_file_handle_to_descriptor(handle)
        handle = None
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
        digest_state = sha256()
        copied = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            digest_state.update(chunk)
        if copied != expected_bytes or digest_state.hexdigest() != digest:
            raise _store.ArtifactIntegrityError(
                "content-addressed object digest mismatch"
            )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        elif handle is not None:
            _guard._close_windows_handle(handle)


def _publish_object_windows_bound(
    self,
    *,
    digest: str,
    data: bytes,
) -> tuple[str, Any, int]:
    """Publish and return the exact prefix HANDLE retained through manifest commit."""

    objects = _windows._open_mutation_directory(
        self,
        ("objects", "sha256"),
        retained_name="objects",
    )
    staging = _windows._open_mutation_directory(
        self,
        ("staging",),
        retained_name="staging",
    )
    prefix_handle = None
    descriptor = None
    try:
        prefix_name = digest[:2]
        prefix_handle = _windows._open_or_create_prefix(objects, prefix_name)
        prefix_identity = _guard._windows_handle_information(
            prefix_handle,
            subject="artifact object prefix",
        )
        _assert_windows_prefix_identity(self, prefix_name, prefix_identity)

        try:
            existing = _guard._nt_open_relative_handle(
                prefix_handle,
                digest,
                directory=False,
                subject="artifact object",
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            _guard._close_windows_handle(existing)
            _verify_bound_windows_object(
                self,
                prefix_handle,
                digest,
                expected_bytes=len(data),
            )
            _assert_windows_prefix_identity(
                self,
                prefix_name,
                prefix_identity,
            )
            result = (prefix_name, prefix_identity, prefix_handle)
            prefix_handle = None
            return result

        _name, descriptor = _windows._create_temp_fd(
            staging,
            prefix="artifact",
        )
        _publication._write_all(descriptor, data)
        _publication._verify_staged_descriptor(
            descriptor,
            expected_digest=digest,
            expected_bytes=len(data),
        )
        _retained._assert_directory_continuity(
            self,
            ("staging",),
            "staging",
        )
        _assert_windows_prefix_identity(
            self,
            prefix_name,
            prefix_identity,
        )
        try:
            _windows._publish_temp_fd(
                descriptor,
                target_parent=prefix_handle,
                target_name=digest,
                replace=False,
            )
        except OSError as error:
            # _publish_temp_fd owns and closes the descriptor on failure too.
            descriptor = None
            if not _is_destination_collision(error):
                raise
        else:
            descriptor = None

        _verify_bound_windows_object(
            self,
            prefix_handle,
            digest,
            expected_bytes=len(data),
        )
        _assert_windows_prefix_identity(
            self,
            prefix_name,
            prefix_identity,
        )
        result = (prefix_name, prefix_identity, prefix_handle)
        prefix_handle = None
        return result
    finally:
        if descriptor is not None:
            try:
                _delete_fd_on_close(descriptor)
            except BaseException:
                pass
            os.close(descriptor)
        if prefix_handle is not None:
            _guard._close_windows_handle(prefix_handle)
        _guard._close_windows_handle(staging)
        _guard._close_windows_handle(objects)


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
            expected_digest=sha256(payload).hexdigest(),
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


def _publish_bytes_windows_hardened(
    self,
    *,
    artifact_id: str,
    data: bytes,
    media_type: str,
    rights: dict[str, Any],
    source_refs: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_id = self._artifact_id(artifact_id)
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if not isinstance(media_type, str) or not media_type.strip():
        raise ValueError("media_type is required")
    normalized_rights = self._validate_rights(rights)
    sources = list(source_refs or [])
    if not all(isinstance(item, str) and item for item in sources):
        raise ValueError("source_refs must contain non-empty strings")
    meta = dict(metadata or {})
    digest = sha256(data).hexdigest()

    with _store.ResourceLock(self.lock_path):
        manifest_path = self._manifest_path(normalized_id)
        self._validate_manifest_namespace(manifest_path)
        self._validate_staging_namespace()
        _retained._assert_all_continuity(self)

        try:
            existing = self.load_manifest(normalized_id)
        except FileNotFoundError:
            existing = None

        immutable = {
            "artifact_id": normalized_id,
            "sha256": f"sha256:{digest}",
            "bytes": len(data),
            "media_type": media_type,
            "rights": normalized_rights,
            "source_refs": sources,
            "metadata": meta,
        }
        if existing is not None:
            if any(existing.get(key) != value for key, value in immutable.items()):
                raise _store.ArtifactConflict(
                    "artifact_id is already committed with different content or metadata"
                )
            self._verify_manifest_object(existing)
            if not _store._verify_manifest_integrity(existing, required=False):
                rebound = {
                    "schema_version": self.SCHEMA_VERSION,
                    **immutable,
                    "created_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
                rebound["manifest_hash"] = _store._manifest_integrity_hash(rebound)
                _publish_manifest_windows(
                    self,
                    manifest=rebound,
                    replace_existing=True,
                )
                _retained._assert_all_continuity(self)
                return rebound
            return existing

        prefix_name, prefix_identity, prefix_handle = _publish_object_windows_bound(
            self,
            digest=digest,
            data=data,
        )
        try:
            _assert_windows_prefix_identity(
                self,
                prefix_name,
                prefix_identity,
            )
            _retained._assert_all_continuity(self)

            manifest = {
                "schema_version": self.SCHEMA_VERSION,
                **immutable,
                "created_at": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
            manifest["manifest_hash"] = _store._manifest_integrity_hash(manifest)
            _publish_manifest_windows(
                self,
                manifest=manifest,
                replace_existing=False,
            )

            # The object verified after manifest commit is the object under the
            # original retained prefix HANDLE, not whatever a same-name prefix
            # replacement might expose lexically.
            _verify_bound_windows_object(
                self,
                prefix_handle,
                digest,
                expected_bytes=len(data),
            )
            _assert_windows_prefix_identity(
                self,
                prefix_name,
                prefix_identity,
            )
            _retained._assert_all_continuity(self)
            return manifest
        finally:
            _guard._close_windows_handle(prefix_handle)


def install_windows_retained_publication_hardening() -> None:
    artifact_store = _store.ArtifactStore
    if getattr(
        artifact_store,
        "_windows_retained_publication_hardening",
        False,
    ):
        return

    _windows._delete_fd_on_close = _delete_fd_on_close
    _windows._publish_manifest_windows = _publish_manifest_windows
    _windows._publish_object_windows = _publish_object_windows_bound
    _windows._publish_bytes_windows = _publish_bytes_windows_hardened
    artifact_store._windows_retained_publication_hardening = True
