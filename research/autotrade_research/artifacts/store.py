from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable
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

    def __init__(
        self,
        root: str | Path,
        *,
        export_authorizer: Callable[[str, str], bool] | None = None,
    ):
        self.root = Path(root)
        # Manifest hashes detect corruption but do not authenticate mutable policy.
        # Export authority must come from a caller-controlled source outside this
        # writable artifact-store namespace and bind the artifact identity+digest.
        self._export_authorizer = export_authorizer
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

    @classmethod
    def _validate_manifest_contract(
        cls,
        manifest: dict[str, Any],
        *,
        authenticated: bool,
    ) -> None:
        """Validate the closed v1 manifest contract without trusting its hash alone.

        A SHA-256 stored beside mutable local bytes is an integrity binding, not a
        schema validator. Authenticated manifests therefore admit only the exact
        canonical top-level field set. Legacy hashless manifests may retain extra
        untrusted fields only so publish_bytes can reconstruct and rebind the
        canonical manifest from independently verified immutable inputs.
        """

        required_fields = {
            "schema_version",
            "artifact_id",
            "sha256",
            "bytes",
            "media_type",
            "rights",
            "source_refs",
            "metadata",
            "created_at",
        }
        missing = required_fields - set(manifest)
        if missing:
            raise ArtifactIntegrityError(
                "artifact manifest is missing required fields: "
                + ", ".join(sorted(missing))
            )
        if authenticated:
            allowed = required_fields | {"manifest_hash"}
            unexpected = set(manifest) - allowed
            if unexpected:
                raise ArtifactIntegrityError(
                    "artifact manifest has unexpected authenticated fields: "
                    + ", ".join(sorted(unexpected))
                )

        if type(manifest.get("schema_version")) is not int:
            raise ArtifactIntegrityError("artifact manifest schema_version is invalid")
        if manifest["schema_version"] != cls.SCHEMA_VERSION:
            raise ArtifactIntegrityError("artifact manifest schema_version is unsupported")

        artifact_id = manifest.get("artifact_id")
        if not isinstance(artifact_id, str):
            raise ArtifactIntegrityError("artifact manifest artifact_id is invalid")
        try:
            canonical_artifact_id = cls._artifact_id(artifact_id)
        except ValueError as error:
            raise ArtifactIntegrityError(
                "artifact manifest artifact_id is invalid"
            ) from error
        if canonical_artifact_id != artifact_id:
            raise ArtifactIntegrityError(
                "artifact manifest artifact_id is not canonical"
            )

        digest = manifest.get("sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 71
            or not digest.startswith("sha256:")
            or any(ch not in "0123456789abcdef" for ch in digest[7:])
        ):
            raise ArtifactIntegrityError("artifact manifest digest is invalid")

        byte_count = manifest.get("bytes")
        if type(byte_count) is not int or byte_count < 0:
            raise ArtifactIntegrityError("artifact manifest byte count is invalid")

        media_type = manifest.get("media_type")
        if not isinstance(media_type, str) or not media_type.strip():
            raise ArtifactIntegrityError("artifact manifest media_type is invalid")

        rights = manifest.get("rights")
        if type(rights) is not dict:
            raise ArtifactIntegrityError("artifact manifest rights are invalid")
        if rights.get("storage") is not True or type(rights.get("export")) is not bool:
            raise ArtifactIntegrityError("artifact manifest rights contract is invalid")

        source_refs = manifest.get("source_refs")
        if type(source_refs) is not list or not all(
            isinstance(item, str) and bool(item) for item in source_refs
        ):
            raise ArtifactIntegrityError("artifact manifest source_refs are invalid")

        if type(manifest.get("metadata")) is not dict:
            raise ArtifactIntegrityError("artifact manifest metadata is invalid")

        created_at = manifest.get("created_at")
        if not isinstance(created_at, str) or not created_at.strip():
            raise ArtifactIntegrityError("artifact manifest created_at is invalid")
        try:
            parsed_created_at = datetime.fromisoformat(
                created_at.replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ArtifactIntegrityError(
                "artifact manifest created_at is invalid"
            ) from error
        if (
            parsed_created_at.tzinfo is None
            or parsed_created_at.utcoffset() is None
        ):
            raise ArtifactIntegrityError(
                "artifact manifest created_at must include timezone"
            )

    def _load_manifest_path(self, path: Path) -> dict[str, Any]:
        try:
            value = strict_json_loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise ArtifactIntegrityError(f"invalid artifact manifest: {path.name}") from error
        if type(value) is not dict:
            raise ArtifactIntegrityError(f"unsupported artifact manifest: {path.name}")
        authenticated = _verify_manifest_integrity(value, required=False)
        self._validate_manifest_contract(value, authenticated=authenticated)
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
                    # Legacy manifests have no authenticated metadata boundary.
                    # Never make caller-editable historical fields trustworthy by
                    # hashing the bytes in place. Reconstruct the canonical v1
                    # manifest from the verified object and the exact immutable
                    # inputs supplied for this rebind. The timestamp is the
                    # rebind instant, not an unverifiable legacy creation claim.
                    existing = {
                        "schema_version": self.SCHEMA_VERSION,
                        **immutable,
                        "created_at": datetime.now(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    }
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
        if self._export_authorizer is None:
            raise PermissionError("independent export authorization is required")
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
