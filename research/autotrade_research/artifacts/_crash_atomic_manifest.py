from __future__ import annotations

from typing import Any

from . import store as _store

_ORIGINAL_VALIDATE_MANIFEST_CONTRACT = _store.ArtifactStore._validate_manifest_contract.__func__
_ORIGINAL_VERIFY_MANIFEST_INTEGRITY = _store._verify_manifest_integrity
_ORIGINAL_LOAD_MANIFEST = _store.ArtifactStore.load_manifest


def _validate_manifest_contract_v1_v2(
    cls,
    manifest: dict[str, Any],
    *,
    authenticated: bool,
) -> None:
    schema = manifest.get("schema_version")
    if schema == 1:
        if "publication_state" in manifest:
            raise _store.ArtifactIntegrityError(
                "legacy artifact manifest must not contain publication_state"
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
    compatibility = dict(manifest)
    compatibility.pop("publication_state", None)
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
    if (
        manifest.get("schema_version") == 2
        and manifest.get("publication_state") != "COMMITTED"
    ):
        raise _store.ArtifactIntegrityError(
            "artifact manifest publication is not committed"
        )
    return manifest


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
    artifact_store._crash_atomic_manifest_contract = True
