from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any

from . import _namespace_guard as _guard
from . import _retained_namespace as _retained
from . import store as _store

_ORIGINAL_VALIDATE_MANIFEST_CONTRACT = _store.ArtifactStore._validate_manifest_contract.__func__
_ORIGINAL_VERIFY_MANIFEST_INTEGRITY = _store._verify_manifest_integrity
_ORIGINAL_LOAD_MANIFEST = _store.ArtifactStore.load_manifest
_ORIGINAL_MANIFEST_OBJECT_CONTRACT = _store.ArtifactStore._manifest_object_contract
_ORIGINAL_VERIFY_MANIFEST_OBJECT = _store.ArtifactStore._verify_manifest_object
_ORIGINAL_READ_VERIFIED_OBJECT_BYTES = _store.ArtifactStore._read_verified_object_bytes
_ORIGINAL_EXPORT = _store.ArtifactStore.export


def _validate_generation(generation: Any) -> dict[str, Any]:
    if type(generation) is not dict:
        raise _store.ArtifactIntegrityError(
            "artifact manifest object_generation is invalid"
        )
    kind = generation.get("kind")
    if kind == "posix":
        if set(generation) != {"kind", "device", "inode"}:
            raise _store.ArtifactIntegrityError(
                "artifact manifest POSIX object_generation is invalid"
            )
        if (
            type(generation.get("device")) is not int
            or generation["device"] < 0
            or type(generation.get("inode")) is not int
            or generation["inode"] <= 0
        ):
            raise _store.ArtifactIntegrityError(
                "artifact manifest POSIX object_generation is invalid"
            )
        return generation
    if kind == "windows":
        required = {"kind", "volume_serial", "file_index_high", "file_index_low"}
        if set(generation) != required:
            raise _store.ArtifactIntegrityError(
                "artifact manifest Windows object_generation is invalid"
            )
        for field in required - {"kind"}:
            if type(generation.get(field)) is not int or generation[field] < 0:
                raise _store.ArtifactIntegrityError(
                    "artifact manifest Windows object_generation is invalid"
                )
        return generation
    raise _store.ArtifactIntegrityError(
        "artifact manifest object_generation kind is invalid"
    )


def _validate_manifest_contract_v1_v2(
    cls,
    manifest: dict[str, Any],
    *,
    authenticated: bool,
) -> None:
    schema = manifest.get("schema_version")
    if schema == 1:
        if "publication_state" in manifest or "object_generation" in manifest:
            raise _store.ArtifactIntegrityError(
                "legacy artifact manifest must not contain v2 publication fields"
            )
        compatibility = dict(manifest)
        compatibility["schema_version"] = 2
        return _ORIGINAL_VALIDATE_MANIFEST_CONTRACT(
            cls,
            compatibility,
            authenticated=authenticated,
        )
    if schema != 2:
        raise _store.ArtifactIntegrityError(
            "artifact manifest schema_version is unsupported"
        )
    publication_state = manifest.get("publication_state")
    if publication_state not in {"PREPARED", "COMMITTED"}:
        raise _store.ArtifactIntegrityError(
            "artifact manifest publication_state is invalid"
        )
    _validate_generation(manifest.get("object_generation"))
    compatibility = dict(manifest)
    compatibility.pop("publication_state", None)
    compatibility.pop("object_generation", None)
    return _ORIGINAL_VALIDATE_MANIFEST_CONTRACT(
        cls,
        compatibility,
        authenticated=authenticated,
    )


def _verify_manifest_integrity_v2_commit(
    manifest: dict[str, Any],
    *,
    required: bool,
) -> bool:
    verified = _ORIGINAL_VERIFY_MANIFEST_INTEGRITY(
        manifest,
        required=required,
    )
    if (
        required
        and manifest.get("schema_version") == 2
        and manifest.get("publication_state") != "COMMITTED"
    ):
        raise _store.ArtifactIntegrityError(
            "artifact manifest publication is not committed"
        )
    return verified


def _load_manifest_committed(self, artifact_id: str):
    manifest = _ORIGINAL_LOAD_MANIFEST(self, artifact_id)
    if manifest.get("schema_version") == 2:
        if manifest.get("publication_state") != "COMMITTED":
            raise _store.ArtifactIntegrityError(
                "artifact manifest publication is not committed"
            )
        _store._verify_manifest_integrity(manifest, required=True)
    _verify_generation(self, manifest)
    return manifest


def generation_from_posix_stat(entry) -> dict[str, Any]:
    return {
        "kind": "posix",
        "device": int(entry.st_dev),
        "inode": int(entry.st_ino),
    }


def generation_from_windows_info(entry) -> dict[str, Any]:
    return {
        "kind": "windows",
        "volume_serial": int(entry.dwVolumeSerialNumber),
        "file_index_high": int(entry.nFileIndexHigh),
        "file_index_low": int(entry.nFileIndexLow),
    }


