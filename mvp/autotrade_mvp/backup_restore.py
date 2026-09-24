"""Verified backup/restore foundation for the canonical journal and artifact store.

This module has no trading or recovery authority. A successful restore always
emits a durable reconciliation-required marker; another canonical recovery
component must clear that condition after fencing and account reconciliation.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Any

from autotrade_research.artifacts.durable_publish import atomic_write_json, sha256_file
from autotrade_research.artifacts.resource_lock import ResourceLock
from autotrade_research.artifacts.store import ArtifactStore
from autotrade_research.io.strict_json import strict_json_loads

from .persistence import JournalStore


BACKUP_SCHEMA_VERSION = 1
BACKUP_MANIFEST = "backup-manifest.json"
BACKUP_MANIFEST_SHA256 = "backup-manifest.sha256"
RESTORE_MARKER = "RESTORE_RECONCILIATION_REQUIRED.json"


class BackupRestoreError(ValueError):
    """Raised when a backup or restore cannot be proven safe."""


def _source_sha(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise BackupRestoreError("source_sha must be an exact lowercase 40-character Git SHA")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_text_durable(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def _sqlite_schema_version(path: Path) -> int:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise BackupRestoreError("journal integrity_check failed")
            row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise BackupRestoreError("journal database is unreadable") from error
    if row is None or row[0] is None:
        raise BackupRestoreError("journal schema version is unavailable")
    return int(row[0])


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        source_connection = sqlite3.connect(
            f"file:{source}?mode=ro",
            uri=True,
            timeout=30,
            isolation_level=None,
        )
        destination_connection = sqlite3.connect(destination, timeout=30, isolation_level=None)
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
            source_connection.close()
    except sqlite3.Error as error:
        raise BackupRestoreError("SQLite backup failed") from error
    if _sqlite_schema_version(destination) != JournalStore.SCHEMA_VERSION:
        raise BackupRestoreError("journal backup schema is incompatible")


def _artifact_relative_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for base in (root / "manifests", root / "objects" / "sha256"):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_symlink():
                raise BackupRestoreError("artifact backup refuses symbolic links")
            if path.is_file():
                files.append(path.relative_to(root))
    return tuple(sorted(files, key=lambda item: item.as_posix()))


def _copy_artifacts(source_root: Path, destination_root: Path) -> None:
    store = ArtifactStore(source_root)
    with ResourceLock(store.lock_path):
        audit = store.audit()
        if audit.missing_objects or audit.corrupt_objects:
            raise BackupRestoreError("artifact store is not internally consistent")
        for relative in _artifact_relative_files(source_root):
            source = source_root / relative
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)


def _inventory(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    journal = root / "journal.sqlite3"
    if not journal.is_file() or journal.is_symlink():
        raise BackupRestoreError("backup journal file is missing or unsafe")
    result["journal.sqlite3"] = "sha256:" + sha256_file(journal)
    artifacts = root / "artifacts"
    for relative in _artifact_relative_files(artifacts):
        path = artifacts / relative
        key = "artifacts/" + relative.as_posix()
        result[key] = "sha256:" + sha256_file(path)
    return result


def _manifest_path(bundle: Path) -> Path:
    return bundle / BACKUP_MANIFEST


def _load_verified_manifest(bundle: Path) -> dict[str, Any]:
    manifest_path = _manifest_path(bundle)
    digest_path = bundle / BACKUP_MANIFEST_SHA256
    if not manifest_path.is_file() or not digest_path.is_file():
        raise BackupRestoreError("backup manifest or digest is missing")
    try:
        expected = digest_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise BackupRestoreError("backup manifest digest is unreadable") from error
    actual = "sha256:" + sha256_file(manifest_path)
    if expected != actual:
        raise BackupRestoreError("backup manifest digest mismatch")
    try:
        manifest = strict_json_loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise BackupRestoreError("backup manifest is unreadable") from error
    if type(manifest) is not dict or manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise BackupRestoreError("backup schema is incompatible")
    _source_sha(manifest.get("source_sha"))
    if manifest.get("journal_schema_version") != JournalStore.SCHEMA_VERSION:
        raise BackupRestoreError("journal schema is incompatible with this runtime")
    if manifest.get("artifact_schema_version") != ArtifactStore.SCHEMA_VERSION:
        raise BackupRestoreError("artifact schema is incompatible with this runtime")
    files = manifest.get("files")
    if type(files) is not dict or not files:
        raise BackupRestoreError("backup file inventory is missing")
    for relative, digest in files.items():
        if not isinstance(relative, str) or not relative or relative.startswith("/") or ".." in Path(relative).parts:
            raise BackupRestoreError("backup inventory contains an unsafe path")
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        ):
            raise BackupRestoreError("backup inventory contains an invalid digest")
        path = bundle / relative
        if not path.is_file() or path.is_symlink():
            raise BackupRestoreError(f"backup file is missing or unsafe: {relative}")
        if "sha256:" + sha256_file(path) != digest:
            raise BackupRestoreError(f"backup file hash mismatch: {relative}")
    return manifest


def create_backup(
    *,
    journal_path: str | Path,
    artifact_root: str | Path,
    destination: str | Path,
    source_sha: str,
) -> dict[str, Any]:
    """Create one verified point-in-time backup without copying a live DB file."""

    exact_sha = _source_sha(source_sha)
    journal = Path(journal_path)
    artifacts = Path(artifact_root)
    target = Path(destination)
    if not journal.is_file():
        raise FileNotFoundError(journal)
    if not artifacts.is_dir():
        raise FileNotFoundError(artifacts)
    if target.exists():
        raise BackupRestoreError("backup destination must not already exist")
    target.parent.mkdir(parents=True, exist_ok=True)

    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        _sqlite_backup(journal, stage / "journal.sqlite3")
        _copy_artifacts(artifacts, stage / "artifacts")
        copied_store = ArtifactStore(stage / "artifacts")
        audit = copied_store.audit()
        if audit.missing_objects or audit.corrupt_objects:
            raise BackupRestoreError("copied artifact store failed verification")

        manifest = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "source_sha": exact_sha,
            "journal_schema_version": JournalStore.SCHEMA_VERSION,
            "artifact_schema_version": ArtifactStore.SCHEMA_VERSION,
            "created_at": _utc_now(),
            "files": _inventory(stage),
            "unresolved_limits": [
                "restore requires external fencing and reconciliation before new exposure",
                "backup cannot guarantee data never durably committed before disk destruction",
            ],
        }
        atomic_write_json(_manifest_path(stage), manifest)
        manifest_digest = "sha256:" + sha256_file(_manifest_path(stage))
        _write_text_durable(stage / BACKUP_MANIFEST_SHA256, manifest_digest + "\n")
        os.replace(stage, target)
        stage = None
        return manifest
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)


def restore_backup(
    *,
    bundle: str | Path,
    destination: str | Path,
    expected_source_sha: str | None = None,
) -> Path:
    """Restore into a new directory and leave new exposure explicitly blocked."""

    source = Path(bundle)
    target = Path(destination)
    if target.exists():
        raise BackupRestoreError("restore destination must not already exist")
    manifest = _load_verified_manifest(source)
    if expected_source_sha is not None and manifest["source_sha"] != _source_sha(expected_source_sha):
        raise BackupRestoreError("backup source SHA does not match requested build")
    if _sqlite_schema_version(source / "journal.sqlite3") != JournalStore.SCHEMA_VERSION:
        raise BackupRestoreError("backup journal schema is incompatible")

    source_store = ArtifactStore(source / "artifacts")
    source_audit = source_store.audit()
    if source_audit.missing_objects or source_audit.corrupt_objects:
        raise BackupRestoreError("backup artifact store failed verification")

    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        for relative in manifest["files"]:
            src = source / relative
            dst = stage / relative
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)

        if _inventory(stage) != manifest["files"]:
            raise BackupRestoreError("restored file inventory does not match backup")
        if _sqlite_schema_version(stage / "journal.sqlite3") != JournalStore.SCHEMA_VERSION:
            raise BackupRestoreError("restored journal verification failed")

        restored_store = ArtifactStore(stage / "artifacts")
        restored_audit = restored_store.audit()
        if restored_audit.missing_objects or restored_audit.corrupt_objects:
            raise BackupRestoreError("restored artifact store failed verification")

        marker = {
            "schema_version": 1,
            "source_sha": manifest["source_sha"],
            "backup_manifest_sha256": (
                "sha256:" + sha256_file(_manifest_path(source))
            ),
            "restored_at": _utc_now(),
            "reconciliation_required": True,
            "fencing_required": True,
            "new_exposure_allowed": False,
            "trading_authority_granted": False,
        }
        atomic_write_json(stage / RESTORE_MARKER, marker)
        os.replace(stage, target)
        stage = None
        return target / RESTORE_MARKER
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
