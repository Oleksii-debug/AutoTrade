from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from . import _crash_atomic_manifest as _crash
from . import _namespace_guard as _guard
from . import _retained_namespace as _retained
from . import store as _store
from .durable_publish import sync_parent_directory


def _generation_matches(expected: dict[str, Any], observed: Any) -> bool:
    if sys.platform == "win32":
        return _crash.generation_from_windows_info(observed) == expected
    return _crash.generation_from_posix_stat(observed) == expected


def _open_prefix_authority(self, manifest: dict[str, Any], object_path: Path):
    generation = _crash._validate_generation(manifest.get("object_generation"))
    prefix, digest = _retained._validate_object_name(self, object_path)
    _retained._assert_directory_continuity(self, ("objects", "sha256"), "objects")

    if sys.platform == "win32":
        if generation.get("kind") != "windows":
            raise _store.ArtifactIntegrityError(
                "artifact object generation platform mismatch"
            )
        parent = getattr(self, "_retained_objects_handle", None)
        if not parent:
            raise _store.ArtifactIntegrityError(
                "retained object namespace handle is unavailable"
            )
        authority = _guard._nt_open_relative_handle(
            parent,
            prefix,
            directory=True,
            subject="artifact object generation",
        )
        try:
            opened = _guard._windows_handle_information(
                authority,
                subject="artifact object generation",
            )
            if not _generation_matches(generation, opened):
                raise _store.ArtifactIntegrityError(
                    "artifact object generation changed after publication"
                )
            return authority, opened, digest
        except Exception:
            _guard._close_windows_handle(authority)
            raise

    if generation.get("kind") != "posix":
        raise _store.ArtifactIntegrityError(
            "artifact object generation platform mismatch"
        )
    parent_fd = getattr(self, "_retained_objects_fd", None)
    if parent_fd is None:
        raise _store.ArtifactIntegrityError(
            "retained object namespace descriptor is unavailable"
        )
    authority = _guard._open_posix_directory_component(
        self,
        parent_fd,
        prefix,
        subject="artifact object generation",
    )
    try:
        opened = os.fstat(authority)
        if not _generation_matches(generation, opened):
            raise _store.ArtifactIntegrityError(
                "artifact object generation changed after publication"
            )
        return authority, opened, digest
    except Exception:
        os.close(authority)
        raise


def _close_prefix_authority(authority) -> None:
    if sys.platform == "win32":
        _guard._close_windows_handle(authority)
    else:
        os.close(authority)


def _open_reference_object(self, prefix_authority, digest: str, *, expected_bytes: int):
    if sys.platform == "win32":
        handle = _guard._nt_open_relative_handle(
            prefix_authority,
            digest,
            directory=False,
            subject="artifact object generation reference",
        )
        descriptor = _guard._windows_file_handle_to_descriptor(handle)
        try:
            opened = os.fstat(descriptor)
            if opened.st_nlink != 1 or opened.st_size != expected_bytes:
                raise _store.ArtifactIntegrityError(
                    "artifact object size or link identity mismatch"
                )
            return descriptor, opened
        except Exception:
            os.close(descriptor)
            raise

    descriptor, opened = _retained._retained_posix_file(
        self,
        prefix_authority,
        digest,
        subject="artifact object generation reference",
    )
    if opened.st_size != expected_bytes:
        os.close(descriptor)
        raise _store.ArtifactIntegrityError("artifact object size mismatch")
    return descriptor, opened


def _open_generation_bound_descriptor(
    self,
    manifest: dict[str, Any],
    object_path: Path,
    *,
    expected_bytes: int,
):
    """Open through the canonical seam and prove the file descends from manifest P1.

    The retained prefix is opened and generation-checked before the established
    ArtifactStore object-open seam runs. That keeps existing race/fault injection
    coverage intact. A second reference open through the retained P1 authority is
    then compared to the descriptor returned by the canonical seam, so a transient
    or persistent P1->P2 swap cannot smuggle an equal-bytes object from P2.
    """

    prefix_authority, prefix_opened, digest = _open_prefix_authority(
        self, manifest, object_path
    )
    try:
        descriptor, opened = self._open_object_descriptor(
            object_path,
            expected_bytes=expected_bytes,
        )
        try:
            reference_descriptor, reference = _open_reference_object(
                self,
                prefix_authority,
                digest,
                expected_bytes=expected_bytes,
            )
            try:
                if not self._same_filesystem_entry(opened, reference):
                    # Distinguish a replaced prefix generation from replacement of
                    # the digest entry inside the still-authoritative prefix. The
                    # former fails here as generation drift. For the latter, keep
                    # the original descriptor alive and let canonical revalidation
                    # preserve its established "path changed during descriptor read"
                    # classification after the bounded read.
                    _crash._verify_generation(self, manifest)
            finally:
                os.close(reference_descriptor)
        except Exception:
            os.close(descriptor)
            raise
        return descriptor, opened, prefix_authority, prefix_opened
    except Exception:
        _close_prefix_authority(prefix_authority)
        raise


