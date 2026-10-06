from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import importlib
import json
import os
import stat
import sys
from typing import Any
from uuid import uuid4

from . import _namespace_guard as _guard
from . import _retained_namespace as _retained

_store = importlib.import_module(f"{__package__}.store")


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        try:
            written = os.write(descriptor, payload[offset:])
        except OSError as error:
            raise _store.ArtifactIntegrityError(
                "artifact publication staging write failed"
            ) from error
        if written <= 0:
            raise _store.ArtifactIntegrityError(
                "artifact publication staging write made no progress"
            )
        offset += written


def _verify_staged_descriptor(
    descriptor: int,
    *,
    expected_digest: str,
    expected_bytes: int,
) -> None:
    try:
        os.fsync(descriptor)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size != expected_bytes
        ):
            raise _store.ArtifactIntegrityError(
                "staged artifact identity or size changed"
            )
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = sha256()
        copied = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            digest.update(chunk)
    except _store.ArtifactIntegrityError:
        raise
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "staged artifact could not be verified"
        ) from error
    if copied != expected_bytes or digest.hexdigest() != expected_digest:
        raise _store.ArtifactIntegrityError("staged artifact hash changed")


def _open_temp_posix(parent_fd: int, *, prefix: str) -> tuple[str, int]:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if (
        not no_follow
        or os.open not in getattr(os, "supports_dir_fd", set())
        or os.unlink not in getattr(os, "supports_dir_fd", set())
        or os.link not in getattr(os, "supports_dir_fd", set())
    ):
        raise _store.ArtifactIntegrityError(
            "artifact publication lacks descriptor-relative platform support"
        )
    for _attempt in range(32):
        name = f".{prefix}-{uuid4().hex}.tmp"
        try:
            descriptor = os.open(
                name,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | no_follow,
                0o600,
                dir_fd=parent_fd,
            )
        except FileExistsError:
            continue
        except OSError as error:
            raise _store.ArtifactIntegrityError(
                "artifact publication temporary file could not be created safely"
            ) from error
        return name, descriptor
    raise _store.ArtifactIntegrityError(
        "artifact publication could not allocate a unique temporary name"
    )


def _safe_unlink(parent_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


def _sync_directory_fd(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "artifact publication directory durability sync failed"
        ) from error


def _ensure_object_prefix_posix(self, prefix: str) -> int:
    _retained._assert_directory_continuity(
        self,
        ("objects", "sha256"),
        "objects",
    )
    try:
        os.mkdir(prefix, 0o700, dir_fd=self._retained_objects_fd)
        _sync_directory_fd(self._retained_objects_fd)
    except FileExistsError:
        pass
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "artifact object prefix could not be created safely"
        ) from error
    return _guard._open_posix_directory_component(
        self,
        self._retained_objects_fd,
        prefix,
        subject="artifact object prefix",
    )


def _entry_stat(parent_fd: int, name: str):
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "artifact publication target cannot be inspected safely"
        ) from error


def _publish_object_posix(self, *, digest: str, data: bytes) -> None:
    prefix = digest[:2]
    prefix_fd = _ensure_object_prefix_posix(self, prefix)
    try:
        existing = _entry_stat(prefix_fd, digest)
        if existing is not None:
            self._verify_manifest_object(
                {"sha256": f"sha256:{digest}", "bytes": len(data)}
            )
            return

        _retained._assert_directory_continuity(self, ("staging",), "staging")
        temporary_name = None
        descriptor = None
        try:
            temporary_name, descriptor = _open_temp_posix(
                self._retained_staging_fd,
                prefix="artifact",
            )
            _write_all(descriptor, data)
            _verify_staged_descriptor(
                descriptor,
                expected_digest=digest,
                expected_bytes=len(data),
            )
            os.close(descriptor)
            descriptor = None

            _retained._assert_directory_continuity(self, ("staging",), "staging")
            _retained._assert_directory_continuity(
                self,
                ("objects", "sha256"),
                "objects",
            )
            try:
                os.link(
                    temporary_name,
                    digest,
                    src_dir_fd=self._retained_staging_fd,
                    dst_dir_fd=prefix_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                self._verify_manifest_object(
                    {"sha256": f"sha256:{digest}", "bytes": len(data)}
                )
            else:
                _sync_directory_fd(prefix_fd)
            _safe_unlink(self._retained_staging_fd, temporary_name)
            temporary_name = None
            _sync_directory_fd(self._retained_staging_fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_name is not None:
                _safe_unlink(self._retained_staging_fd, temporary_name)
    finally:
        os.close(prefix_fd)


def _publish_manifest_posix(
    self,
    *,
    manifest: dict[str, Any],
    replace_existing: bool,
) -> None:
    name = f"{manifest['artifact_id']}.json"
    payload = _json_bytes(manifest)
    temporary_name = None
    descriptor = None
    try:
        temporary_name, descriptor = _open_temp_posix(
            self._retained_manifests_fd,
            prefix=f"manifest-{manifest['artifact_id']}",
        )
        _write_all(descriptor, payload)
        _verify_staged_descriptor(
            descriptor,
            expected_digest=sha256(payload).hexdigest(),
            expected_bytes=len(payload),
        )
        os.close(descriptor)
        descriptor = None

        _retained._assert_directory_continuity(self, ("manifests",), "manifests")
        if replace_existing:
            os.replace(
                temporary_name,
                name,
                src_dir_fd=self._retained_manifests_fd,
                dst_dir_fd=self._retained_manifests_fd,
            )
            temporary_name = None
        else:
            try:
                os.link(
                    temporary_name,
                    name,
                    src_dir_fd=self._retained_manifests_fd,
                    dst_dir_fd=self._retained_manifests_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                raise _store.ArtifactConflict(
                    "artifact_id became committed during publication"
                )
            _safe_unlink(self._retained_manifests_fd, temporary_name)
            temporary_name = None
        _sync_directory_fd(self._retained_manifests_fd)
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "artifact manifest could not be published safely"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            _safe_unlink(self._retained_manifests_fd, temporary_name)


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

        _publish_object_posix(self, digest=digest, data=data)
        _retained._assert_all_continuity(self)

        manifest = {
            "schema_version": self.SCHEMA_VERSION,
            **immutable,
            "created_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        manifest["manifest_hash"] = _store._manifest_integrity_hash(manifest)
        _publish_manifest_posix(self, manifest=manifest, replace_existing=False)
        _retained._assert_all_continuity(self)
        return manifest


def install_retained_publication_authority() -> None:
    artifact_store = _store.ArtifactStore
    if getattr(artifact_store, "_retained_publication_authority", False):
        return
    original_publish = artifact_store.publish_bytes

    def bound_publish(self, *args, **kwargs):
        if sys.platform == "win32":
            return original_publish(self, *args, **kwargs)
        return _publish_bytes_posix(self, *args, **kwargs)

    artifact_store.publish_bytes = bound_publish
    artifact_store._retained_publication_authority = True
    artifact_store._retained_publication_posix_bound = True
    artifact_store._retained_publication_windows_bound = False
