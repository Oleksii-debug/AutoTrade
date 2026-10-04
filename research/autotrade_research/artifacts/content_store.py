"""Legacy content-hash artifact-store compatibility surface.

This module predates the canonical rights-bound WP-06 store in store.py.
It is retained only to characterize or migrate the earlier on-disk format.
New production code must import ArtifactStore from autotrade_research.artifacts
and must not establish this module as a second artifact authority.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from .durable_publish import atomic_write_json, durable_path_lock, sha256_file
from ..io.strict_json import strict_json_loads


class ArtifactStoreError(RuntimeError):
    """Base error for immutable artifact-store failures."""


class ArtifactIntegrityError(ArtifactStoreError):
    """Raised when stored bytes or manifest evidence no longer match."""


class ArtifactConflictError(ArtifactStoreError):
    """Raised when immutable metadata conflicts with an existing artifact."""


@dataclass(frozen=True)
class ArtifactManifest:
    schema_version: int
    algorithm: str
    digest: str
    size_bytes: int
    media_type: str
    rights_basis: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sync_parent_directory(path: Path) -> None:
    if os.name == "nt":
        return
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        with durable_path_lock(path):
            os.replace(temporary, path)
            temporary = None
            _sync_parent_directory(path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _require_digest(digest: str) -> str:
    normalized = digest.lower()
    if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise ValueError("artifact digest must be a 64-character SHA-256 hex value")
    return normalized


class ArtifactStore:
    """Immutable content-addressed research/evidence artifact store.

    The store deliberately records a rights basis before publication/export.
    It is not a financial ledger and must not be used to authorize trading.
    """

    MANIFEST_KEYS = {
        "schema_version",
        "algorithm",
        "digest",
        "size_bytes",
        "media_type",
        "rights_basis",
    }

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.objects_root = self.root / "objects" / "sha256"
        self.manifests_root = self.root / "manifests" / "sha256"
        self._guard_path = self.root / "store.guard"

    def _object_path(self, digest: str) -> Path:
        value = _require_digest(digest)
        return self.objects_root / value[:2] / value

    def _manifest_path(self, digest: str) -> Path:
        value = _require_digest(digest)
        return self.manifests_root / value[:2] / f"{value}.json"

    @staticmethod
    def _build_manifest(payload: bytes, media_type: str, rights_basis: str) -> ArtifactManifest:
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        if not media_type or not media_type.strip():
            raise ValueError("media_type is required")
        if not rights_basis or not rights_basis.strip():
            raise ValueError("rights_basis is required")
        digest = hashlib.sha256(payload).hexdigest()
        return ArtifactManifest(
            schema_version=1,
            algorithm="sha256",
            digest=digest,
            size_bytes=len(payload),
            media_type=media_type.strip(),
            rights_basis=rights_basis.strip(),
        )

    def put_bytes(self, payload: bytes, *, media_type: str, rights_basis: str) -> ArtifactManifest:
        manifest = self._build_manifest(payload, media_type, rights_basis)
        object_path = self._object_path(manifest.digest)
        manifest_path = self._manifest_path(manifest.digest)

        with durable_path_lock(self._guard_path):
            if object_path.exists():
                if not object_path.is_file() or sha256_file(object_path) != manifest.digest:
                    raise ArtifactIntegrityError("existing artifact object does not match its digest")
            else:
                _atomic_write_bytes(object_path, payload)

            if manifest_path.exists():
                existing = self.read_manifest(manifest.digest)
                if existing != manifest:
                    raise ArtifactConflictError("immutable artifact manifest conflicts with requested metadata")
            else:
                atomic_write_json(manifest_path, manifest.to_dict())

            self.verify(manifest.digest)
        return manifest

    def put_file(self, source: str | Path, *, media_type: str, rights_basis: str) -> ArtifactManifest:
        source_path = Path(source)
        return self.put_bytes(source_path.read_bytes(), media_type=media_type, rights_basis=rights_basis)

    def read_manifest(self, digest: str) -> ArtifactManifest:
        manifest_path = self._manifest_path(digest)
        try:
            payload = strict_json_loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            raise ArtifactIntegrityError("artifact manifest cannot be read safely") from error
        if not isinstance(payload, dict) or set(payload) != self.MANIFEST_KEYS:
            raise ArtifactIntegrityError("artifact manifest structure is invalid")
        if (
            payload.get("schema_version") != 1
            or payload.get("algorithm") != "sha256"
            or payload.get("digest") != _require_digest(digest)
            or not isinstance(payload.get("size_bytes"), int)
            or payload["size_bytes"] < 0
            or not isinstance(payload.get("media_type"), str)
            or not payload["media_type"].strip()
            or not isinstance(payload.get("rights_basis"), str)
            or not payload["rights_basis"].strip()
        ):
            raise ArtifactIntegrityError("artifact manifest values are invalid")
        return ArtifactManifest(**payload)

    def verify(self, digest: str) -> ArtifactManifest:
        normalized = _require_digest(digest)
        manifest = self.read_manifest(normalized)
        object_path = self._object_path(normalized)
        if not object_path.is_file():
            raise ArtifactIntegrityError("artifact object is missing")
        try:
            size = object_path.stat().st_size
        except OSError as error:
            raise ArtifactIntegrityError("artifact object cannot be inspected") from error
        if size != manifest.size_bytes or sha256_file(object_path) != normalized:
            raise ArtifactIntegrityError("artifact object does not match the immutable manifest")
        return manifest

    def export(self, digest: str, destination: str | Path) -> Path:
        manifest = self.verify(digest)
        if not manifest.rights_basis.strip():
            raise ArtifactIntegrityError("artifact has no export rights basis")
        source = self._object_path(manifest.digest)
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
                with source.open("rb") as source_handle:
                    shutil.copyfileobj(source_handle, handle)
                handle.flush()
                os.fsync(handle.fileno())
            with durable_path_lock(target):
                os.replace(temporary, target)
                temporary = None
                _sync_parent_directory(target)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        if sha256_file(target) != manifest.digest:
            raise ArtifactIntegrityError("exported artifact does not match source digest")
        return target

    def recover_orphans(self) -> dict[str, list[str]]:
        report = {
            "temp_removed": [],
            "orphan_objects": [],
            "missing_objects": [],
            "invalid_manifests": [],
        }
        with durable_path_lock(self._guard_path):
            if self.root.exists():
                for path in sorted(self.root.rglob("*.tmp")):
                    if path.is_file() and path.name.startswith("."):
                        path.unlink()
                        report["temp_removed"].append(str(path.relative_to(self.root)))

            manifest_digests: set[str] = set()
            if self.manifests_root.exists():
                for path in sorted(self.manifests_root.rglob("*.json")):
                    digest = path.stem
                    try:
                        manifest = self.verify(digest)
                    except (ArtifactStoreError, ValueError):
                        report["invalid_manifests"].append(str(path.relative_to(self.root)))
                        try:
                            manifest = self.read_manifest(digest)
                        except (ArtifactStoreError, ValueError):
                            continue
                        manifest_digests.add(manifest.digest)
                        if not self._object_path(manifest.digest).is_file():
                            report["missing_objects"].append(manifest.digest)
                    else:
                        manifest_digests.add(manifest.digest)

            if self.objects_root.exists():
                for path in sorted(self.objects_root.rglob("*")):
                    if not path.is_file():
                        continue
                    digest = path.name
                    try:
                        normalized = _require_digest(digest)
                    except ValueError:
                        continue
                    if normalized not in manifest_digests:
                        report["orphan_objects"].append(normalized)
        return report
