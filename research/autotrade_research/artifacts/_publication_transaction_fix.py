from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import os
import stat
from typing import Any

from . import _namespace_guard as _guard
from . import _retained_namespace as _retained
from . import _retained_publication as _publication
from . import _retained_publication_hardening as _posix
from . import _windows_retained_publication as _windows
from . import _windows_retained_publication_hardening as _win

_store = _posix._store


def _rollback_new_manifest_posix(self, artifact_id: str) -> None:
    name = f"{artifact_id}.json"
    try:
        opened = os.stat(
            name,
            dir_fd=self._retained_manifests_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "rejected artifact publication manifest rollback inspection failed"
        ) from error
    self._reject_reparse_point(opened, subject="rejected artifact manifest")
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        raise _store.ArtifactIntegrityError(
            "rejected artifact publication manifest rollback target is not canonical"
        )
    try:
        os.unlink(name, dir_fd=self._retained_manifests_fd)
        _publication._sync_directory_fd(self._retained_manifests_fd)
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "rejected artifact publication manifest rollback failed"
        ) from error


def _rollback_new_manifest_windows(self, artifact_id: str) -> None:
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
                    _guard._NT_FILE_READ_ATTRIBUTES
                    | _windows._DELETE
                    | _guard._NT_SYNCHRONIZE
                ),
                subject="rejected artifact manifest",
            )
        except FileNotFoundError:
            return
        descriptor = _guard._windows_file_handle_to_descriptor(handle)
        handle = None
        opened = os.fstat(descriptor)
        self._reject_reparse_point(opened, subject="rejected artifact manifest")
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise _store.ArtifactIntegrityError(
                "rejected artifact publication manifest rollback target is not canonical"
            )
        _win._delete_fd_on_close(descriptor)
        os.close(descriptor)
        descriptor = None
        _store.sync_parent_directory(self._manifest_path(artifact_id))
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
        committed = False
        try:
            _posix._publish_manifest_posix(
                self,
                manifest=manifest,
                replace_existing=False,
            )
            committed = True
            _posix._assert_prefix_identity(self, prefix, prefix_identity)
            _retained._assert_all_continuity(self)
            self._verify_manifest_object(manifest)
            return manifest
        except BaseException as failure:
            if committed:
                try:
                    _rollback_new_manifest_posix(self, normalized_id)
                except BaseException as rollback_error:
                    raise _store.ArtifactIntegrityError(
                        "rejected artifact publication could not roll back committed manifest"
                    ) from rollback_error
            raise failure


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
            committed = False
            try:
                _win._publish_manifest_windows(
                    self,
                    manifest=manifest,
                    replace_existing=False,
                )
                committed = True
                _win._verify_bound_windows_object(
                    self,
                    prefix_handle,
                    digest,
                    expected_bytes=len(data),
                )
                _win._assert_windows_prefix_identity(self, prefix_name, prefix_identity)
                _retained._assert_all_continuity(self)
                return manifest
            except BaseException as failure:
                if committed:
                    try:
                        _rollback_new_manifest_windows(self, normalized_id)
                    except BaseException as rollback_error:
                        raise _store.ArtifactIntegrityError(
                            "rejected artifact publication could not roll back committed manifest"
                        ) from rollback_error
                raise failure
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
