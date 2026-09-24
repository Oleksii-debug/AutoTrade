"""Verified backup and fail-closed restore foundation for AutoTrade.

The backup uses SQLite's backup API for the durable journal, hashes every
payload file, verifies content-addressed artifact objects and restores through
an isolated staging directory. A restored runtime is always marked as requiring
reconciliation before any future trading authority can be considered.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
from uuid import UUID
from typing import Any, Mapping, Sequence

from .diagnostics import build_diagnostic_snapshot
from .persistence import JournalStore
from .reconciliation import ReconciliationResult
from .recovery import HostState, RecoveryController


BACKUP_SCHEMA_VERSION = 1
MANIFEST_NAME = "backup-manifest.json"
MANIFEST_DIGEST_NAME = "backup-manifest.sha256"
RESTORE_MARKER_NAME = "RESTORE_RECONCILIATION_REQUIRED.json"
RESTORE_COMPLETION_PROOF_NAME = "RESTORE_RECONCILIATION_COMPLETE.json"


class BackupError(RuntimeError):
    """Base error for backup or restore failures."""


class BackupIntegrityError(BackupError):
    """Raised when backup bytes or metadata do not verify."""


class BackupCompatibilityError(BackupError):
    """Raised when a backup uses an unsupported schema."""


def _sha256_bytes(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_bytes_durable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _safe_relative_path(raw: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise BackupIntegrityError("Backup manifest path is invalid")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise BackupIntegrityError("Backup manifest path is unsafe")
    return Path(*pure.parts)


def _copy_stable_file(source: Path, destination: Path) -> tuple[str, int]:
    if source.is_symlink() or not source.is_file():
        raise BackupError(f"Backup source is not a regular file: {source.name}")
    before = _sha256_file(source)
    payload = source.read_bytes()
    digest = _sha256_bytes(payload)
    after = _sha256_file(source)
    if before != digest or after != digest:
        raise BackupError(f"Backup source changed while being copied: {source.name}")
    _write_bytes_durable(destination, payload)
    return digest, len(payload)


def _sqlite_schema_version(path: Path) -> int:
    if not path.is_file():
        raise BackupError("Durable journal is missing")
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            rows = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
    except sqlite3.Error as error:
        raise BackupIntegrityError(
            "Durable journal schema evidence cannot be read"
        ) from error
    if not rows:
        raise BackupCompatibilityError("Durable journal has no schema migration record")
    return max(int(row[0]) for row in rows)


def _backup_sqlite(source: Path, destination: Path) -> tuple[str, int, int]:
    schema_version = _sqlite_schema_version(source)
    if schema_version != JournalStore.SCHEMA_VERSION:
        raise BackupCompatibilityError(
            f"Unsupported journal schema version: {schema_version}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = source.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(source_uri, uri=True)) as source_db:
            with closing(sqlite3.connect(destination)) as destination_db:
                source_db.backup(destination_db)
                destination_db.commit()
                # SQLite backup preserves the source journal mode. A portable
                # backup bundle must be a self-contained database, not a main
                # file whose latest pages live in undeclared WAL sidecars.
                destination_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                journal_mode = destination_db.execute(
                    "PRAGMA journal_mode=DELETE"
                ).fetchone()[0]
                if str(journal_mode).lower() != "delete":
                    raise BackupIntegrityError(
                        "SQLite backup could not be finalized as a standalone snapshot"
                    )
                destination_db.commit()
    except sqlite3.Error as error:
        raise BackupError("SQLite backup failed") from error
    copied_schema = _sqlite_schema_version(destination)
    if copied_schema != schema_version:
        raise BackupIntegrityError("SQLite backup changed the journal schema")
    return _sha256_file(destination), destination.stat().st_size, schema_version


def _entry(path: str, digest: str, size: int, kind: str) -> dict[str, Any]:
    return {
        "path": path,
        "sha256": f"sha256:{digest}",
        "size_bytes": size,
        "kind": kind,
    }


def _artifact_source_files(root: Path) -> list[tuple[Path, str]]:
    if not root.is_dir():
        raise BackupError("Artifact store root is missing")
    files: list[tuple[Path, str]] = []
    objects_root = root / "objects" / "sha256"
    manifests_root = root / "manifests" / "sha256"
    if objects_root.exists():
        for path in sorted(objects_root.rglob("*")):
            if path.is_file():
                files.append((path, "artifact-object"))
    if manifests_root.exists():
        for path in sorted(manifests_root.rglob("*.json")):
            if path.is_file():
                files.append((path, "artifact-manifest"))
    return files


def _validate_artifact_source(root: Path) -> None:
    manifests_root = root / "manifests" / "sha256"
    objects_root = root / "objects" / "sha256"
    if objects_root.exists():
        for path in objects_root.rglob("*"):
            if not path.is_file():
                continue
            digest = path.name.lower()
            if (
                len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or _sha256_file(path) != digest
            ):
                raise BackupIntegrityError("Artifact object is not content-addressed correctly")
    if manifests_root.exists():
        for path in manifests_root.rglob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                digest = payload["digest"]
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
                raise BackupIntegrityError("Artifact manifest is unreadable") from error
            if not isinstance(digest, str) or path.stem != digest:
                raise BackupIntegrityError("Artifact manifest digest identity is invalid")
            object_path = objects_root / digest[:2] / digest
            if not object_path.is_file() or _sha256_file(object_path) != digest:
                raise BackupIntegrityError("Artifact manifest references a missing or corrupt object")


def _canonical_sha256_ref(value: str, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise BackupIntegrityError(f"{name} must be a canonical SHA-256 reference")
    return value


def _exact_git_sha(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BackupCompatibilityError(
            f"{name} must be an exact lowercase 40-character Git SHA"
        )
    return value


def _load_typed_artifact(
    artifact_root: Path,
    digest_ref: str,
    *,
    expected_kind: str,
) -> dict[str, Any]:
    canonical = _canonical_sha256_ref(digest_ref, name="artifact digest")
    digest = canonical.removeprefix("sha256:")
    manifest_path = (
        artifact_root
        / "manifests"
        / "sha256"
        / digest[:2]
        / f"{digest}.json"
    )
    object_path = artifact_root / "objects" / "sha256" / digest[:2] / digest
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload_bytes = object_path.read_bytes()
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BackupIntegrityError("Referenced evidence artifact is missing") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("algorithm") != "sha256"
        or manifest.get("digest") != digest
        or manifest.get("size_bytes") != len(payload_bytes)
        or _sha256_bytes(payload_bytes) != digest
    ):
        raise BackupIntegrityError(
            "Referenced evidence artifact manifest/object does not verify"
        )
    try:
        payload = json.loads(payload_bytes)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise BackupIntegrityError("Referenced typed artifact is not valid JSON") from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("kind") != expected_kind
    ):
        raise BackupCompatibilityError(
            f"Referenced artifact is not {expected_kind}"
        )
    return payload


def _load_build_identity(
    artifact_root: Path,
    digest_ref: str,
) -> tuple[str, str]:
    payload = _load_typed_artifact(
        artifact_root,
        digest_ref,
        expected_kind="AUTOTRADE_BUILD_IDENTITY",
    )
    source_sha = _exact_git_sha(payload.get("source_sha"), name="build identity source_sha")
    composition = _canonical_sha256_ref(
        payload.get("composition_sha256"),
        name="build identity composition_sha256",
    )
    return source_sha, composition


def create_backup(
    state_dir: str | Path,
    artifact_root: str | Path,
    destination: str | Path,
    *,
    build_identity_sha256: str | None = None,
) -> Path:
    """Create an atomic verified backup bundle.

    The runtime state may continue to exist while the SQLite backup API copies
    the journal. Mutable non-database state is copied with before/after digest
    checks; if it changes during capture, the backup fails rather than silently
    mixing versions.
    """

    state = Path(state_dir)
    artifacts = Path(artifact_root)
    target = Path(destination)
    if target.exists():
        raise BackupError("Backup destination already exists")
    if not state.is_dir():
        raise BackupError("Runtime state directory is missing")
    if _inside(target, state) or _inside(target, artifacts):
        raise BackupError("Backup destination must be outside source directories")
    _validate_artifact_source(artifacts)
    source_sha: str | None = None
    composition_sha256: str | None = None
    if build_identity_sha256 is not None:
        build_identity_sha256 = _canonical_sha256_ref(
            build_identity_sha256,
            name="build_identity_sha256",
        )
        source_sha, composition_sha256 = _load_build_identity(
            artifacts,
            build_identity_sha256,
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".autotrade-backup-", dir=target.parent))
    entries: list[dict[str, Any]] = []
    source_rechecks: list[tuple[Path, str]] = []
    try:
        journal_source = state / "journal.sqlite3"
        journal_target = stage / "state" / "journal.sqlite3"
        journal_digest, journal_size, journal_schema = _backup_sqlite(
            journal_source, journal_target
        )
        entries.append(
            _entry(
                "state/journal.sqlite3",
                journal_digest,
                journal_size,
                "sqlite-journal",
            )
        )

        for source in [
            state / "checkpoint.json",
            state / "learning-evidence.jsonl",
        ]:
            if source.exists():
                relative = Path("state") / source.name
                digest, size = _copy_stable_file(source, stage / relative)
                entries.append(_entry(relative.as_posix(), digest, size, "runtime-state"))
                source_rechecks.append((source, digest))

        intents_root = state / "order-intents"
        if intents_root.exists():
            for source in sorted(intents_root.rglob("*.json")):
                relative = Path("state") / source.relative_to(state)
                digest, size = _copy_stable_file(source, stage / relative)
                entries.append(_entry(relative.as_posix(), digest, size, "order-intent"))
                source_rechecks.append((source, digest))

        for source, kind in _artifact_source_files(artifacts):
            relative = Path("artifacts") / source.relative_to(artifacts)
            digest, size = _copy_stable_file(source, stage / relative)
            entries.append(_entry(relative.as_posix(), digest, size, kind))
            source_rechecks.append((source, digest))

        for source, expected_digest in source_rechecks:
            if not source.is_file() or _sha256_file(source) != expected_digest:
                raise BackupError("Source changed before backup commit")

        if (stage / "state" / "checkpoint.json").is_file() and (
            stage / "state" / "learning-evidence.jsonl"
        ).is_file():
            try:
                # Diagnostic reconstruction opens the journal in WAL mode. Run it
                # against an isolated byte-for-byte verification copy so SQLite
                # sidecars can never become undeclared backup payloads.
                with tempfile.TemporaryDirectory(
                    prefix=".autotrade-backup-check-",
                    dir=target.parent,
                ) as verification_directory:
                    verification_state = Path(verification_directory) / "state"
                    verification_state.mkdir()
                    for name in (
                        "journal.sqlite3",
                        "checkpoint.json",
                        "learning-evidence.jsonl",
                    ):
                        shutil.copy2(stage / "state" / name, verification_state / name)
                    build_diagnostic_snapshot(verification_state)
            except ValueError as error:
                raise BackupIntegrityError(
                    "Runtime state, journal and evidence are not one consistent snapshot"
                ) from error

        unresolved_limits = ["RECONCILIATION_REQUIRED_AFTER_RESTORE"]
        if build_identity_sha256 is None:
            unresolved_limits.append("SOURCE_SHA_UNBOUND")
        manifest = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "created_at": _utc_now(),
            "source_sha": source_sha,
            "source_sha_bound": source_sha is not None,
            "build_identity_sha256": build_identity_sha256,
            "composition_sha256": composition_sha256,
            "unresolved_limits": unresolved_limits,
            "journal_schema_version": journal_schema,
            "reconciliation_required_after_restore": True,
            "runtime_consistency_check": "DURABLE_TRACE_RECONSTRUCTION",
            "files": sorted(entries, key=lambda item: item["path"]),
        }
        manifest_bytes = _canonical_json(manifest)
        _write_bytes_durable(stage / MANIFEST_NAME, manifest_bytes)
        manifest_digest = _sha256_bytes(manifest_bytes)
        _write_bytes_durable(
            stage / MANIFEST_DIGEST_NAME,
            f"sha256:{manifest_digest}\n".encode("ascii"),
        )
        verify_backup(stage)
        os.replace(stage, target)
        return target
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def verify_backup(
    backup_root: str | Path,
    *,
    expected_source_sha: str | None = None,
    expected_build_identity_sha256: str | None = None,
) -> dict[str, Any]:
    """Verify the backup manifest, every payload byte and compatibility gates."""

    root = Path(backup_root)
    manifest_path = root / MANIFEST_NAME
    digest_path = root / MANIFEST_DIGEST_NAME
    try:
        manifest_bytes = manifest_path.read_bytes()
        expected_manifest_digest = digest_path.read_text(encoding="ascii").strip()
        manifest = json.loads(manifest_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BackupIntegrityError("Backup manifest evidence is unreadable") from error

    actual_manifest_digest = f"sha256:{_sha256_bytes(manifest_bytes)}"
    if expected_manifest_digest != actual_manifest_digest:
        raise BackupIntegrityError("Backup manifest digest does not match")

    if not isinstance(manifest, dict) or manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise BackupCompatibilityError("Unsupported backup schema version")
    source_sha = manifest.get("source_sha")
    source_bound = manifest.get("source_sha_bound")
    build_identity = manifest.get("build_identity_sha256")
    composition = manifest.get("composition_sha256")
    unresolved_limits = manifest.get("unresolved_limits")
    if not isinstance(unresolved_limits, list) or any(
        not isinstance(item, str) or not item for item in unresolved_limits
    ):
        raise BackupIntegrityError("Backup unresolved limits are invalid")
    if source_bound is True:
        source_sha = _exact_git_sha(source_sha, name="backup source_sha")
        if not isinstance(build_identity, str):
            raise BackupIntegrityError("Bound backup is missing build identity")
        build_identity = _canonical_sha256_ref(
            build_identity,
            name="backup build_identity_sha256",
        )
        verified_source_sha, verified_composition = _load_build_identity(
            root / "artifacts",
            build_identity,
        )
        if source_sha != verified_source_sha or composition != verified_composition:
            raise BackupIntegrityError(
                "Backup build identity does not match immutable artifact"
            )
        if "SOURCE_SHA_UNBOUND" in unresolved_limits:
            raise BackupIntegrityError("Bound backup cannot declare SOURCE_SHA_UNBOUND")
    elif source_bound is False:
        if source_sha is not None or build_identity is not None or composition is not None:
            raise BackupIntegrityError("Unbound backup contains build identity metadata")
        if "SOURCE_SHA_UNBOUND" not in unresolved_limits:
            raise BackupIntegrityError("Unbound backup must declare SOURCE_SHA_UNBOUND")
    else:
        raise BackupIntegrityError("Backup source-SHA binding flag is invalid")
    if expected_source_sha is not None:
        requested_source = _exact_git_sha(
            expected_source_sha,
            name="expected_source_sha",
        )
        if source_sha != requested_source:
            raise BackupCompatibilityError(
                "Backup source SHA does not match requested build"
            )
    if expected_build_identity_sha256 is not None:
        requested_identity = _canonical_sha256_ref(
            expected_build_identity_sha256,
            name="expected_build_identity_sha256",
        )
        if build_identity != requested_identity:
            raise BackupCompatibilityError(
                "Backup build identity does not match requested artifact"
            )
    if manifest.get("journal_schema_version") != JournalStore.SCHEMA_VERSION:
        raise BackupCompatibilityError("Unsupported backed-up journal schema version")
    if manifest.get("reconciliation_required_after_restore") is not True:
        raise BackupIntegrityError("Restore reconciliation gate is missing")
    if manifest.get("runtime_consistency_check") != "DURABLE_TRACE_RECONSTRUCTION":
        raise BackupIntegrityError("Runtime consistency evidence is missing")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise BackupIntegrityError("Backup file inventory is empty")

    expected_paths: set[str] = set()
    for item in entries:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "sha256",
            "size_bytes",
            "kind",
        }:
            raise BackupIntegrityError("Backup file inventory entry is invalid")
        relative = _safe_relative_path(item["path"])
        normalized = relative.as_posix()
        if normalized in expected_paths:
            raise BackupIntegrityError("Backup file inventory contains duplicates")
        expected_paths.add(normalized)
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise BackupIntegrityError(f"Backup payload is missing: {normalized}")
        digest = item["sha256"]
        if (
            not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or digest != f"sha256:{_sha256_file(path)}"
        ):
            raise BackupIntegrityError(f"Backup payload digest mismatch: {normalized}")
        if item["size_bytes"] != path.stat().st_size:
            raise BackupIntegrityError(f"Backup payload size mismatch: {normalized}")
        if normalized.startswith("artifacts/objects/sha256/"):
            object_digest = path.name.lower()
            if item["sha256"] != f"sha256:{object_digest}":
                raise BackupIntegrityError("Content-addressed artifact object identity mismatch")

    observed_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name not in {MANIFEST_NAME, MANIFEST_DIGEST_NAME}
    }
    if observed_paths != expected_paths:
        raise BackupIntegrityError(
            "Backup contains untracked or missing payload files; "
            f"extra={sorted(observed_paths - expected_paths)}; "
            f"missing={sorted(expected_paths - observed_paths)}"
        )

    journal_relative = "state/journal.sqlite3"
    if journal_relative not in expected_paths:
        raise BackupIntegrityError("Backed-up journal is missing")
    if _sqlite_schema_version(root / journal_relative) != JournalStore.SCHEMA_VERSION:
        raise BackupCompatibilityError("Backed-up journal schema is incompatible")

    artifact_manifests = [
        root / _safe_relative_path(item["path"])
        for item in entries
        if item["kind"] == "artifact-manifest"
    ]
    for path in artifact_manifests:
        try:
            artifact_manifest = json.loads(path.read_text(encoding="utf-8"))
            digest = artifact_manifest["digest"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise BackupIntegrityError("Backed-up artifact manifest is invalid") from error
        object_relative = f"artifacts/objects/sha256/{digest[:2]}/{digest}"
        if object_relative not in expected_paths:
            raise BackupIntegrityError("Backed-up artifact manifest has no matching object")

    return manifest


def restore_backup(
    backup_root: str | Path,
    destination_root: str | Path,
    *,
    expected_source_sha: str | None = None,
    expected_build_identity_sha256: str | None = None,
) -> Path:
    """Restore through staging and leave a mandatory reconciliation marker."""

    backup = Path(backup_root)
    manifest = verify_backup(
        backup,
        expected_source_sha=expected_source_sha,
        expected_build_identity_sha256=expected_build_identity_sha256,
    )
    destination = Path(destination_root)
    if destination.exists():
        raise BackupError("Restore destination already exists")
    if _inside(destination, backup):
        raise BackupError("Restore destination must be outside the backup bundle")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".autotrade-restore-", dir=destination.parent))
    try:
        for item in manifest["files"]:
            relative = _safe_relative_path(item["path"])
            source = backup / relative
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if _sha256_file(target) != item["sha256"].removeprefix("sha256:"):
                raise BackupIntegrityError("Restored payload digest mismatch")

        marker = {
            "schema_version": 2,
            "status": "RECONCILIATION_REQUIRED",
            "restored_at": _utc_now(),
            "reason": "RECONCILIATION_AND_OWNERSHIP_FENCING_REQUIRED",
            "backup_manifest_sha256": (backup / MANIFEST_DIGEST_NAME)
            .read_text(encoding="ascii")
            .strip(),
            "source_sha": manifest["source_sha"],
            "build_identity_sha256": manifest["build_identity_sha256"],
        }
        _write_bytes_durable(stage / RESTORE_MARKER_NAME, _canonical_json(marker))
        os.replace(stage, destination)
        return destination
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _read_restore_marker(destination_root: str | Path) -> dict[str, Any]:
    marker = Path(destination_root) / RESTORE_MARKER_NAME
    if not marker.is_file():
        raise BackupIntegrityError("Restore reconciliation marker is missing")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackupIntegrityError("Restore reconciliation marker is unreadable") from error
    if (
        not isinstance(payload, dict)
        or payload.get("reason") != "RECONCILIATION_AND_OWNERSHIP_FENCING_REQUIRED"
        or not isinstance(payload.get("backup_manifest_sha256"), str)
        or not payload["backup_manifest_sha256"].startswith("sha256:")
    ):
        raise BackupIntegrityError("Restore reconciliation marker is invalid")
    return payload


def _normalize_fencing_evidence(
    values: Sequence[Mapping[str, object]],
) -> tuple[dict[str, str], ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("fencing_evidence must be a sequence of canonical EvidenceRef objects")
    allowed = {"artifact_id", "sha256", "source_uri", "observed_at", "rights_id"}
    required = {"artifact_id", "sha256", "observed_at"}
    normalized: list[dict[str, str]] = []
    artifact_ids: set[str] = set()
    digests: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise BackupError("fencing evidence must use canonical EvidenceRef objects")
        keys = set(value)
        if not required <= keys or not keys <= allowed:
            raise BackupError("fencing evidence does not match canonical EvidenceRef")
        try:
            artifact_id = str(UUID(str(value["artifact_id"])))
        except (ValueError, TypeError, AttributeError) as error:
            raise BackupError("fencing evidence artifact_id must be a UUID") from error
        digest = value["sha256"]
        if (
            not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or len(digest) != 71
            or any(ch not in "0123456789abcdef" for ch in digest[7:])
        ):
            raise BackupError("fencing evidence sha256 must be canonical")
        observed = value["observed_at"]
        if not isinstance(observed, str) or not observed.endswith("Z"):
            raise BackupError("fencing evidence observed_at must be UTC with Z")
        try:
            parsed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        except ValueError as error:
            raise BackupError("fencing evidence observed_at must be an ISO timestamp") from error
        if parsed.tzinfo is None:
            raise BackupError("fencing evidence observed_at must include timezone")
        item: dict[str, str] = {
            "artifact_id": artifact_id,
            "sha256": digest,
            "observed_at": parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        for optional in ("source_uri", "rights_id"):
            if optional in value:
                optional_value = value[optional]
                if not isinstance(optional_value, str) or not optional_value.strip():
                    raise BackupError(f"fencing evidence {optional} must be non-empty")
                item[optional] = optional_value.strip()
        if artifact_id in artifact_ids:
            raise BackupError(
                "fencing evidence artifact_id cannot identify conflicting evidence"
            )
        if digest in digests:
            raise BackupError(
                "unique old-sender fencing evidence bytes are required"
            )
        artifact_ids.add(artifact_id)
        digests.add(digest)
        normalized.append(item)
    if not normalized:
        raise BackupError("unique old-sender fencing evidence is required")
    return tuple(normalized)


def complete_restore_reconciliation(
    destination_root: str | Path,
    *,
    controller: RecoveryController,
    reconciliation: ReconciliationResult,
    fencing_evidence: Sequence[Mapping[str, object]],
    completed_at: str,
) -> dict[str, Any]:
    """Durably clear the restore gate only after reconciliation and sender fencing.

    This does not grant live trading authority. It proves only that restored
    local state has completed the generic recovery gate. Provider qualification,
    risk admission, policy authority and final dispatch fencing remain separate
    mandatory controls.
    """

    root = Path(destination_root)
    marker = _read_restore_marker(root)
    if marker.get("status") == "RECONCILIATION_COMPLETE":
        if restore_requires_reconciliation(root):
            raise BackupIntegrityError("Existing restore completion proof is invalid")
        proof_path = root / RESTORE_COMPLETION_PROOF_NAME
        return json.loads(proof_path.read_text(encoding="utf-8"))
    if marker.get("status") not in {None, "RECONCILIATION_REQUIRED"}:
        raise BackupIntegrityError("Restore reconciliation marker status is invalid")
    if not isinstance(controller, RecoveryController):
        raise TypeError("controller must be RecoveryController")
    if not isinstance(reconciliation, ReconciliationResult):
        raise TypeError("reconciliation must be ReconciliationResult")
    refs = _normalize_fencing_evidence(fencing_evidence)
    if not any(item.get("rights_id") == "recovery:fencing" for item in refs):
        raise BackupError(
            "fencing evidence must include an explicit recovery:fencing proof"
        )
    restored_at = marker.get("restored_at")
    if not isinstance(restored_at, str) or not restored_at.endswith("Z"):
        raise BackupIntegrityError("Restore marker restored_at is invalid")
    try:
        restored = datetime.fromisoformat(restored_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise BackupIntegrityError("Restore marker restored_at is invalid") from error
    if restored.tzinfo is None:
        raise BackupIntegrityError("Restore marker restored_at is invalid")
    restored_utc = restored.astimezone(timezone.utc)
    if any(
        datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00"))
        < restored_utc
        for item in refs
    ):
        raise BackupError("fencing evidence cannot predate the restored runtime")
    if not isinstance(completed_at, str) or not completed_at.strip():
        raise BackupError("completed_at is required")
    try:
        completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise BackupError("completed_at must be an ISO timestamp") from error
    if completed.tzinfo is None:
        raise BackupError("completed_at must include a timezone")
    completed_utc = completed.astimezone(timezone.utc)
    if any(
        datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00")) > completed_utc
        for item in refs
    ):
        raise BackupError("fencing evidence cannot postdate restore completion")

    unresolved = tuple(
        item.attempt_id
        for item in reconciliation.submission_resolutions
        if item.outcome == "UNKNOWN"
    )
    if (
        not reconciliation.complete
        or reconciliation.blocks_new_risk
        or unresolved
    ):
        raise BackupError("Restore cannot complete while reconciliation is incomplete")
    if controller.owner is None:
        raise BackupError("Restore recovery requires an active fenced owner")

    controller.record_reconciliation(consistent=True, uncertainty=unresolved)
    if (
        controller.state is not HostState.READY
        or not controller.provider_reconciled
        or controller.unresolved_attempts
        or not controller.storage_writable
        or not controller.clock_trusted
    ):
        raise BackupError("Recovery controller is not READY after reconciliation")

    resolution_proof: list[dict[str, Any]] = []
    allowed_resolution_outcomes = {
        "PROVEN_ABSENT",
        "OBSERVED_EXECUTION",
        "OBSERVED_WORKING_ORDER",
    }
    for item in reconciliation.submission_resolutions:
        outcome = item.outcome
        if outcome not in allowed_resolution_outcomes:
            raise BackupError(
                "Restore reconciliation contains a non-canonical submission outcome"
            )
        execution_ids = tuple(getattr(item, "provider_execution_ids", ()))
        provider_order_ids = tuple(getattr(item, "provider_order_ids", ()))
        for values, label in (
            (execution_ids, "provider execution"),
            (provider_order_ids, "provider order"),
        ):
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise BackupError(f"{label} identities must be non-empty strings")
            if len(values) != len(set(values)):
                raise BackupError(f"{label} identities must be unique")
        if outcome == "OBSERVED_EXECUTION" and not execution_ids:
            raise BackupError(
                "Observed execution cannot clear restore without exact provider execution identity"
            )
        if outcome == "OBSERVED_WORKING_ORDER" and not provider_order_ids:
            raise BackupError(
                "Observed working order cannot clear restore without exact provider order identity"
            )
        resolution_proof.append(
            {
                "attempt_id": item.attempt_id,
                "client_order_id": item.client_order_id,
                "outcome": outcome,
                "provider_execution_ids": list(execution_ids),
                "provider_order_ids": list(provider_order_ids),
            }
        )

    proof = {
        "schema_version": 1,
        "backup_manifest_sha256": marker["backup_manifest_sha256"],
        "completed_at": completed_utc.isoformat().replace("+00:00", "Z"),
        "owner_id": controller.owner.owner_id,
        "owner_epoch": controller.owner.epoch,
        "fencing_evidence": [dict(item) for item in refs],
        "matched_execution_ids": list(reconciliation.matched_execution_ids),
        "submission_resolutions": resolution_proof,
        "blocking_resources": list(reconciliation.blocking_resources),
    }
    proof_bytes = _canonical_json(proof)
    proof_path = root / RESTORE_COMPLETION_PROOF_NAME
    _write_bytes_durable(proof_path, proof_bytes)
    proof_hash = f"sha256:{_sha256_bytes(proof_bytes)}"

    completed_marker = {
        **marker,
        "schema_version": 2,
        "status": "RECONCILIATION_COMPLETE",
        "completed_at": proof["completed_at"],
        "completion_proof_sha256": proof_hash,
    }
    _write_bytes_durable(root / RESTORE_MARKER_NAME, _canonical_json(completed_marker))
    return proof


def restore_requires_reconciliation(destination_root: str | Path) -> bool:
    """Return False only for a durable, internally consistent completion proof."""

    root = Path(destination_root)
    try:
        marker = _read_restore_marker(root)
    except BackupIntegrityError:
        return True
    if marker.get("status") != "RECONCILIATION_COMPLETE":
        return True
    expected = marker.get("completion_proof_sha256")
    if not isinstance(expected, str) or not expected.startswith("sha256:"):
        return True
    proof_path = root / RESTORE_COMPLETION_PROOF_NAME
    if not proof_path.is_file():
        return True
    try:
        proof_bytes = proof_path.read_bytes()
        proof = json.loads(proof_bytes)
    except (OSError, json.JSONDecodeError):
        return True
    if expected != f"sha256:{_sha256_bytes(proof_bytes)}":
        return True
    if not isinstance(proof, dict):
        return True
    if proof.get("backup_manifest_sha256") != marker.get("backup_manifest_sha256"):
        return True
    if (
        not proof.get("owner_id")
        or type(proof.get("owner_epoch")) is not int
        or proof["owner_epoch"] < 1
    ):
        return True
    if not isinstance(proof.get("fencing_evidence"), list) or not proof["fencing_evidence"]:
        return True
    try:
        evidence = _normalize_fencing_evidence(proof["fencing_evidence"])
        completed_at = proof.get("completed_at")
        if not isinstance(completed_at, str) or not completed_at.endswith("Z"):
            return True
        completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        restored_at = marker.get("restored_at")
        if not isinstance(restored_at, str) or not restored_at.endswith("Z"):
            return True
        restored = datetime.fromisoformat(restored_at.replace("Z", "+00:00"))
        if any(
            datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00")) > completed
            or datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00")) < restored
            for item in evidence
        ):
            return True
        if not any(
            item.get("rights_id") == "recovery:fencing"
            for item in evidence
        ):
            return True
    except (BackupError, TypeError, ValueError):
        return True
    if proof.get("blocking_resources") != []:
        return True
    resolutions = proof.get("submission_resolutions")
    if not isinstance(resolutions, list):
        return True
    for item in resolutions:
        if not isinstance(item, dict):
            return True
        outcome = item.get("outcome")
        if outcome not in {
            "PROVEN_ABSENT",
            "OBSERVED_EXECUTION",
            "OBSERVED_WORKING_ORDER",
        }:
            return True
        execution_ids = item.get("provider_execution_ids")
        provider_order_ids = item.get("provider_order_ids")
        for values in (execution_ids, provider_order_ids):
            if (
                not isinstance(values, list)
                or any(not isinstance(value, str) or not value for value in values)
                or len(values) != len(set(values))
            ):
                return True
        if outcome == "OBSERVED_EXECUTION" and not execution_ids:
            return True
        if outcome == "OBSERVED_WORKING_ORDER" and not provider_order_ids:
            return True
    return False