def _revalidate_generation_bound_descriptor(
    self,
    manifest: dict[str, Any],
    object_path: Path,
    descriptor: int,
    opened,
    prefix_authority,
    prefix_opened,
    *,
    expected_bytes: int,
) -> None:
    # Preserve canonical object-descriptor/path race checks and diagnostics.
    self._revalidate_object_descriptor(
        object_path,
        descriptor,
        opened,
        expected_bytes=expected_bytes,
    )

    generation = _crash._validate_generation(manifest.get("object_generation"))
    if sys.platform == "win32":
        prefix_current = _guard._windows_handle_information(
            prefix_authority,
            subject="artifact object generation",
        )
        if (
            not _generation_matches(generation, prefix_opened)
            or not _generation_matches(generation, prefix_current)
        ):
            raise _store.ArtifactIntegrityError(
                "artifact object generation changed during read"
            )
    else:
        prefix_current = os.fstat(prefix_authority)
        if (
            not self._same_filesystem_entry(prefix_opened, prefix_current)
            or not _generation_matches(generation, prefix_current)
        ):
            raise _store.ArtifactIntegrityError(
                "artifact object generation changed during read"
            )

    # The configured current prefix must still be the generation held for this
    # operation. A transient A->B->A remains safe because object bytes came from
    # the retained A generation; a persistent B cannot become current authority.
    prefix, _digest = _retained._validate_object_name(self, object_path)
    if sys.platform == "win32":
        current = _guard._nt_open_relative_handle(
            self._retained_objects_handle,
            prefix,
            directory=True,
            subject="artifact object generation revalidation",
        )
        try:
            current_info = _guard._windows_handle_information(
                current,
                subject="artifact object generation revalidation",
            )
            if not _generation_matches(generation, current_info):
                raise _store.ArtifactIntegrityError(
                    "artifact object generation changed during read"
                )
        finally:
            _guard._close_windows_handle(current)
    else:
        current = _guard._open_posix_directory_component(
            self,
            self._retained_objects_fd,
            prefix,
            subject="artifact object generation revalidation",
        )
        try:
            current_info = os.fstat(current)
            if not _generation_matches(generation, current_info):
                raise _store.ArtifactIntegrityError(
                    "artifact object generation changed during read"
                )
        finally:
            os.close(current)


def _verify_manifest_object_bound(self, manifest: dict[str, Any]) -> Path:
    if manifest.get("schema_version") != 2:
        return _ORIGINAL_VERIFY_MANIFEST_OBJECT(self, manifest)
    object_path, expected_digest, expected_bytes = _crash._ORIGINAL_MANIFEST_OBJECT_CONTRACT(
        self, manifest
    )
    descriptor, opened, prefix_authority, prefix_opened = (
        _open_generation_bound_descriptor(
            self,
            manifest,
            object_path,
            expected_bytes=expected_bytes,
        )
    )
    try:
        copied = 0
        copied_hash = sha256()
        for chunk in self._bounded_descriptor_chunks(descriptor, expected_bytes):
            copied += len(chunk)
            copied_hash.update(chunk)
        _revalidate_generation_bound_descriptor(
            self,
            manifest,
            object_path,
            descriptor,
            opened,
            prefix_authority,
            prefix_opened,
            expected_bytes=expected_bytes,
        )
    finally:
        os.close(descriptor)
        _close_prefix_authority(prefix_authority)
    if copied != expected_bytes:
        raise _store.ArtifactIntegrityError("artifact object size mismatch")
    if copied_hash.hexdigest() != expected_digest:
        raise _store.ArtifactIntegrityError("artifact object hash mismatch")
    return object_path