def _verify_generation(self, manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 2:
        return
    generation = _validate_generation(manifest.get("object_generation"))
    digest_value = manifest.get("sha256")
    if not isinstance(digest_value, str) or len(digest_value) != 71:
        raise _store.ArtifactIntegrityError("manifest digest is invalid")
    prefix = digest_value[7:9]
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
        handle = _guard._nt_open_relative_handle(
            parent,
            prefix,
            directory=True,
            subject="artifact object generation",
        )
        try:
            current = _guard._windows_handle_information(
                handle,
                subject="artifact object generation",
            )
            if generation_from_windows_info(current) != generation:
                raise _store.ArtifactIntegrityError(
                    "artifact object generation changed after publication"
                )
        finally:
            _guard._close_windows_handle(handle)
        return

    if generation.get("kind") != "posix":
        raise _store.ArtifactIntegrityError(
            "artifact object generation platform mismatch"
        )
    parent_fd = getattr(self, "_retained_objects_fd", None)
    if parent_fd is None:
        raise _store.ArtifactIntegrityError(
            "retained object namespace descriptor is unavailable"
        )
    descriptor = _guard._open_posix_directory_component(
        self,
        parent_fd,
        prefix,
        subject="artifact object generation",
    )
    try:
        current = os.fstat(descriptor)
        if generation_from_posix_stat(current) != generation:
            raise _store.ArtifactIntegrityError(
                "artifact object generation changed after publication"
            )
    finally:
        os.close(descriptor)


def _manifest_object_contract_v2(self, manifest: dict[str, Any]):
    _verify_generation(self, manifest)
    return _ORIGINAL_MANIFEST_OBJECT_CONTRACT(self, manifest)


def _open_generation_bound_object(
    self,
    manifest: dict[str, Any],
):
    object_path, expected_digest, expected_bytes = _ORIGINAL_MANIFEST_OBJECT_CONTRACT(
        self,
        manifest,
    )
    generation = _validate_generation(manifest.get("object_generation"))
    prefix, digest = _retained._validate_object_name(self, object_path)
    _retained._assert_directory_continuity(
        self,
        ("objects", "sha256"),
        "objects",
    )

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
        prefix_authority = _guard._nt_open_relative_handle(
            parent,
            prefix,
            directory=True,
            subject="artifact object prefix",
        )
        try:
            prefix_info = _guard._windows_handle_information(
                prefix_authority,
                subject="artifact object generation",
            )
            if generation_from_windows_info(prefix_info) != generation:
                raise _store.ArtifactIntegrityError(
                    "artifact object generation changed after publication"
                )
            handle = _guard._nt_open_relative_handle(
                prefix_authority,
                digest,
                directory=False,
                subject="artifact object",
            )
            descriptor = _guard._windows_file_handle_to_descriptor(handle)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_size != expected_bytes
            ):
                os.close(descriptor)
                raise _store.ArtifactIntegrityError(
                    "artifact object size or link identity mismatch"
                )
        except Exception:
            _guard._close_windows_handle(prefix_authority)
            raise
        return (
            object_path,
            expected_digest,
            expected_bytes,
            generation,
            descriptor,
            opened,
            prefix_authority,
        )

    if generation.get("kind") != "posix":
        raise _store.ArtifactIntegrityError(
            "artifact object generation platform mismatch"
        )
    parent_fd = getattr(self, "_retained_objects_fd", None)
    if parent_fd is None:
        raise _store.ArtifactIntegrityError(
            "retained object namespace descriptor is unavailable"
        )
    prefix_authority = _guard._open_posix_directory_component(
        self,
        parent_fd,
        prefix,
        subject="artifact object prefix",
    )
    try:
        if generation_from_posix_stat(os.fstat(prefix_authority)) != generation:
            raise _store.ArtifactIntegrityError(
                "artifact object generation changed after publication"
            )
        descriptor, opened = _retained._retained_posix_file(
            self,
            prefix_authority,
            digest,
            subject="artifact object",
        )
        if opened.st_size != expected_bytes:
            os.close(descriptor)
            raise _store.ArtifactIntegrityError("artifact object size mismatch")
    except Exception:
        os.close(prefix_authority)
        raise
    return (
        object_path,
        expected_digest,
        expected_bytes,
        generation,
        descriptor,
        opened,
        prefix_authority,
    )


def _close_prefix_authority(prefix_authority: Any) -> None:
    if sys.platform == "win32":
        _guard._close_windows_handle(prefix_authority)
    else:
        os.close(prefix_authority)


def _revalidate_generation_bound_object(
    self,
    *,
    object_path: Path,
    descriptor: int,
    opened: os.stat_result,
    expected_bytes: int,
    expected_generation: dict[str, Any],
    prefix_authority: Any,
) -> None:
    self._revalidate_object_descriptor(
        object_path,
        descriptor,
        opened,
        expected_bytes=expected_bytes,
    )
    if sys.platform == "win32":
        current = _guard._windows_handle_information(
            prefix_authority,
            subject="artifact object generation",
        )
        actual = generation_from_windows_info(current)
    else:
        actual = generation_from_posix_stat(os.fstat(prefix_authority))
    if actual != expected_generation:
        raise _store.ArtifactIntegrityError(
            "artifact object generation changed during read"
        )


