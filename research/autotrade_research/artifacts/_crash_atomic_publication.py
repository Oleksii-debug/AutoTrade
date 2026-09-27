from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import os
import sys
from typing import Any

from . import _crash_atomic_manifest as _contract
from . import _namespace_guard as _guard
from . import _publication_contract_compat as _compat
from . import _retained_namespace as _retained
from . import _retained_publication_hardening as _posix
from . import _windows_retained_publication_hardening as _windows

_store = _posix._store


def _raw_existing_manifest(self, artifact_id: str):
    try:
        manifest = self._load_manifest_path(self._manifest_path(artifact_id))
    except FileNotFoundError:
        return None
    if manifest.get("artifact_id") != artifact_id:
        raise _store.ArtifactIntegrityError("manifest artifact identity mismatch")
    return manifest


def _immutable_inputs(
    *, artifact_id: str, digest: str, data: bytes, media_type: str,
    rights: dict[str, Any], source_refs: list[str], metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "sha256": f"sha256:{digest}",
        "bytes": len(data),
        "media_type": media_type,
        "rights": rights,
        "source_refs": source_refs,
        "metadata": metadata,
    }


def _prepared_manifest(immutable, generation):
    manifest = {
        "schema_version": 2,
        **immutable,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "publication_state": "PREPARED",
        "object_generation": generation,
    }
    manifest["manifest_hash"] = _store._manifest_integrity_hash(manifest)
    return manifest


def _committed_manifest(prepared):
    committed = dict(prepared)
    committed["publication_state"] = "COMMITTED"
    committed["manifest_hash"] = _store._manifest_integrity_hash(committed)
    return committed


def _commit_prepared(self, prepared: dict[str, Any], *, windows: bool):
    """Resume authenticated PREPARED state without unlink-by-name cleanup."""
    _contract._verify_generation(self, prepared)
    self._verify_manifest_object(prepared)
    _retained._assert_all_continuity(self)
    committed = _committed_manifest(prepared)
    publisher = (
        _windows._publish_manifest_windows
        if windows
        else _posix._publish_manifest_posix
    )
    publisher(self, manifest=committed, replace_existing=True)
    _retained._assert_all_continuity(self)
    self._verify_manifest_object(committed)
    return committed


def _upgrade_hashless_legacy_posix(
    self,
    existing: dict[str, Any],
    immutable: dict[str, Any],
):
    """Actively migrate hashless v1 to v2 under one retained prefix generation."""
    digest = existing["sha256"].removeprefix("sha256:")
    prefix = digest[:2]
    prefix_fd = _guard._open_posix_directory_component(
        self,
        self._retained_objects_fd,
        prefix,
        subject="legacy artifact object prefix",
    )
    try:
        prefix_identity = os.fstat(prefix_fd)
        generation = _contract.generation_from_posix_stat(prefix_identity)
        _posix._verify_bound_object_bytes(
            self,
            prefix_fd,
            digest,
            expected_bytes=existing["bytes"],
        )
        _posix._assert_prefix_identity(self, prefix, prefix_identity)
        _retained._assert_all_continuity(self)

        prepared = _prepared_manifest(immutable, generation)
        _posix._publish_manifest_posix(
            self,
            manifest=prepared,
            replace_existing=True,
        )
        _posix._verify_bound_object_bytes(
            self,
            prefix_fd,
            digest,
            expected_bytes=existing["bytes"],
        )
        _posix._assert_prefix_identity(self, prefix, prefix_identity)
        _retained._assert_all_continuity(self)

        committed = _committed_manifest(prepared)
        _posix._publish_manifest_posix(
            self,
            manifest=committed,
            replace_existing=True,
        )
        _posix._assert_prefix_identity(self, prefix, prefix_identity)
        _retained._assert_all_continuity(self)
        self._verify_manifest_object(committed)
        return committed
    finally:
        os.close(prefix_fd)