def _read_verified_object_bytes_bound(self, manifest: dict[str, Any]) -> bytes:
    if manifest.get("schema_version") != 2:
        return _ORIGINAL_READ_VERIFIED_OBJECT_BYTES(self, manifest)
    object_path, expected_digest, expected_bytes = _crash._ORIGINAL_MANIFEST_OBJECT_CONTRACT(
        self, manifest
    )
    descriptor, opened, prefix_authority, prefix_opened = (
        _open_generation_bound_descriptor(
            self,
            manifest,
            object_path,
            expected_bytes=expected_bytes,
        )
    )
    try:
        chunks: list[bytes] = []
        copied = 0
        copied_hash = sha256()
        for chunk in self._bounded_descriptor_chunks(descriptor, expected_bytes):
            chunks.append(chunk)
            copied += len(chunk)
            copied_hash.update(chunk)
        _revalidate_generation_bound_descriptor(
            self,
            manifest,
            object_path,
            descriptor,
            opened,
            prefix_authority,
            prefix_opened,
            expected_bytes=expected_bytes,
        )
    finally:
        os.close(descriptor)
        _close_prefix_authority(prefix_authority)
    if copied != expected_bytes or copied_hash.hexdigest() != expected_digest:
        raise _store.ArtifactIntegrityError("artifact object changed during read")
    return b"".join(chunks)


def _export_bound(self, artifact_id: str, destination: str | Path) -> Path:
    manifest = self.load_manifest(artifact_id)
    _store._verify_manifest_integrity(manifest, required=True)
    if manifest.get("schema_version") != 2:
        return _ORIGINAL_EXPORT(self, artifact_id, destination)
    if manifest.get("rights", {}).get("export") is not True:
        raise PermissionError("artifact rights do not permit export")
    if self._export_authorizer is None:
        raise PermissionError("independent export authorization is required")

    source, expected_digest, expected_bytes = _crash._ORIGINAL_MANIFEST_OBJECT_CONTRACT(
        self, manifest
    )
    descriptor, opened, prefix_authority, prefix_opened = (
        _open_generation_bound_descriptor(
            self,
            manifest,
            source,
            expected_bytes=expected_bytes,
        )
    )
    try:
        try:
            authorized = self._export_authorizer(
                manifest["artifact_id"], manifest["sha256"]
            )
        except Exception as error:
            raise PermissionError(
                "independent export authorization failed closed"
            ) from error
        if authorized is not True:
            raise PermissionError(
                "independent export authority does not permit export"
            )

        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                copied = 0
                copied_hash = sha256()
                for chunk in self._bounded_descriptor_chunks(descriptor, expected_bytes):
                    handle.write(chunk)
                    copied += len(chunk)
                    copied_hash.update(chunk)
                _revalidate_generation_bound_descriptor(
                    self,
                    manifest,
                    source,
                    descriptor,
                    opened,
                    prefix_authority,
                    prefix_opened,
                    expected_bytes=expected_bytes,
                )
                handle.flush()
                os.fsync(handle.fileno())
            if copied != expected_bytes or copied_hash.hexdigest() != expected_digest:
                raise _store.ArtifactIntegrityError(
                    "artifact object changed during export copy"
                )
            os.replace(temporary, target)
            temporary = None
            sync_parent_directory(target)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        return target
    finally:
        os.close(descriptor)
        _close_prefix_authority(prefix_authority)


def install_generation_bound_reads() -> None:
    artifact_store = _store.ArtifactStore
    if getattr(artifact_store, "_generation_bound_reads", False):
        return
    global _ORIGINAL_VERIFY_MANIFEST_OBJECT
    global _ORIGINAL_READ_VERIFIED_OBJECT_BYTES
    global _ORIGINAL_EXPORT
    _ORIGINAL_VERIFY_MANIFEST_OBJECT = artifact_store._verify_manifest_object
    _ORIGINAL_READ_VERIFIED_OBJECT_BYTES = artifact_store._read_verified_object_bytes
    _ORIGINAL_EXPORT = artifact_store.export
    artifact_store._verify_manifest_object = _verify_manifest_object_bound
    artifact_store._read_verified_object_bytes = _read_verified_object_bytes_bound
    artifact_store.export = _export_bound
    artifact_store._generation_bound_reads = True
