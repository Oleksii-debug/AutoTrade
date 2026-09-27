from __future__ import annotations

import os
import sys
from typing import Any

from . import _namespace_guard as _guard
from . import store as _store

_ORIGINAL_VALIDATE_MANIFEST_CONTRACT = _store.ArtifactStore._validate_manifest_contract.__func__
_ORIGINAL_VERIFY_MANIFEST_INTEGRITY = _store._verify_manifest_integrity
_ORIGINAL_LOAD_MANIFEST = _store.ArtifactStore.load_manifest
_ORIGINAL_MANIFEST_OBJECT_CONTRACT = _store.ArtifactStore._manifest_object_contract


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
        # Schema v2 is an authenticated publication protocol. A hashless v2
        # record must never become metadata authority merely because its state
        # string says COMMITTED; legacy hashless compatibility is v1-only.
        _store._verify_manifest_integrity(manifest, required=True)
    # A durable COMMITTED marker is not sufficient authority by itself. Rebind
    # the manifest to the exact retained object-prefix generation before any
    # caller can observe it as committed metadata after a process/power crash.
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
    artifact_store._crash_atomic_manifest_contract = True