def _upgrade_hashless_legacy_windows(
    self,
    existing: dict[str, Any],
    immutable: dict[str, Any],
):
    digest = existing["sha256"].removeprefix("sha256:")
    prefix_name = digest[:2]
    parent = getattr(self, "_retained_objects_handle", None)
    if not parent:
        raise _store.ArtifactIntegrityError(
            "retained object namespace handle is unavailable"
        )
    prefix_handle = _guard._nt_open_relative_handle(
        parent,
        prefix_name,
        directory=True,
        subject="legacy artifact object prefix",
    )
    try:
        prefix_identity = _guard._windows_handle_information(
            prefix_handle,
            subject="legacy artifact object prefix",
        )
        generation = _contract.generation_from_windows_info(prefix_identity)
        _windows._verify_bound_windows_object(
            self,
            prefix_handle,
            digest,
            expected_bytes=existing["bytes"],
        )
        _windows._assert_windows_prefix_identity(
            self,
            prefix_name,
            prefix_identity,
        )
        _retained._assert_all_continuity(self)

        prepared = _prepared_manifest(immutable, generation)
        _windows._publish_manifest_windows(
            self,
            manifest=prepared,
            replace_existing=True,
        )
        _windows._verify_bound_windows_object(
            self,
            prefix_handle,
            digest,
            expected_bytes=existing["bytes"],
        )
        _windows._assert_windows_prefix_identity(
            self,
            prefix_name,
            prefix_identity,
        )
        _retained._assert_all_continuity(self)

        committed = _committed_manifest(prepared)
        _windows._publish_manifest_windows(
            self,
            manifest=committed,
            replace_existing=True,
        )
        _windows._assert_windows_prefix_identity(
            self,
            prefix_name,
            prefix_identity,
        )
        _retained._assert_all_continuity(self)
        self._verify_manifest_object(committed)
        return committed
    finally:
        _guard._close_windows_handle(prefix_handle)


def _upgrade_hashless_legacy(self, existing, immutable, *, windows: bool):
    if windows:
        return _upgrade_hashless_legacy_windows(self, existing, immutable)
    return _upgrade_hashless_legacy_posix(self, existing, immutable)


def _handle_existing(self, existing, immutable, *, windows: bool):
    if existing is None:
        return None

    schema = existing.get("schema_version")
    if schema == 2:
        # Every v2 record is authenticated transaction state before it can
        # influence retry or recovery behavior.
        _contract._ORIGINAL_VERIFY_MANIFEST_INTEGRITY(existing, required=True)

    if any(existing.get(key) != value for key, value in immutable.items()):
        raise _store.ArtifactConflict(
            "artifact_id is already committed with different content or metadata"
        )

    if schema == 2 and existing.get("publication_state") == "PREPARED":
        return _commit_prepared(self, existing, windows=windows)

    authenticated = _store._verify_manifest_integrity(existing, required=False)
    if not authenticated:
        # v1 is grandfathered for reading old authenticated evidence, but an
        # active hashless rebind is a new authority transition. Migrate it to
        # generation-bound v2 instead of minting fresh legacy authority.
        return _upgrade_hashless_legacy(
            self,
            existing,
            immutable,
            windows=windows,
        )

    self._verify_manifest_object(existing)
    _store._verify_manifest_integrity(existing, required=True)
    return existing


def _validated_inputs(self, *, artifact_id, data, media_type, rights, source_refs, metadata):
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
    immutable = _immutable_inputs(
        artifact_id=normalized_id,
        digest=digest,
        data=data,
        media_type=media_type,
        rights=normalized_rights,
        source_refs=sources,
        metadata=meta,
    )
    return normalized_id, digest, immutable


