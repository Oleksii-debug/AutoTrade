from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import ctypes
import errno
import importlib
import os
import stat
import sys
from typing import Any

from . import _namespace_guard as _guard
from . import _retained_namespace as _retained
from . import _retained_publication as _publication

_store = importlib.import_module(f"{__package__}.store")

_RENAME_NOREPLACE = 1


def _rename_noreplace_posix(parent_fd: int, source: str, target: str) -> None:
    """Atomically rename inside one retained directory without replacement."""

    if not sys.platform.startswith("linux"):
        raise _store.ArtifactIntegrityError(
            "artifact manifest publication lacks crash-safe no-replace rename support"
        )
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise _store.ArtifactIntegrityError(
            "artifact manifest publication lacks crash-safe no-replace rename support"
        )
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(target),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise _store.ArtifactConflict(
            "artifact_id became committed during publication"
        )
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        raise _store.ArtifactIntegrityError(
            "artifact manifest publication lacks crash-safe no-replace rename support"
        )
    raise OSError(error_number, os.strerror(error_number), target)


def _assert_prefix_identity(
    self,
    prefix: str,
    expected: os.stat_result,
) -> None:
    _retained._assert_directory_continuity(
        self,
        ("objects", "sha256"),
        "objects",
    )
    descriptor = _guard._open_posix_directory_component(
        self,
        self._retained_objects_fd,
        prefix,
        subject="artifact object prefix",
    )
    try:
        current = os.fstat(descriptor)
        if not self._same_filesystem_entry(expected, current):
            raise _store.ArtifactIntegrityError(
                "artifact object prefix changed during publication"
            )
    finally:
        os.close(descriptor)


def _verify_bound_object_bytes(
    self,
    prefix_fd: int,
    digest: str,
    *,
    expected_bytes: int,
) -> None:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if not no_follow or os.open not in getattr(os, "supports_dir_fd", set()):
        raise _store.ArtifactIntegrityError(
            "artifact object verification lacks descriptor-relative platform support"
        )
    try:
        descriptor = os.open(
            digest,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | no_follow
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=prefix_fd,
        )
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "content-addressed object could not be verified from retained prefix"
        ) from error
    try:
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
        hasher = sha256()
        copied = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            hasher.update(chunk)
        if copied != expected_bytes or hasher.hexdigest() != digest:
            raise _store.ArtifactIntegrityError(
                "content-addressed object digest mismatch"
            )
    finally:
        os.close(descriptor)


