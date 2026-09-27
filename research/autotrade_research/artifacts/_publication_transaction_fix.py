from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import os
import stat
from typing import Any
from uuid import uuid4

from . import _namespace_guard as _guard
from . import _publication_admission as _admission
from . import _retained_namespace as _retained
from . import _retained_publication as _publication
from . import _retained_publication_hardening as _posix
from . import _windows_retained_publication as _windows
from . import _windows_retained_publication_hardening as _win

_store = _posix._store


def _read_exact_descriptor(descriptor: int, expected: bytes) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = len(expected) + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(1024 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _rollback_new_manifest_posix(
    self,
    artifact_id: str,
    expected_manifest: dict[str, Any],
) -> bool:
    name = f"{artifact_id}.json"
    expected = _publication._json_bytes(expected_manifest)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow,
                dir_fd=self._retained_manifests_fd,
            )
        except FileNotFoundError:
            return False
        opened = os.fstat(descriptor)
        self._reject_reparse_point(opened, subject="rejected artifact manifest")
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise _store.ArtifactIntegrityError(
                "rejected artifact publication manifest rollback target is not canonical"
            )
        if opened.st_size != len(expected):
            raise _store.ArtifactIntegrityError(
                "rejected artifact publication manifest rollback target changed identity"
            )
        if _read_exact_descriptor(descriptor, expected) != expected:
            raise _store.ArtifactIntegrityError(
                "rejected artifact publication manifest rollback target changed identity"
            )
    except _store.ArtifactIntegrityError:
        raise
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "rejected artifact publication manifest rollback inspection failed"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)

    try:
        os.unlink(name, dir_fd=self._retained_manifests_fd)
        _publication._sync_directory_fd(self._retained_manifests_fd)
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "rejected artifact publication manifest rollback failed"
        ) from error
    return True


def _rollback_new_manifest_windows(
    self,
    artifact_id: str,
    expected_manifest: dict[str, Any],
) -> bool:
    expected = _publication._json_bytes(expected_manifest)
    manifests = _windows._open_mutation_directory(
        self,
        ("manifests",),
        retained_name="manifests",
    )
    handle = None
    descriptor = None
    try:
        try:
            handle = _windows._nt_create_relative(
                manifests,
                f"{artifact_id}.json",
                directory=False,
                disposition=_windows._FILE_OPEN,
                desired_access=(
                    _guard._NT_FILE_READ_DATA
                    | _guard._NT_FILE_READ_ATTRIBUTES
                    | _windows._DELETE
                    | _guard._NT_SYNCHRONIZE
                ),
                subject="rejected artifact manifest",
            )
        except FileNotFoundError:
            return False
        descriptor = _guard._windows_file_handle_to_descriptor(handle)
        handle = None
        opened = os.fstat(descriptor)
        self._reject_reparse_point(opened, subject="rejected artifact manifest")
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise _store.ArtifactIntegrityError(
                "rejected artifact publication manifest rollback target is not canonical"
            )
        if opened.st_size != len(expected):
            raise _store.ArtifactIntegrityError(
                "rejected artifact publication manifest rollback target changed identity"
            )
        if _read_exact_descriptor(descriptor, expected) != expected:
            raise _store.ArtifactIntegrityError(
                "rejected artifact publication manifest rollback target changed identity"
            )
        _win._delete_fd_on_close(descriptor)
        os.close(descriptor)
        descriptor = None
        _store.sync_parent_directory(self._manifest_path(artifact_id))
        return True
    except _store.ArtifactIntegrityError:
        raise
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "rejected artifact publication manifest rollback failed"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        elif handle is not None:
            _guard._close_windows_handle(handle)
        _guard._close_windows_handle(manifests)


def _delete_admission_marker_posix(self, name: str) -> None:
    try:
        os.unlink(name, dir_fd=self._retained_manifests_fd)
    except FileNotFoundError:
        return
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "artifact publication admission rollback failed"
        ) from error


def _delete_admission_marker_windows(self, name: str) -> None:
    manifests = _windows._open_mutation_directory(
        self,
        ("manifests",),
        retained_name="manifests",
    )
    handle = None
    descriptor = None
    try:
        try:
            handle = _windows._nt_create_relative(
                manifests,
                name,
                directory=False,
                disposition=_windows._FILE_OPEN,
                desired_access=(
                    _guard._NT_FILE_READ_ATTRIBUTES
                    | _windows._DELETE
                    | _guard._NT_SYNCHRONIZE
                ),
                subject="artifact publication admission rollback marker",
            )
        except FileNotFoundError:
            return
        descriptor = _guard._windows_file_handle_to_descriptor(handle)
        handle = None
        _win._delete_fd_on_close(descriptor)
        os.close(descriptor)
        descriptor = None
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "artifact publication admission rollback failed"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        elif handle is not None:
            _guard._close_windows_handle(handle)
        _guard._close_windows_handle(manifests)


