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
from typing import Any

from .diagnostics import build_diagnostic_snapshot
from .persistence import JournalStore


BACKUP_SCHEMA_VERSION = 1
MANIFEST_NAME = "backup-manifest.json"
MANIFEST_DIGEST_NAME = "backup-manifest.sha256"
RESTORE_MARKER_NAME = "RESTORE_RECONCILIATION_REQUIRED.json"
_SHA256_HEX = frozenset("0123456789abcdef")


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
    if "\\" in raw or "\x00" in raw:
        raise BackupIntegrityError("Backup manifest path must use canonical POSIX separators")
    if len(raw) >= 2 and raw[0].isalpha() and raw[1] == ":":
        raise BackupIntegrityError("Backup manifest path must not use Windows drive notation")
    pure = PurePosixPath(raw)
    if (
        pure.is_absolute()
        or pure.as_posix() != raw
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise BackupIntegrityError("Backup manifest path is unsafe")
    return Path(*pure.parts)


def _valid_sha256_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in _SHA256_HEX for character in value)
    )


def _expected_kind(path: str) -> str:
    parts = PurePosixPath(path).parts
    if parts == ("state", "journal.sqlite3"):
        return "sqlite-journal"
    if parts in {
        ("state", "checkpoint.json"),
        ("state", "learning-evidence.jsonl"),
    }:
        return "runtime-state"
    if len(parts) >= 3 and parts[:2] == ("state", "order-intents") and parts[-1].endswith(".json"):
        return "order-intent"
    if (
        len(parts) == 5
        and parts[:3] == ("artifacts", "objects", "sha256")
        and len(parts[3]) == 2
        and _valid_sha256_digest(parts[4])
        and parts[3] == parts[4][:2]
    ):
        return "artifact-object"
    if (
        len(parts) == 5
        and parts[:3] == ("artifacts", "manifests", "sha256")
        and len(parts[3]) == 2
        and parts[4].endswith(".json")
        and _valid_sha256_digest(parts[4][:-5])
        and parts[3] == parts[4][:-5][:2]
    ):
        return "artifact-manifest"
    raise BackupIntegrityError(f"Backup payload path is outside the canonical inventory: {path}")


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
    try:
        versions = [int(row[0]) for row in rows]
    except (TypeError, ValueError) as error:
        raise BackupIntegrityError(
            "Durable journal schema migration history is invalid"
        ) from error
    if versions != list(range(1, versions[-1] + 1)):
        raise BackupIntegrityError(
            "Durable journal schema migration history is not contiguous"
        )
    return versions[-1]


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
            digest = path.name
            if (
                not _valid_sha256_digest(digest)
                or path.parent.name != digest[:2]
                or path.parent.parent != objects_root
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
            declared_size = payload.get("size_bytes")
            if (
                payload.get("algorithm") != "sha256"
                or not _valid_sha256_digest(digest)
                or path.stem != digest
                or path.parent.name != digest[:2]
                or path.parent.parent != manifests_root
                or isinstance(declared_size, bool)
                or not isinstance(declared_size, int)
                or declared_size < 0
            ):
                raise BackupIntegrityError("Artifact manifest digest identity is invalid")
            object_path = objects_root / digest[:2] / digest
            if (
                not object_path.is_file()
                or _sha256_file(object_path) != digest
                or object_path.stat().st_size != declared_size
            ):
                raise BackupIntegrityError("Artifact manifest references a missing or corrupt object")


def create_backup(
    state_dir: str | Path,
    artifact_root: str | Path,
    destination: str | Path,
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

        checkpoint_present = (stage / "state" / "checkpoint.json").is_file()
        learning_evidence_present = (
            stage / "state" / "learning-evidence.jsonl"
        ).is_file()
        if checkpoint_present != learning_evidence_present:
            raise BackupIntegrityError(
                "Runtime consistency evidence is partial; checkpoint and learning evidence "
                "must be captured together or both be absent"
            )

        if checkpoint_present:
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
            runtime_consistency_check = "DURABLE_TRACE_RECONSTRUCTION"
        else:
            runtime_consistency_check = "JOURNAL_ONLY"

        manifest = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "created_at": _utc_now(),
            "journal_schema_version": journal_schema,
            "reconciliation_required_after_restore": True,
            "runtime_consistency_check": runtime_consistency_check,
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


def verify_backup(backup_root: str | Path) -> dict[str, Any]:
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
    if manifest.get("journal_schema_version") != JournalStore.SCHEMA_VERSION:
        raise BackupCompatibilityError("Unsupported backed-up journal schema version")
    if manifest.get("reconciliation_required_after_restore") is not True:
        raise BackupIntegrityError("Restore reconciliation gate is missing")
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
        expected_kind = _expected_kind(normalized)
        if item["kind"] != expected_kind:
            raise BackupIntegrityError(
                f"Backup payload kind does not match canonical path: {normalized}"
            )
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
        size_bytes = item["size_bytes"]
        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
            or size_bytes != path.stat().st_size
        ):
            raise BackupIntegrityError(f"Backup payload size mismatch: {normalized}")
        if normalized.startswith("artifacts/objects/sha256/"):
            object_digest = path.name.lower()
            if item["sha256"] != f"sha256:{object_digest}":
                raise BackupIntegrityError("Content-addressed artifact object identity mismatch")

    checkpoint_present = "state/checkpoint.json" in expected_paths
    learning_evidence_present = "state/learning-evidence.jsonl" in expected_paths
    if checkpoint_present != learning_evidence_present:
        raise BackupIntegrityError(
            "Backup runtime consistency evidence is partial"
        )
    expected_consistency_check = (
        "DURABLE_TRACE_RECONSTRUCTION"
        if checkpoint_present
        else "JOURNAL_ONLY"
    )
    if manifest.get("runtime_consistency_check") != expected_consistency_check:
        raise BackupIntegrityError(
            "Runtime consistency claim does not match the backup inventory"
        )

    observed_paths = {
        relative
        for path in root.rglob("*")
        if path.is_file()
        for relative in (path.relative_to(root).as_posix(),)
        if relative not in {MANIFEST_NAME, MANIFEST_DIGEST_NAME}
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
        declared_size = artifact_manifest.get("size_bytes")
        expected_manifest_relative = (
            f"artifacts/manifests/sha256/{digest[:2]}/{digest}.json"
            if _valid_sha256_digest(digest)
            else ""
        )
        actual_manifest_relative = path.relative_to(root).as_posix()
        if (
            artifact_manifest.get("algorithm") != "sha256"
            or not _valid_sha256_digest(digest)
            or actual_manifest_relative != expected_manifest_relative
            or isinstance(declared_size, bool)
            or not isinstance(declared_size, int)
            or declared_size < 0
        ):
            raise BackupIntegrityError("Backed-up artifact manifest identity is invalid")
        object_relative = f"artifacts/objects/sha256/{digest[:2]}/{digest}"
        if object_relative not in expected_paths:
            raise BackupIntegrityError("Backed-up artifact manifest has no matching object")
        object_path = root / _safe_relative_path(object_relative)
        if _sha256_file(object_path) != digest or object_path.stat().st_size != declared_size:
            raise BackupIntegrityError("Backed-up artifact manifest does not match its object")

    return manifest


def restore_backup(backup_root: str | Path, destination_root: str | Path) -> Path:
    """Restore through staging and leave a mandatory reconciliation marker."""

    backup = Path(backup_root)
    manifest = verify_backup(backup)
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
            "schema_version": 1,
            "restored_at": _utc_now(),
            "reason": "RECONCILIATION_AND_OWNERSHIP_FENCING_REQUIRED",
            "backup_manifest_sha256": (backup / MANIFEST_DIGEST_NAME)
            .read_text(encoding="ascii")
            .strip(),
        }
        _write_bytes_durable(stage / RESTORE_MARKER_NAME, _canonical_json(marker))
        os.replace(stage, destination)
        return destination
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def restore_requires_reconciliation(destination_root: str | Path) -> bool:
    """Fail closed until a future qualified reconciliation flow is implemented.

    Missing, unreadable or merely present restore metadata can never grant
    execution authority. The current recovery foundation has no gate-clearing
    operation, so every restored or uncertain state requires reconciliation.
    """

    marker = Path(destination_root) / RESTORE_MARKER_NAME
    if not marker.is_file():
        return True
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    if not isinstance(payload, dict) or payload.get("reason") != "RECONCILIATION_AND_OWNERSHIP_FENCING_REQUIRED":
        return True
    return True
