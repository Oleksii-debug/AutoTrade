from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from uuid import UUID

from .durable_publish import atomic_write_json, sha256_file, sync_parent_directory
from .resource_lock import ResourceLock
from ..io.strict_json import strict_json_loads


class ArtifactConflict(ValueError):
    """Raised when immutable artifact identity or content conflicts."""


class ArtifactIntegrityError(ValueError):
    """Raised when a committed artifact cannot be verified."""


@dataclass(frozen=True)
class ArtifactAudit:
    manifests: int
    objects: int
    unreferenced_objects: tuple[str, ...]
    missing_objects: tuple[str, ...]
    corrupt_objects: tuple[str, ...]


def _manifest_integrity_hash(manifest: dict[str, Any]) -> str:
    payload = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(canonical).hexdigest()


def _verify_manifest_integrity(
    manifest: dict[str, Any],
    *,
    required: bool,
) -> bool:
    recorded = manifest.get("manifest_hash")
    if recorded is None:
        if required:
            raise ArtifactIntegrityError("artifact manifest lacks integrity binding")
        return False
    if (
        not isinstance(recorded, str)
        or len(recorded) != 71
        or not recorded.startswith("sha256:")
        or any(ch not in "0123456789abcdef" for ch in recorded[7:])
    ):
        raise ArtifactIntegrityError("artifact manifest integrity hash is invalid")
    if recorded != _manifest_integrity_hash(manifest):
        raise ArtifactIntegrityError("artifact manifest integrity mismatch")
    return True