def _publish_object_posix(
    self,
    *,
    digest: str,
    data: bytes,
) -> tuple[str, os.stat_result]:
    prefix = digest[:2]
    prefix_fd = _publication._ensure_object_prefix_posix(self, prefix)
    prefix_identity = os.fstat(prefix_fd)
    try:
        existing = _publication._entry_stat(prefix_fd, digest)
        if existing is not None:
            _verify_bound_object_bytes(
                self,
                prefix_fd,
                digest,
                expected_bytes=len(data),
            )
            _assert_prefix_identity(self, prefix, prefix_identity)
            return prefix, prefix_identity

        _retained._assert_directory_continuity(self, ("staging",), "staging")
        temporary_name = None
        descriptor = None
        try:
            temporary_name, descriptor = _publication._open_temp_posix(
                self._retained_staging_fd,
                prefix="artifact",
            )
            _publication._write_all(descriptor, data)
            _publication._verify_staged_descriptor(
                descriptor,
                expected_digest=digest,
                expected_bytes=len(data),
            )
            os.close(descriptor)
            descriptor = None

            _retained._assert_directory_continuity(
                self,
                ("staging",),
                "staging",
            )
            _assert_prefix_identity(self, prefix, prefix_identity)
            linked_new_object = False
            try:
                os.link(
                    temporary_name,
                    digest,
                    src_dir_fd=self._retained_staging_fd,
                    dst_dir_fd=prefix_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                _verify_bound_object_bytes(
                    self,
                    prefix_fd,
                    digest,
                    expected_bytes=len(data),
                )
            else:
                linked_new_object = True
                _publication._sync_directory_fd(prefix_fd)

            # Remove the staging alias before enforcing the canonical object's
            # single-link invariant. A successful link temporarily raises
            # st_nlink to two; a crash in that window is recoverable because
            # the second name lives in retained staging.
            _publication._safe_unlink(
                self._retained_staging_fd,
                temporary_name,
            )
            temporary_name = None
            _publication._sync_directory_fd(self._retained_staging_fd)
            if linked_new_object:
                _verify_bound_object_bytes(
                    self,
                    prefix_fd,
                    digest,
                    expected_bytes=len(data),
                )
            _assert_prefix_identity(self, prefix, prefix_identity)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_name is not None:
                _publication._safe_unlink(
                    self._retained_staging_fd,
                    temporary_name,
                )
    finally:
        os.close(prefix_fd)
    return prefix, prefix_identity


def _publish_manifest_posix(
    self,
    *,
    manifest: dict[str, Any],
    replace_existing: bool,
) -> None:
    name = f"{manifest['artifact_id']}.json"
    payload = _publication._json_bytes(manifest)
    temporary_name = None
    descriptor = None
    try:
        temporary_name, descriptor = _publication._open_temp_posix(
            self._retained_manifests_fd,
            prefix=f"manifest-{manifest['artifact_id']}",
        )
        _publication._write_all(descriptor, payload)
        _publication._verify_staged_descriptor(
            descriptor,
            expected_digest=sha256(payload).hexdigest(),
            expected_bytes=len(payload),
        )
        os.close(descriptor)
        descriptor = None

        _retained._assert_directory_continuity(
            self,
            ("manifests",),
            "manifests",
        )
        if replace_existing:
            os.replace(
                temporary_name,
                name,
                src_dir_fd=self._retained_manifests_fd,
                dst_dir_fd=self._retained_manifests_fd,
            )
        else:
            _rename_noreplace_posix(
                self._retained_manifests_fd,
                temporary_name,
                name,
            )
        temporary_name = None
        _publication._sync_directory_fd(self._retained_manifests_fd)

        committed = os.stat(
            name,
            dir_fd=self._retained_manifests_fd,
            follow_symlinks=False,
        )
        self._reject_reparse_point(
            committed,
            subject="artifact manifest",
        )
        if not stat.S_ISREG(committed.st_mode) or committed.st_nlink != 1:
            raise _store.ArtifactIntegrityError(
                "artifact manifest publication did not produce a canonical single-link file"
            )
    except (_store.ArtifactConflict, _store.ArtifactIntegrityError):
        raise
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "artifact manifest could not be published safely"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            _publication._safe_unlink(
                self._retained_manifests_fd,
                temporary_name,
            )


def _existing_manifest(self, normalized_id: str):
    try:
        return self.load_manifest(normalized_id)
    except FileNotFoundError:
        return None


def _publish_bytes_posix(
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

        existing = _existing_manifest(self, normalized_id)
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
                _publish_manifest_posix(
                    self,
                    manifest=rebound,
                    replace_existing=True,
                )
                _retained._assert_all_continuity(self)
                return rebound
            return existing

        prefix, prefix_identity = _publish_object_posix(
            self,
            digest=digest,
            data=data,
        )
        _assert_prefix_identity(self, prefix, prefix_identity)
        _retained._assert_all_continuity(self)

        manifest = {
            "schema_version": self.SCHEMA_VERSION,
            **immutable,
            "created_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        manifest["manifest_hash"] = _store._manifest_integrity_hash(manifest)
        _publish_manifest_posix(
            self,
            manifest=manifest,
            replace_existing=False,
        )
        _assert_prefix_identity(self, prefix, prefix_identity)
        _retained._assert_all_continuity(self)
        self._verify_manifest_object(manifest)
        return manifest


def install_retained_publication_hardening() -> None:
    if getattr(
        _store.ArtifactStore,
        "_retained_publication_hardening",
        False,
    ):
        return
    _publication._publish_object_posix = _publish_object_posix
    _publication._publish_manifest_posix = _publish_manifest_posix
    _publication._publish_bytes_posix = _publish_bytes_posix
    _store.ArtifactStore._retained_publication_hardening = True