def _rollback_admission_markers(self, artifact_id: str) -> None:
    names = (
        _admission._marker_name(artifact_id, _admission._PREPARED),
        _admission._marker_name(artifact_id, _admission._COMMITTED),
    )
    for name in names:
        if os.name == "nt":
            _delete_admission_marker_windows(self, name)
        else:
            _delete_admission_marker_posix(self, name)
    if os.name != "nt":
        _publication._sync_directory_fd(self._retained_manifests_fd)
    else:
        _store.sync_parent_directory(self._manifest_path(artifact_id))


def _rollback_or_raise(self, manifest, failure, *, windows: bool) -> None:
    try:
        if windows:
            _rollback_new_manifest_windows(
                self,
                manifest["artifact_id"],
                manifest,
            )
        else:
            _rollback_new_manifest_posix(
                self,
                manifest["artifact_id"],
                manifest,
            )
        _rollback_admission_markers(self, manifest["artifact_id"])
    except BaseException as rollback_error:
        error = _store.ArtifactIntegrityError(
            "rejected artifact publication could not roll back committed state"
        )
        try:
            error.add_note(
                f"publication also failed: {type(failure).__name__}: {failure}"
            )
        except BaseException:
            pass
        raise error from rollback_error


def _publish_bytes_posix_transactional(
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

        existing = _posix._existing_manifest(self, normalized_id)
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
                    "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }
                rebound["manifest_hash"] = _store._manifest_integrity_hash(rebound)
                _posix._publish_manifest_posix(
                    self,
                    manifest=rebound,
                    replace_existing=True,
                )
                _retained._assert_all_continuity(self)
                return rebound
            return existing

        prefix, prefix_identity = _posix._publish_object_posix(
            self,
            digest=digest,
            data=data,
        )
        _posix._assert_prefix_identity(self, prefix, prefix_identity)
        _retained._assert_all_continuity(self)

        manifest = {
            "schema_version": self.SCHEMA_VERSION,
            **immutable,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        manifest["manifest_hash"] = _store._manifest_integrity_hash(manifest)
        transaction_id = uuid4().hex
        _admission.prepare_publication(
            self,
            manifest=manifest,
            transaction_id=transaction_id,
            prefix_identity=prefix_identity,
        )
        try:
            _posix._publish_manifest_posix(
                self,
                manifest=manifest,
                replace_existing=False,
            )
            _posix._assert_prefix_identity(self, prefix, prefix_identity)
            _retained._assert_all_continuity(self)
            self._verify_manifest_object(manifest)
            _admission.commit_publication(
                self,
                manifest=manifest,
                transaction_id=transaction_id,
                prefix_identity=prefix_identity,
            )
            return manifest
        except BaseException as failure:
            _rollback_or_raise(self, manifest, failure, windows=False)
            raise


def _publish_bytes_windows_transactional(
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
                    "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }
                rebound["manifest_hash"] = _store._manifest_integrity_hash(rebound)
                _win._publish_manifest_windows(
                    self,
                    manifest=rebound,
                    replace_existing=True,
                )
                _retained._assert_all_continuity(self)
                return rebound
            return existing

        prefix_name, prefix_identity, prefix_handle = _win._publish_object_windows_bound(
            self,
            digest=digest,
            data=data,
        )
        try:
            _win._assert_windows_prefix_identity(self, prefix_name, prefix_identity)
            _retained._assert_all_continuity(self)
            manifest = {
                "schema_version": self.SCHEMA_VERSION,
                **immutable,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            manifest["manifest_hash"] = _store._manifest_integrity_hash(manifest)
            transaction_id = uuid4().hex
            _admission.prepare_publication(
                self,
                manifest=manifest,
                transaction_id=transaction_id,
                prefix_identity=prefix_identity,
            )
            try:
                _win._publish_manifest_windows(
                    self,
                    manifest=manifest,
                    replace_existing=False,
                )
                _win._verify_bound_windows_object(
                    self,
                    prefix_handle,
                    digest,
                    expected_bytes=len(data),
                )
                _win._assert_windows_prefix_identity(self, prefix_name, prefix_identity)
                _retained._assert_all_continuity(self)
                _admission.commit_publication(
                    self,
                    manifest=manifest,
                    transaction_id=transaction_id,
                    prefix_identity=prefix_identity,
                )
                return manifest
            except BaseException as failure:
                _rollback_or_raise(self, manifest, failure, windows=True)
                raise
        finally:
            _guard._close_windows_handle(prefix_handle)


def install_publication_transaction_fix() -> None:
    artifact_store = _store.ArtifactStore
    if getattr(artifact_store, "_publication_transaction_fix", False):
        return
    _posix._publish_bytes_posix = _publish_bytes_posix_transactional
    _publication._publish_bytes_posix = _publish_bytes_posix_transactional
    _win._publish_bytes_windows_hardened = _publish_bytes_windows_transactional
    _windows._publish_bytes_windows = _publish_bytes_windows_transactional
    artifact_store._publication_transaction_fix = True