class ArtifactStore:
    """Content-addressed evidence store with rights-aware export.

    Objects are immutable and addressed by SHA-256. Artifact IDs point to
    immutable manifests. A crash after object publication but before manifest
    publication can only leave an unreferenced object; recovery may remove it
    after recomputing all manifest references under the store lock.
    """

    SCHEMA_VERSION = 1

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.objects = self.root / "objects" / "sha256"
        self.manifests = self.root / "manifests"
        self.staging = self.root / "staging"
        self.lock_path = self.root / ".artifact-store.lock"
        for path in (self.objects, self.manifests, self.staging):
            path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _artifact_id(value: str) -> str:
        try:
            parsed = UUID(value)
        except (ValueError, AttributeError, TypeError) as error:
            raise ValueError("artifact_id must be a UUID") from error
        return str(parsed)

    @staticmethod
    def _validate_rights(rights: dict[str, Any]) -> dict[str, Any]:
        if type(rights) is not dict:
            raise TypeError("rights must be a dict")
        if rights.get("storage") is not True:
            raise ValueError("artifact storage is not permitted by rights")
        export = rights.get("export")
        if export not in (True, False):
            raise ValueError("rights.export must be explicitly true or false")
        normalized = dict(rights)
        normalized["storage"] = True
        normalized["export"] = export
        return normalized

    def _object_path(self, digest: str) -> Path:
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("digest must be lowercase SHA-256")
        return self.objects / digest[:2] / digest

    def _manifest_path(self, artifact_id: str) -> Path:
        return self.manifests / f"{self._artifact_id(artifact_id)}.json"

    def _load_manifest_path(self, path: Path) -> dict[str, Any]:
        try:
            value = strict_json_loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise ArtifactIntegrityError(f"invalid artifact manifest: {path.name}") from error
        if type(value) is not dict or value.get("schema_version") != self.SCHEMA_VERSION:
            raise ArtifactIntegrityError(f"unsupported artifact manifest: {path.name}")
        _verify_manifest_integrity(value, required=False)
        return value

    def load_manifest(self, artifact_id: str) -> dict[str, Any]:
        path = self._manifest_path(artifact_id)
        if not path.is_file():
            raise FileNotFoundError(path)
        manifest = self._load_manifest_path(path)
        if manifest.get("artifact_id") != self._artifact_id(artifact_id):
            raise ArtifactIntegrityError("manifest artifact identity mismatch")
        return manifest

    def publish_bytes(
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
        digest = __import__("hashlib").sha256(data).hexdigest()
        object_path = self._object_path(digest)
        manifest_path = self._manifest_path(normalized_id)

        with ResourceLock(self.lock_path):
            if manifest_path.exists():
                existing = self._load_manifest_path(manifest_path)
                immutable = {
                    "artifact_id": normalized_id,
                    "sha256": f"sha256:{digest}",
                    "bytes": len(data),
                    "media_type": media_type,
                    "rights": normalized_rights,
                    "source_refs": sources,
                    "metadata": meta,
                }
                if any(existing.get(key) != value for key, value in immutable.items()):
                    raise ArtifactConflict("artifact_id is already committed with different content or metadata")
                self._verify_manifest_object(existing)
                if not _verify_manifest_integrity(existing, required=False):
                    existing = dict(existing)
                    existing["manifest_hash"] = _manifest_integrity_hash(existing)
                    atomic_write_json(manifest_path, existing)
                return existing

            object_path.parent.mkdir(parents=True, exist_ok=True)
            if object_path.is_symlink():
                raise ArtifactIntegrityError(
                    "content-addressed object path must not be a symlink"
                )
            if object_path.exists():
                if object_path.stat().st_size != len(data) or sha256_file(object_path) != digest:
                    raise ArtifactIntegrityError("content-addressed object path is corrupt")
            else:
                temporary: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        "wb",
                        dir=self.staging,
                        prefix="artifact-",
                        suffix=".tmp",
                        delete=False,
                    ) as handle:
                        temporary = Path(handle.name)
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    if sha256_file(temporary) != digest:
                        raise ArtifactIntegrityError("staged artifact hash changed")
                    os.replace(temporary, object_path)
                    temporary = None
                    sync_parent_directory(object_path)
                finally:
                    if temporary is not None:
                        try:
                            temporary.unlink()
                        except FileNotFoundError:
                            pass

            manifest = {
                "schema_version": self.SCHEMA_VERSION,
                "artifact_id": normalized_id,
                "sha256": f"sha256:{digest}",
                "bytes": len(data),
                "media_type": media_type,
                "rights": normalized_rights,
                "source_refs": sources,
                "metadata": meta,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            manifest["manifest_hash"] = _manifest_integrity_hash(manifest)
            atomic_write_json(manifest_path, manifest)
            return manifest

    def _verify_manifest_object(self, manifest: dict[str, Any]) -> Path:
        digest_value = manifest.get("sha256")
        if not isinstance(digest_value, str) or not digest_value.startswith("sha256:"):
            raise ArtifactIntegrityError("manifest digest is invalid")
        digest = digest_value.removeprefix("sha256:")
        object_path = self._object_path(digest)
        if object_path.is_symlink():
            raise ArtifactIntegrityError("artifact object must not be a symlink")
        if not object_path.is_file():
            raise ArtifactIntegrityError("artifact object is missing")
        if object_path.stat().st_size != manifest.get("bytes"):
            raise ArtifactIntegrityError("artifact object size mismatch")
        if sha256_file(object_path) != digest:
            raise ArtifactIntegrityError("artifact object hash mismatch")
        return object_path

    def read_bytes(self, artifact_id: str) -> bytes:
        manifest = self.load_manifest(artifact_id)
        _verify_manifest_integrity(manifest, required=True)
        return self._verify_manifest_object(manifest).read_bytes()

    def export(self, artifact_id: str, destination: str | Path) -> Path:
        manifest = self.load_manifest(artifact_id)
        _verify_manifest_integrity(manifest, required=True)
        if manifest.get("rights", {}).get("export") is not True:
            raise PermissionError("artifact rights do not permit export")
        source = self._verify_manifest_object(manifest)
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
                handle.write(source.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
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

    def audit(self) -> ArtifactAudit:
        referenced: set[str] = set()
        missing: list[str] = []
        corrupt: list[str] = []
        manifest_count = 0
        for manifest_path in sorted(self.manifests.glob("*.json")):
            manifest_count += 1
            try:
                manifest = self._load_manifest_path(manifest_path)
                digest = manifest["sha256"].removeprefix("sha256:")
                referenced.add(digest)
                self._verify_manifest_object(manifest)
            except (ArtifactIntegrityError, KeyError, AttributeError, ValueError):
                try:
                    value = json.loads(manifest_path.read_text(encoding="utf-8"))
                    digest_value = value.get("sha256") if isinstance(value, dict) else None
                    digest = digest_value.removeprefix("sha256:") if isinstance(digest_value, str) else ""
                    if digest:
                        referenced.add(digest)
                        path = self._object_path(digest)
                        if not path.exists():
                            missing.append(manifest_path.name)
                        else:
                            corrupt.append(manifest_path.name)
                    else:
                        corrupt.append(manifest_path.name)
                except Exception:
                    corrupt.append(manifest_path.name)

        object_digests: set[str] = set()
        for path in self.objects.glob("*/*"):
            if path.is_symlink():
                corrupt.append(
                    "object:" + path.relative_to(self.root).as_posix()
                )
                continue
            if not path.is_file():
                continue
            digest = path.name
            try:
                canonical = self._object_path(digest)
            except ValueError:
                corrupt.append(
                    "object:" + path.relative_to(self.root).as_posix()
                )
                continue
            if path != canonical:
                corrupt.append(
                    "object:" + path.relative_to(self.root).as_posix()
                )
                continue
            object_digests.add(digest)
        return ArtifactAudit(
            manifests=manifest_count,
            objects=len(object_digests),
            unreferenced_objects=tuple(sorted(object_digests - referenced)),
            missing_objects=tuple(sorted(set(missing))),
            corrupt_objects=tuple(sorted(set(corrupt))),
        )

    def recover_orphans(self) -> ArtifactAudit:
        with ResourceLock(self.lock_path):
            before = self.audit()
            for digest in before.unreferenced_objects:
                path = self._object_path(digest)
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            for path in self.staging.glob("*"):
                if path.is_file():
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
            return self.audit()