def _verify_manifest_object_generation_bound(self, manifest: dict[str, Any]) -> Path:
    if manifest.get("schema_version") != 2:
        return _ORIGINAL_VERIFY_MANIFEST_OBJECT(self, manifest)
    (
        object_path,
        expected_digest,
        expected_bytes,
        generation,
        descriptor,
        opened,
        prefix_authority,
    ) = _open_generation_bound_object(self, manifest)
    try:
        copied = 0
        copied_hash = sha256()
        for chunk in self._bounded_descriptor_chunks(
            descriptor,
            expected_bytes,
        ):
            copied += len(chunk)
            copied_hash.update(chunk)
        _revalidate_generation_bound_object(
            self,
            object_path=object_path,
            descriptor=descriptor,
            opened=opened,
            expected_bytes=expected_bytes,
            expected_generation=generation,
            prefix_authority=prefix_authority,
        )
    finally:
        os.close(descriptor)
        _close_prefix_authority(prefix_authority)
    if copied != expected_bytes:
        raise _store.ArtifactIntegrityError("artifact object size mismatch")
    if copied_hash.hexdigest() != expected_digest:
        raise _store.ArtifactIntegrityError("artifact object hash mismatch")
    return object_path


def _read_verified_object_bytes_generation_bound(
    self,
    manifest: dict[str, Any],
) -> bytes:
    if manifest.get("schema_version") != 2:
        return _ORIGINAL_READ_VERIFIED_OBJECT_BYTES(self, manifest)
    (
        object_path,
        expected_digest,
        expected_bytes,
        generation,
        descriptor,
        opened,
        prefix_authority,
    ) = _open_generation_bound_object(self, manifest)
    try:
        chunks: list[bytes] = []
        copied = 0
        copied_hash = sha256()
        for chunk in self._bounded_descriptor_chunks(
            descriptor,
            expected_bytes,
        ):
            chunks.append(chunk)
            copied += len(chunk)
            copied_hash.update(chunk)
        _revalidate_generation_bound_object(
            self,
            object_path=object_path,
            descriptor=descriptor,
            opened=opened,
            expected_bytes=expected_bytes,
            expected_generation=generation,
            prefix_authority=prefix_authority,
        )
    finally:
        os.close(descriptor)
        _close_prefix_authority(prefix_authority)
    if copied != expected_bytes or copied_hash.hexdigest() != expected_digest:
        raise _store.ArtifactIntegrityError("artifact object changed during read")
    return b"".join(chunks)


def _export_generation_bound(self, artifact_id: str, destination: str | Path) -> Path:
    manifest = self.load_manifest(artifact_id)
    if manifest.get("schema_version") != 2:
        return _ORIGINAL_EXPORT(self, artifact_id, destination)
    _store._verify_manifest_integrity(manifest, required=True)
    if manifest.get("rights", {}).get("export") is not True:
        raise PermissionError("artifact rights do not permit export")
    if self._export_authorizer is None:
        raise PermissionError("independent export authorization is required")

    (
        source,
        expected_digest,
        expected_bytes,
        generation,
        descriptor,
        opened,
        prefix_authority,
    ) = _open_generation_bound_object(self, manifest)
    try:
        try:
            authorized = self._export_authorizer(
                manifest["artifact_id"],
                manifest["sha256"],
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
                for chunk in self._bounded_descriptor_chunks(
                    descriptor,
                    expected_bytes,
                ):
                    handle.write(chunk)
                    copied += len(chunk)
                    copied_hash.update(chunk)
                _revalidate_generation_bound_object(
                    self,
                    object_path=source,
                    descriptor=descriptor,
                    opened=opened,
                    expected_bytes=expected_bytes,
                    expected_generation=generation,
                    prefix_authority=prefix_authority,
                )
                handle.flush()
                os.fsync(handle.fileno())
            if (
                copied != expected_bytes
                or copied_hash.hexdigest() != expected_digest
            ):
                raise _store.ArtifactIntegrityError(
                    "artifact object changed during export copy"
                )
            os.replace(temporary, target)
            temporary = None
            _store.sync_parent_directory(target)
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


def install_crash_atomic_manifest_contract() -> None:
    artifact_store = _store.ArtifactStore
    if getattr(artifact_store, "_crash_atomic_manifest_contract", False):
        return
    artifact_store.SCHEMA_VERSION = 2
    artifact_store._validate_manifest_contract = classmethod(
        _validate_manifest_contract_v1_v2
    )
    _store._verify_manifest_integrity = _verify_manifest_integrity_v2_commit
    artifact_store.load_manifest = _load_manifest_committed
    artifact_store._manifest_object_contract = _manifest_object_contract_v2
    artifact_store._verify_manifest_object = _verify_manifest_object_generation_bound
    artifact_store._read_verified_object_bytes = (
        _read_verified_object_bytes_generation_bound
    )
    artifact_store.export = _export_generation_bound
    artifact_store._crash_atomic_manifest_contract = True
