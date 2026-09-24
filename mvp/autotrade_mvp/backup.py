"""Verified backup and fail-closed restore for the simulated runtime."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
from typing import Any
from uuid import uuid4

BACKUP_SCHEMA_VERSION = 1
MANIFEST_NAME = "backup-manifest.json"
RESTORE_GATE_NAME = "restore-gate.json"
JOURNAL_NAME = "journal.sqlite3"


class BackupError(RuntimeError):
    pass


class BackupIntegrityError(BackupError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(raw: Any) -> PurePosixPath:
    if not isinstance(raw, str):
        raise BackupIntegrityError("backup entry path must be text")
    value = PurePosixPath(raw)
    if value.is_absolute() or not value.parts or any(part in {"", ".", ".."} for part in value.parts):
        raise BackupIntegrityError("backup entry path is unsafe")
    return value


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        with source.open("rb") as source_handle, temporary.open("wb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _sqlite_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        source_uri = f"{source.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True, timeout=30) as source_db:
            with sqlite3.connect(temporary, timeout=30) as target_db:
                source_db.backup(target_db)
                result = target_db.execute("PRAGMA quick_check").fetchone()
                if result is None or result[0] != "ok":
                    raise BackupIntegrityError("SQLite backup failed integrity check")
                target_db.commit()
        os.replace(temporary, target)
    except sqlite3.Error as error:
        raise BackupIntegrityError("SQLite backup could not be created safely") from error
    finally:
        if temporary.exists():
            temporary.unlink()


def _source_files(root: Path, *, skip_journal: bool) -> list[Path]:
    files: list[Path] = []
    if not root.exists():
        return files
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if skip_journal and relative == JOURNAL_NAME:
            continue
        if relative == RESTORE_GATE_NAME:
            continue
        if path.name.endswith("-wal") or path.name.endswith("-shm"):
            continue
        if path.name.startswith(".") and (path.name.endswith(".tmp") or ".staging" in path.name):
            continue
        files.append(path)
    return files


def _manifest_entry(scope: str, root: Path, path: Path) -> dict[str, Any]:
    return {
        "scope": scope,
        "path": path.relative_to(root).as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def create_backup_bundle(
    state_dir: str | Path,
    bundle_dir: str | Path,
    *,
    artifact_store_dir: str | Path | None = None,
) -> Path:
    """Create a verified bundle.

    Non-SQLite writers must be quiesced by the caller. The SQLite journal is
    snapshotted through the online backup API so a WAL-active database is safe.
    """

    state_root = Path(state_dir)
    destination = Path(bundle_dir)
    artifact_root = Path(artifact_store_dir) if artifact_store_dir is not None else None
    if not state_root.is_dir():
        raise BackupError("state directory does not exist")
    if destination.exists():
        raise BackupError("backup destination already exists")
    if artifact_root is not None and not artifact_root.is_dir():
        raise BackupError("artifact store directory does not exist")

    staging = destination.with_name(f".{destination.name}.{uuid4().hex}.staging")
    entries: list[dict[str, Any]] = []
    try:
        state_target = staging / "state"
        state_target.mkdir(parents=True)

        journal = state_root / JOURNAL_NAME
        if journal.is_file():
            journal_target = state_target / JOURNAL_NAME
            _sqlite_backup(journal, journal_target)
            entries.append(_manifest_entry("state", state_target, journal_target))

        for source in _source_files(state_root, skip_journal=True):
            target = state_target / source.relative_to(state_root)
            _copy_file(source, target)
            entries.append(_manifest_entry("state", state_target, target))

        if artifact_root is not None:
            artifact_target = staging / "artifacts"
            artifact_target.mkdir(parents=True)
            for source in _source_files(artifact_root, skip_journal=False):
                target = artifact_target / source.relative_to(artifact_root)
                _copy_file(source, target)
                entries.append(_manifest_entry("artifacts", artifact_target, target))

        if not entries:
            raise BackupError("nothing exists to back up")

        manifest = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_mode": "SIMULATION",
            "snapshot_requirement": "NON_SQLITE_WRITERS_QUIESCED",
            "live_execution_authority": "NOT_INCLUDED",
            "entries": sorted(entries, key=lambda row: (row["scope"], row["path"])),
        }
        _atomic_json(staging / MANIFEST_NAME, manifest)
        verify_backup_bundle(staging)
        os.replace(staging, destination)
        return destination
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _load_manifest(bundle: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((bundle / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackupIntegrityError("backup manifest is missing or unreadable") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise BackupIntegrityError("unsupported backup manifest schema")
    if manifest.get("source_mode") != "SIMULATION":
        raise BackupIntegrityError("backup source mode is not supported")
    if manifest.get("live_execution_authority") != "NOT_INCLUDED":
        raise BackupIntegrityError("backup must not contain live execution authority")
    if not isinstance(manifest.get("entries"), list) or not manifest["entries"]:
        raise BackupIntegrityError("backup manifest has no entries")
    return manifest


def verify_backup_bundle(bundle_dir: str | Path) -> dict[str, Any]:
    bundle = Path(bundle_dir)
    manifest = _load_manifest(bundle)
    declared: set[str] = set()

    for raw in manifest["entries"]:
        if not isinstance(raw, dict) or set(raw) != {"scope", "path", "size_bytes", "sha256"}:
            raise BackupIntegrityError("backup entry structure is invalid")
        scope = raw["scope"]
        if scope not in {"state", "artifacts"}:
            raise BackupIntegrityError("backup entry scope is invalid")
        relative = _safe_relative(raw["path"])
        key = f"{scope}/{relative.as_posix()}"
        if key in declared:
            raise BackupIntegrityError("backup contains a duplicate entry")
        declared.add(key)
        path = bundle / scope / Path(*relative.parts)
        if not path.is_file():
            raise BackupIntegrityError(f"backup entry is missing: {key}")
        if not isinstance(raw["size_bytes"], int) or isinstance(raw["size_bytes"], bool) or raw["size_bytes"] < 0:
            raise BackupIntegrityError("backup entry size is invalid")
        if path.stat().st_size != raw["size_bytes"]:
            raise BackupIntegrityError(f"backup entry size mismatch: {key}")
        expected = raw["sha256"]
        if (
            not isinstance(expected, str)
            or len(expected) != 64
            or any(ch not in "0123456789abcdef" for ch in expected)
        ):
            raise BackupIntegrityError("backup entry hash is invalid")
        if _sha256_file(path) != expected:
            raise BackupIntegrityError(f"backup entry hash mismatch: {key}")

    observed: set[str] = set()
    for scope in ("state", "artifacts"):
        root = bundle / scope
        if root.exists():
            observed.update(path.relative_to(bundle).as_posix() for path in root.rglob("*") if path.is_file())
    if observed != declared:
        raise BackupIntegrityError("backup contains undeclared or missing files")

    journal = bundle / "state" / JOURNAL_NAME
    if journal.is_file():
        try:
            uri = f"{journal.resolve().as_uri()}?mode=ro"
            with sqlite3.connect(uri, uri=True) as connection:
                result = connection.execute("PRAGMA quick_check").fetchone()
                if result is None or result[0] != "ok":
                    raise BackupIntegrityError("backup journal fails SQLite integrity check")
        except sqlite3.Error as error:
            raise BackupIntegrityError("backup journal is unreadable") from error
    return manifest


def restore_backup_bundle(bundle_dir: str | Path, restore_root: str | Path) -> Path:
    """Restore into a clean directory and install a fail-closed authority gate."""

    bundle = Path(bundle_dir)
    manifest = verify_backup_bundle(bundle)
    destination = Path(restore_root)
    if destination.exists():
        raise BackupError("restore destination must not already exist")
    staging = destination.with_name(f".{destination.name}.{uuid4().hex}.staging")

    try:
        for raw in manifest["entries"]:
            relative = _safe_relative(raw["path"])
            source = bundle / raw["scope"] / Path(*relative.parts)
            target = staging / raw["scope"] / Path(*relative.parts)
            _copy_file(source, target)

        gate = {
            "schema_version": 1,
            "live_execution_allowed": False,
            "reason": "RECONCILIATION_REQUIRED_AFTER_RESTORE",
            "restored_from_manifest_sha256": _sha256_file(bundle / MANIFEST_NAME),
        }
        _atomic_json(staging / "state" / RESTORE_GATE_NAME, gate)

        for raw in manifest["entries"]:
            relative = _safe_relative(raw["path"])
            restored = staging / raw["scope"] / Path(*relative.parts)
            if restored.stat().st_size != raw["size_bytes"] or _sha256_file(restored) != raw["sha256"]:
                raise BackupIntegrityError("restored payload does not match backup manifest")

        os.replace(staging, destination)
        return destination
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def restore_requires_reconciliation(restored_state_dir: str | Path) -> bool:
    gate_path = Path(restored_state_dir) / RESTORE_GATE_NAME
    try:
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return gate.get("live_execution_allowed") is not True