def _publish_posix(
    self, *, artifact_id: str, data: bytes, media_type: str,
    rights: dict[str, Any], source_refs: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
):
    normalized_id, digest, immutable = _validated_inputs(
        self,
        artifact_id=artifact_id,
        data=data,
        media_type=media_type,
        rights=rights,
        source_refs=source_refs,
        metadata=metadata,
    )
    manifest_path = self._manifest_path(normalized_id)
    self._validate_manifest_namespace(manifest_path)
    self._validate_staging_namespace()
    _retained._assert_all_continuity(self)
    handled = _handle_existing(
        self,
        _raw_existing_manifest(self, normalized_id),
        immutable,
        windows=False,
    )
    if handled is not None:
        return handled

    prefix, prefix_identity = _posix._publish_object_posix(
        self, digest=digest, data=data
    )
    generation = _contract.generation_from_posix_stat(prefix_identity)
    _posix._assert_prefix_identity(self, prefix, prefix_identity)
    _retained._assert_all_continuity(self)

    prepared = _prepared_manifest(immutable, generation)
    _posix._publish_manifest_posix(
        self, manifest=prepared, replace_existing=False
    )
    _posix._assert_prefix_identity(self, prefix, prefix_identity)
    _retained._assert_all_continuity(self)
    self._verify_manifest_object(prepared)

    committed = _committed_manifest(prepared)
    try:
        _posix._publish_manifest_posix(
            self, manifest=committed, replace_existing=True
        )
        _retained._assert_all_continuity(self)
        self._verify_manifest_object(committed)
        return committed
    except _compat.LegacyPostCommitFault:
        raise
    except BaseException:
        # PREPARED/COMMITTED bytes are restart-verifiable transaction evidence;
        # correctness no longer depends on unlink-by-name rollback.
        raise


def _publish_windows(
    self, *, artifact_id: str, data: bytes, media_type: str,
    rights: dict[str, Any], source_refs: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
):
    normalized_id, digest, immutable = _validated_inputs(
        self,
        artifact_id=artifact_id,
        data=data,
        media_type=media_type,
        rights=rights,
        source_refs=source_refs,
        metadata=metadata,
    )
    manifest_path = self._manifest_path(normalized_id)
    self._validate_manifest_namespace(manifest_path)
    self._validate_staging_namespace()
    _retained._assert_all_continuity(self)
    handled = _handle_existing(
        self,
        _raw_existing_manifest(self, normalized_id),
        immutable,
        windows=True,
    )
    if handled is not None:
        return handled

    prefix_name, prefix_identity, prefix_handle = (
        _windows._publish_object_windows_bound(
            self, digest=digest, data=data
        )
    )
    try:
        generation = _contract.generation_from_windows_info(prefix_identity)
        _windows._assert_windows_prefix_identity(
            self, prefix_name, prefix_identity
        )
        _retained._assert_all_continuity(self)
        prepared = _prepared_manifest(immutable, generation)
        _windows._publish_manifest_windows(
            self, manifest=prepared, replace_existing=False
        )
        _windows._verify_bound_windows_object(
            self, prefix_handle, digest, expected_bytes=len(data)
        )
        _windows._assert_windows_prefix_identity(
            self, prefix_name, prefix_identity
        )
        _retained._assert_all_continuity(self)
        self._verify_manifest_object(prepared)

        committed = _committed_manifest(prepared)
        try:
            _windows._publish_manifest_windows(
                self, manifest=committed, replace_existing=True
            )
            _retained._assert_all_continuity(self)
            self._verify_manifest_object(committed)
            return committed
        except _compat.LegacyPostCommitFault:
            raise
        except BaseException:
            raise
    finally:
        _guard._close_windows_handle(prefix_handle)


def install_crash_atomic_publication() -> None:
    artifact_store = _store.ArtifactStore
    if getattr(artifact_store, "_crash_atomic_publication", False):
        return

    def publish(self, *args, **kwargs):
        if sys.platform == "win32":
            return _publish_windows(self, *args, **kwargs)
        return _publish_posix(self, *args, **kwargs)

    artifact_store.publish_bytes = publish
    artifact_store._crash_atomic_publication = True
