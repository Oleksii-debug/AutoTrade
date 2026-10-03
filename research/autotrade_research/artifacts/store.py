from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Callable
from uuid import UUID

from .durable_publish import atomic_write_json, sha256_file, sync_parent_directory
from .resource_lock import ResourceLock, _open_read_only_descriptor
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
        allowed_fields = {"storage", "export", "rights_id"}
        if (
            not {"storage", "export"}.issubset(rights)
            or set(rights) - allowed_fields
        ):
            raise ValueError(
                "rights must contain storage/export and only canonical optional fields"
            )
        export = rights.get("export")
        if type(export) is not bool:
            raise ValueError("rights.export must be explicitly true or false")
        normalized: dict[str, Any] = {"storage": True, "export": export}
        if "rights_id" in rights:
            rights_id = rights["rights_id"]
            if (
                not isinstance(rights_id, str)
                or not rights_id
                or rights_id != rights_id.strip()
            ):
                raise ValueError(
                    "rights.rights_id must be canonical non-empty text"
                )
            normalized["rights_id"] = rights_id
        return normalized

    def _object_path(self, digest: str) -> Path:
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("digest must be lowercase SHA-256")
        return self.objects / digest[:2] / digest

    def _manifest_path(self, artifact_id: str) -> Path:
        return self.manifests / f"{self._artifact_id(artifact_id)}.json"

    def _validate_staging_namespace(self) -> None:
        try:
            resolved_root = self.root.resolve(strict=False)
            expected_staging = resolved_root / "staging"
            resolved_staging = self.staging.resolve(strict=False)
        except OSError as error:
            raise ArtifactIntegrityError(
                "artifact staging namespace cannot be resolved"
            ) from error
        if resolved_staging != expected_staging:
            raise ArtifactIntegrityError(
                "artifact staging path escapes store namespace"
            )

    def _validate_manifest_namespace(self, manifest_path: Path) -> None:
        try:
            resolved_root = self.root.resolve(strict=False)
            expected_manifests = resolved_root / "manifests"
            resolved_manifests = self.manifests.resolve(strict=False)
            resolved_parent = manifest_path.parent.resolve(strict=False)
        except OSError as error:
            raise ArtifactIntegrityError(
                "artifact manifest namespace cannot be resolved"
            ) from error
        if (
            resolved_manifests != expected_manifests
            or resolved_parent != expected_manifests
        ):
            raise ArtifactIntegrityError(
                "artifact manifest path escapes store namespace"
            )

    def _validate_manifest_entry(self, manifest_path: Path) -> os.stat_result:
        self._validate_manifest_namespace(manifest_path)
        try:
            entry = os.stat(manifest_path, follow_symlinks=False)
        except FileNotFoundError:
            raise FileNotFoundError(manifest_path)
        except OSError as error:
            raise ArtifactIntegrityError(
                "artifact manifest cannot be inspected"
            ) from error
        self._reject_reparse_point(entry, subject="artifact manifest")
        if stat.S_ISLNK(entry.st_mode):
            raise ArtifactIntegrityError("artifact manifest must not be a symlink")
        if not stat.S_ISREG(entry.st_mode):
            raise ArtifactIntegrityError(
                "artifact manifest must be a regular file"
            )
        if entry.st_nlink != 1:
            raise ArtifactIntegrityError(
                "artifact manifest must not have hard-link aliases"
            )
        return entry

    def _validate_object_namespace(self, object_path: Path) -> None:
        try:
            resolved_root = self.root.resolve(strict=False)
            expected_objects = resolved_root / "objects" / "sha256"
            resolved_objects = self.objects.resolve(strict=False)
            resolved_parent = object_path.parent.resolve(strict=False)
        except OSError as error:
            raise ArtifactIntegrityError(
                "content-addressed object namespace cannot be resolved"
            ) from error
        expected_parent = expected_objects / object_path.name[:2]
        if resolved_objects != expected_objects or resolved_parent != expected_parent:
            raise ArtifactIntegrityError(
                "content-addressed object path escapes store namespace"
            )

    def _validate_object_entry(self, object_path: Path) -> os.stat_result:
        self._validate_object_namespace(object_path)
        try:
            entry = os.stat(object_path, follow_symlinks=False)
        except FileNotFoundError:
            raise ArtifactIntegrityError("artifact object is missing")
        except OSError as error:
            raise ArtifactIntegrityError("artifact object cannot be inspected") from error
        self._reject_reparse_point(entry, subject="artifact object")
        if stat.S_ISLNK(entry.st_mode):
            raise ArtifactIntegrityError("artifact object must not be a symlink")
        if not stat.S_ISREG(entry.st_mode):
            raise ArtifactIntegrityError("artifact object must be a regular file")
        if entry.st_nlink != 1:
            raise ArtifactIntegrityError("artifact object must not have hard-link aliases")
        return entry

    def _manifest_object_contract(
        self,
        manifest: dict[str, Any],
    ) -> tuple[Path, str, int]:
        digest_value = manifest.get("sha256")
        if (
            not isinstance(digest_value, str)
            or len(digest_value) != 71
            or not digest_value.startswith("sha256:")
            or any(ch not in "0123456789abcdef" for ch in digest_value[7:])
        ):
            raise ArtifactIntegrityError("manifest digest is invalid")
        expected_bytes = manifest.get("bytes")
        if (
            isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or expected_bytes < 0
        ):
            raise ArtifactIntegrityError("manifest byte count is invalid")
        digest = digest_value.removeprefix("sha256:")
        return self._object_path(digest), digest, expected_bytes

    @staticmethod
    def _same_filesystem_entry(
        first: os.stat_result,
        second: os.stat_result,
    ) -> bool:
        return os.path.samestat(first, second)

    @staticmethod
    def _reject_reparse_point(
        entry: os.stat_result,
        *,
        subject: str,
    ) -> None:
        attributes = getattr(entry, "st_file_attributes", None)
        if os.name == "nt" and attributes is None:
            raise ArtifactIntegrityError(
                f"{subject} reparse-point attributes are unavailable"
            )
        reparse_flag = getattr(
            stat,
            "FILE_ATTRIBUTE_REPARSE_POINT",
            0x400,
        )
        if attributes is not None and attributes & reparse_flag:
            raise ArtifactIntegrityError(
                f"{subject} must not be a reparse point"
            )

    def _open_object_descriptor(
        self,
        object_path: Path,
        *,
        expected_bytes: int,
    ) -> tuple[int, os.stat_result]:
        before = self._validate_object_entry(object_path)
        if before.st_size != expected_bytes:
            raise ArtifactIntegrityError("artifact object size mismatch")

        try:
            if os.name == "nt":
                descriptor = _open_read_only_descriptor(object_path)
            else:
                no_follow = getattr(os, "O_NOFOLLOW", 0)
                if not no_follow:
                    raise OSError(
                        "platform lacks no-follow artifact object open support"
                    )
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | no_follow
                    | getattr(os, "O_NONBLOCK", 0)
                )
                descriptor = os.open(object_path, flags)
        except OSError as error:
            raise ArtifactIntegrityError(
                "artifact object could not be opened safely"
            ) from error

        try:
            opened = os.fstat(descriptor)
            self._reject_reparse_point(
                opened,
                subject="artifact object descriptor",
            )
            if not stat.S_ISREG(opened.st_mode):
                raise ArtifactIntegrityError(
                    "artifact object descriptor must be a regular file"
                )
            if opened.st_nlink != 1:
                raise ArtifactIntegrityError(
                    "artifact object descriptor must not have hard-link aliases"
                )
            if opened.st_size != expected_bytes:
                raise ArtifactIntegrityError("artifact object size mismatch")
            current = self._validate_object_entry(object_path)
            if (
                not self._same_filesystem_entry(before, opened)
                or not self._same_filesystem_entry(opened, current)
            ):
                raise ArtifactIntegrityError(
                    "artifact object changed before descriptor read"
                )
        except Exception:
            os.close(descriptor)
            raise
        return descriptor, opened

    def _revalidate_object_descriptor(
        self,
        object_path: Path,
        descriptor: int,
        opened: os.stat_result,
        *,
        expected_bytes: int,
    ) -> None:
        try:
            after_descriptor = os.fstat(descriptor)
        except OSError as error:
            raise ArtifactIntegrityError(
                "artifact object descriptor could not be revalidated"
            ) from error
        self._reject_reparse_point(
            after_descriptor,
            subject="artifact object descriptor",
        )
        if (
            not stat.S_ISREG(after_descriptor.st_mode)
            or after_descriptor.st_nlink != 1
            or after_descriptor.st_size != expected_bytes
            or not self._same_filesystem_entry(opened, after_descriptor)
        ):
            raise ArtifactIntegrityError(
                "artifact object changed during descriptor read"
            )
        current = self._validate_object_entry(object_path)
        if not self._same_filesystem_entry(after_descriptor, current):
            raise ArtifactIntegrityError(
                "artifact object path changed during descriptor read"
            )

    @staticmethod
    def _bounded_descriptor_chunks(descriptor: int, expected_bytes: int):
        remaining = expected_bytes + 1
        while remaining > 0:
            try:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
            except OSError as error:
                raise ArtifactIntegrityError(
                    "artifact object could not be read safely"
                ) from error
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk

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
        allowed_rights_fields = {"storage", "export", "rights_id"}
        if (
            not {"storage", "export"}.issubset(rights)
            or set(rights) - allowed_rights_fields
            or rights.get("storage") is not True
            or type(rights.get("export")) is not bool
        ):
            raise ArtifactIntegrityError("artifact manifest rights contract is invalid")
        if "rights_id" in rights:
            rights_id = rights["rights_id"]
            if (
                not isinstance(rights_id, str)
                or not rights_id
                or rights_id != rights_id.strip()
            ):
                raise ArtifactIntegrityError(
                    "artifact manifest rights identity is invalid"
                )

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

    @staticmethod
    def _manifest_descriptor_snapshot(entry: os.stat_result) -> tuple[int, ...]:
        """Return descriptor metadata that must remain stable while bytes are read."""

        return (
            entry.st_mode,
            entry.st_nlink,
            entry.st_size,
            entry.st_mtime_ns,
            entry.st_ctime_ns,
        )

    def _open_manifest_descriptor(
        self,
        manifest_path: Path,
    ) -> tuple[int, os.stat_result]:
        before = self._validate_manifest_entry(manifest_path)
        try:
            if os.name == "nt":
                descriptor = _open_read_only_descriptor(manifest_path)
            else:
                no_follow = getattr(os, "O_NOFOLLOW", 0)
                if not no_follow:
                    raise OSError(
                        "platform lacks no-follow artifact manifest open support"
                    )
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | no_follow
                    | getattr(os, "O_NONBLOCK", 0)
                )
                descriptor = os.open(manifest_path, flags)
        except OSError as error:
            raise ArtifactIntegrityError(
                "artifact manifest could not be opened safely"
            ) from error

        try:
            opened = os.fstat(descriptor)
            self._reject_reparse_point(
                opened,
                subject="artifact manifest descriptor",
            )
            if not stat.S_ISREG(opened.st_mode):
                raise ArtifactIntegrityError(
                    "artifact manifest descriptor must be a regular file"
                )
            if opened.st_nlink != 1:
                raise ArtifactIntegrityError(
                    "artifact manifest descriptor must not have hard-link aliases"
                )
            current = self._validate_manifest_entry(manifest_path)
            if (
                not self._same_filesystem_entry(before, opened)
                or not self._same_filesystem_entry(opened, current)
                or before.st_size != opened.st_size
                or opened.st_size != current.st_size
            ):
                raise ArtifactIntegrityError(
                    "artifact manifest changed during read"
                )
        except Exception:
            os.close(descriptor)
            raise
        return descriptor, opened

    def _revalidate_manifest_descriptor(
        self,
        manifest_path: Path,
        descriptor: int,
        opened: os.stat_result,
    ) -> None:
        try:
            after_descriptor = os.fstat(descriptor)
        except OSError as error:
            raise ArtifactIntegrityError(
                "artifact manifest descriptor could not be revalidated"
            ) from error
        self._reject_reparse_point(
            after_descriptor,
            subject="artifact manifest descriptor",
        )
        if (
            not stat.S_ISREG(after_descriptor.st_mode)
            or after_descriptor.st_nlink != 1
            or not self._same_filesystem_entry(opened, after_descriptor)
            or self._manifest_descriptor_snapshot(after_descriptor)
            != self._manifest_descriptor_snapshot(opened)
        ):
            raise ArtifactIntegrityError("artifact manifest changed during read")
        current = self._validate_manifest_entry(manifest_path)
        if (
            not self._same_filesystem_entry(after_descriptor, current)
            or after_descriptor.st_size != current.st_size
        ):
            raise ArtifactIntegrityError("artifact manifest changed during read")

    def _read_manifest_descriptor(
        self,
        manifest_path: Path,
        descriptor: int,
        opened: os.stat_result,
    ) -> bytes:
        expected_bytes = opened.st_size
        remaining = expected_bytes + 1
        chunks: list[bytes] = []
        copied = 0
        while remaining > 0:
            try:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
            except OSError as error:
                raise ArtifactIntegrityError(
                    "artifact manifest could not be read safely"
                ) from error
            if not chunk:
                break
            chunks.append(chunk)
            copied += len(chunk)
            remaining -= len(chunk)

        self._revalidate_manifest_descriptor(
            manifest_path,
            descriptor,
            opened,
        )
        if copied != expected_bytes:
            raise ArtifactIntegrityError("artifact manifest changed during read")
        return b"".join(chunks)

    def _load_manifest_path(self, path: Path) -> dict[str, Any]:
        descriptor, opened = self._open_manifest_descriptor(path)
        try:
            raw_bytes = self._read_manifest_descriptor(
                path,
                descriptor,
                opened,
            )
        finally:
            os.close(descriptor)

        return self._decode_manifest_bytes(path, raw_bytes)

    def _decode_manifest_bytes(
        self,
        path: Path,
        raw_bytes: bytes,
    ) -> dict[str, Any]:
        try:
            raw = raw_bytes.decode("utf-8")
        except UnicodeError as error:
            raise ArtifactIntegrityError(
                f"invalid artifact manifest: {path.name}"
            ) from error
        try:
            value = strict_json_loads(raw)
        except (UnicodeError, ValueError) as error:
            raise ArtifactIntegrityError(
                f"invalid artifact manifest: {path.name}"
            ) from error
        if type(value) is not dict:
            raise ArtifactIntegrityError(
                f"unsupported artifact manifest: {path.name}"
            )
        authenticated = _verify_manifest_integrity(value, required=False)
        self._validate_manifest_contract(value, authenticated=authenticated)
        return value

    def load_manifest(self, artifact_id: str) -> dict[str, Any]:
        path = self._manifest_path(artifact_id)
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
            self._validate_manifest_namespace(manifest_path)
            if manifest_path.exists() or manifest_path.is_symlink():
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
            self._validate_object_namespace(object_path)
            if object_path.exists() or object_path.is_symlink():
                # Preserve the precise fail-closed integrity classification
                # (symlink, hard-link alias, size/hash mismatch, namespace escape).
                # Callers and qualification regressions rely on these diagnostics.
                self._verify_manifest_object(
                    {
                        "sha256": f"sha256:{digest}",
                        "bytes": len(data),
                    }
                )
            else:
                self._validate_staging_namespace()
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
        object_path, expected_digest, expected_bytes = (
            self._manifest_object_contract(manifest)
        )
        descriptor, opened = self._open_object_descriptor(
            object_path,
            expected_bytes=expected_bytes,
        )
        try:
            copied = 0
            copied_hash = sha256()
            for chunk in self._bounded_descriptor_chunks(
                descriptor,
                expected_bytes,
            ):
                copied += len(chunk)
                copied_hash.update(chunk)
            self._revalidate_object_descriptor(
                object_path,
                descriptor,
                opened,
                expected_bytes=expected_bytes,
            )
        finally:
            os.close(descriptor)
        if copied != expected_bytes:
            raise ArtifactIntegrityError("artifact object size mismatch")
        if copied_hash.hexdigest() != expected_digest:
            raise ArtifactIntegrityError("artifact object hash mismatch")
        return object_path

    def _read_verified_object_bytes(self, manifest: dict[str, Any]) -> bytes:
        object_path, expected_digest, expected_bytes = (
            self._manifest_object_contract(manifest)
        )
        descriptor, opened = self._open_object_descriptor(
            object_path,
            expected_bytes=expected_bytes,
        )
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
            self._revalidate_object_descriptor(
                object_path,
                descriptor,
                opened,
                expected_bytes=expected_bytes,
            )
        finally:
            os.close(descriptor)
        if copied != expected_bytes or copied_hash.hexdigest() != expected_digest:
            raise ArtifactIntegrityError("artifact object changed during read")
        return b"".join(chunks)

    def read_authenticated_snapshot(
        self,
        artifact_id: str,
    ) -> tuple[dict[str, Any], bytes]:
        normalized_id = self._artifact_id(artifact_id)
        manifest_path = self._manifest_path(normalized_id)
        descriptor, opened = self._open_manifest_descriptor(manifest_path)
        try:
            raw_bytes = self._read_manifest_descriptor(
                manifest_path,
                descriptor,
                opened,
            )
            manifest = self._decode_manifest_bytes(
                manifest_path,
                raw_bytes,
            )
            if manifest.get("artifact_id") != normalized_id:
                raise ArtifactIntegrityError(
                    "manifest artifact identity mismatch"
                )
            _verify_manifest_integrity(manifest, required=True)
            data = self._read_verified_object_bytes(manifest)
            self._revalidate_manifest_descriptor(
                manifest_path,
                descriptor,
                opened,
            )
            return manifest, data
        finally:
            os.close(descriptor)

    def read_bytes(self, artifact_id: str) -> bytes:
        _manifest, data = self.read_authenticated_snapshot(artifact_id)
        return data

    def export(self, artifact_id: str, destination: str | Path) -> Path:
        manifest = self.load_manifest(artifact_id)
        _verify_manifest_integrity(manifest, required=True)
        if manifest.get("rights", {}).get("export") is not True:
            raise PermissionError("artifact rights do not permit export")
        if self._export_authorizer is None:
            raise PermissionError("independent export authorization is required")

        source, expected_digest, expected_bytes = (
            self._manifest_object_contract(manifest)
        )
        descriptor, opened = self._open_object_descriptor(
            source,
            expected_bytes=expected_bytes,
        )
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
                    self._revalidate_object_descriptor(
                        source,
                        descriptor,
                        opened,
                        expected_bytes=expected_bytes,
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                if (
                    copied != expected_bytes
                    or copied_hash.hexdigest() != expected_digest
                ):
                    raise ArtifactIntegrityError(
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

    def audit(self) -> ArtifactAudit:
        referenced: set[str] = set()
        missing: list[str] = []
        corrupt: list[str] = []
        manifest_count = 0
        for manifest_path in sorted(self.manifests.glob("*.json")):
            manifest_count += 1
            try:
                manifest = self._load_manifest_path(manifest_path)
                # Recovery may delete objects that are not referenced by any
                # trusted manifest. A legacy/hashless or otherwise unauthenticated
                # manifest therefore cannot contribute a recovery reference.
                _verify_manifest_integrity(manifest, required=True)
            except (
                ArtifactIntegrityError,
                FileNotFoundError,
                KeyError,
                AttributeError,
                ValueError,
            ):
                corrupt.append(manifest_path.name)
                continue

            digest = manifest["sha256"].removeprefix("sha256:")
            referenced.add(digest)
            object_path = self._object_path(digest)
            try:
                self._validate_object_namespace(object_path)
                os.stat(object_path, follow_symlinks=False)
            except FileNotFoundError:
                missing.append(manifest_path.name)
                continue
            except (ArtifactIntegrityError, OSError):
                corrupt.append(manifest_path.name)
                continue

            try:
                self._verify_manifest_object(manifest)
            except ArtifactIntegrityError:
                corrupt.append(manifest_path.name)

        object_digests: set[str] = set()
        for path in self.objects.glob("*/*"):
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
            try:
                self._validate_object_entry(path)
            except ArtifactIntegrityError:
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

    @staticmethod
    def _supports_descriptor_relative_cleanup() -> bool:
        """Whether stdlib deletion can stay bound to a verified directory."""

        supports_dir_fd = getattr(os, "supports_dir_fd", set())
        supports_fd = getattr(os, "supports_fd", set())
        return bool(
            os.name != "nt"
            and getattr(os, "O_DIRECTORY", 0)
            and getattr(os, "O_NOFOLLOW", 0)
            and os.stat in supports_dir_fd
            and os.unlink in supports_dir_fd
            and os.listdir in supports_fd
        )

    def _open_verified_cleanup_directory(
        self,
        directory: Path,
        *,
        subject: str,
    ) -> tuple[int, os.stat_result]:
        if not self._supports_descriptor_relative_cleanup():
            raise ArtifactIntegrityError(
                f"{subject} cleanup lacks descriptor-relative platform support"
            )
        try:
            before = os.stat(directory, follow_symlinks=False)
        except OSError as error:
            raise ArtifactIntegrityError(
                f"{subject} cleanup directory cannot be inspected"
            ) from error
        self._reject_reparse_point(before, subject=f"{subject} cleanup directory")
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise ArtifactIntegrityError(
                f"{subject} cleanup directory is not a canonical directory"
            )

        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(directory, flags)
        except OSError as error:
            raise ArtifactIntegrityError(
                f"{subject} cleanup directory could not be opened safely"
            ) from error
        try:
            opened = os.fstat(descriptor)
            current = os.stat(directory, follow_symlinks=False)
            self._reject_reparse_point(
                opened,
                subject=f"{subject} cleanup directory descriptor",
            )
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not self._same_filesystem_entry(before, opened)
                or not self._same_filesystem_entry(opened, current)
            ):
                raise ArtifactIntegrityError(
                    f"{subject} cleanup directory changed before destructive use"
                )
        except Exception:
            os.close(descriptor)
            raise
        return descriptor, opened

    def _revalidate_cleanup_directory(
        self,
        directory: Path,
        descriptor: int,
        opened: os.stat_result,
        *,
        subject: str,
    ) -> None:
        try:
            held = os.fstat(descriptor)
            current = os.stat(directory, follow_symlinks=False)
        except OSError as error:
            raise ArtifactIntegrityError(
                f"{subject} cleanup directory could not be revalidated"
            ) from error
        self._reject_reparse_point(
            held,
            subject=f"{subject} cleanup directory descriptor",
        )
        if (
            not stat.S_ISDIR(held.st_mode)
            or not self._same_filesystem_entry(opened, held)
            or not self._same_filesystem_entry(held, current)
        ):
            raise ArtifactIntegrityError(
                f"{subject} cleanup directory changed before destructive use"
            )

    def _unlink_verified_regular_entry(
        self,
        directory: Path,
        name: str,
        *,
        subject: str,
    ) -> bool:
        """Delete one regular entry without re-resolving its parent pathname.

        POSIX dir_fd semantics bind inspection and unlink to the same held
        directory identity. Platforms without that primitive fail closed by
        declining destructive cleanup.
        """

        if not self._supports_descriptor_relative_cleanup():
            return False
        descriptor, opened = self._open_verified_cleanup_directory(
            directory,
            subject=subject,
        )
        try:
            try:
                entry = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return False
            except OSError as error:
                raise ArtifactIntegrityError(
                    f"{subject} cleanup entry cannot be inspected"
                ) from error
            self._reject_reparse_point(entry, subject=f"{subject} cleanup entry")
            if (
                stat.S_ISLNK(entry.st_mode)
                or not stat.S_ISREG(entry.st_mode)
                or entry.st_nlink != 1
            ):
                return False

            self._revalidate_cleanup_directory(
                directory,
                descriptor,
                opened,
                subject=subject,
            )
            try:
                os.unlink(name, dir_fd=descriptor)
            except FileNotFoundError:
                return False
            return True
        finally:
            os.close(descriptor)

    def _cleanup_staging_entries(self) -> None:
        self._validate_staging_namespace()
        if not self._supports_descriptor_relative_cleanup():
            return
        descriptor, opened = self._open_verified_cleanup_directory(
            self.staging,
            subject="artifact staging",
        )
        try:
            try:
                names = tuple(os.listdir(descriptor))
            except OSError as error:
                raise ArtifactIntegrityError(
                    "artifact staging directory cannot be enumerated safely"
                ) from error
            for name in names:
                try:
                    entry = os.stat(
                        name,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                except OSError as error:
                    raise ArtifactIntegrityError(
                        "artifact staging entry cannot be inspected"
                    ) from error
                self._reject_reparse_point(
                    entry,
                    subject="artifact staging cleanup entry",
                )
                if (
                    not stat.S_ISREG(entry.st_mode)
                    or entry.st_nlink != 1
                ):
                    continue
                self._revalidate_cleanup_directory(
                    self.staging,
                    descriptor,
                    opened,
                    subject="artifact staging",
                )
                try:
                    os.unlink(name, dir_fd=descriptor)
                except FileNotFoundError:
                    continue
        finally:
            os.close(descriptor)

    def recover_orphans(self) -> ArtifactAudit:
        with ResourceLock(self.lock_path):
            self._validate_staging_namespace()
            before = self.audit()
            corrupt_manifests = tuple(
                item
                for item in before.corrupt_objects
                if not item.startswith("object:")
            )
            if not corrupt_manifests:
                for digest in before.unreferenced_objects:
                    path = self._object_path(digest)
                    try:
                        self._validate_object_namespace(path)
                        self._unlink_verified_regular_entry(
                            path.parent,
                            path.name,
                            subject="artifact object",
                        )
                    except ArtifactIntegrityError:
                        continue
            self._cleanup_staging_entries()
            return self.audit()
