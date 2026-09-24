from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from typing import Iterable
import zipfile


MANIFEST_NAME = "backup-manifest.json"
SCHEMA_VERSION = 1
DEFAULT_STATE_PATHS = (
    "checkpoint.json",
    "learning-evidence.jsonl",
    "journal.sqlite3",
    "order-intents",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(path: Path) -> str:
    text = path.as_posix()
    if path.is_absolute() or text.startswith("../") or "/../" in text or text in {"", "."}:
        raise ValueError(f"Unsafe backup path: {text}")
    return text


def _collect_regular_files(root: Path, relative: str) -> list[Path]:
    candidate = root / relative
    if not candidate.exists():
        return []
    if candidate.is_symlink():
        raise ValueError(f"Refusing symbolic link in state: {relative}")
    if candidate.is_file():
        return [candidate]
    if not candidate.is_dir():
        raise ValueError(f"Unsupported state entry: {relative}")
    files: list[Path] = []
    for path in sorted(candidate.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Refusing symbolic link in state: {path.relative_to(root)}")
        if path.is_file():
            files.append(path)
        elif not path.is_dir():
            raise ValueError(f"Unsupported state entry: {path.relative_to(root)}")
    return files


def _snapshot_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as destination_db:
        source_db.backup(destination_db)
        row = destination_db.execute("PRAGMA integrity_check").fetchone()
        if row is None or row[0] != "ok":
            raise ValueError("SQLite backup failed integrity check")


def create_backup(
    state_dir: str | Path,
    archive_path: str | Path,
    *,
    include_paths: Iterable[str] = DEFAULT_STATE_PATHS,
) -> dict:
    """Create an integrity-checked, network-free state backup.

    SQLite is copied through the SQLite backup API instead of copying a live
    database file and its WAL sidecars. Symbolic links are rejected.
    """

    root = Path(state_dir).resolve()
    archive = Path(archive_path)
    if not root.exists() or not root.is_dir():
        raise ValueError("state_dir must be an existing directory")
    archive.parent.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []
    with TemporaryDirectory(prefix="autotrade-backup-") as temporary:
        stage = Path(temporary)

        for requested in include_paths:
            relative = _safe_relative(Path(requested))
            source = root / relative
            if not source.exists():
                continue

            if relative == "journal.sqlite3":
                if source.is_symlink() or not source.is_file():
                    raise ValueError("journal.sqlite3 must be a regular file")
                staged = stage / relative
                _snapshot_sqlite(source, staged)
                sources = [(staged, Path(relative))]
            else:
                sources = [
                    (path, path.relative_to(root))
                    for path in _collect_regular_files(root, relative)
                ]

            for source_path, relative_path in sources:
                clean = _safe_relative(relative_path)
                target = stage / clean
                if source_path != target:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_path, target)
                entries.append(
                    {
                        "path": clean,
                        "size": target.stat().st_size,
                        "sha256": _sha256(target),
                    }
                )

        entries.sort(key=lambda item: item["path"])
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "created_at": _utc_now(),
            "files": entries,
        }
        (stage / MANIFEST_NAME).write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

        temporary_archive = archive.with_suffix(archive.suffix + ".tmp")
        try:
            with zipfile.ZipFile(
                temporary_archive,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as handle:
                for path in sorted(stage.rglob("*")):
                    if path.is_file():
                        handle.write(path, path.relative_to(stage).as_posix())
            os.replace(temporary_archive, archive)
        finally:
            if temporary_archive.exists():
                temporary_archive.unlink()

    return manifest


def _load_manifest(handle: zipfile.ZipFile) -> dict:
    try:
        raw = handle.read(MANIFEST_NAME)
        manifest = json.loads(raw.decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Backup manifest is missing or corrupt") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported backup manifest schema")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("Backup manifest files are corrupt")
    return manifest


def restore_backup(archive_path: str | Path, target_dir: str | Path) -> dict:
    """Verify and restore a backup into a new directory.

    Existing targets are rejected so recovery cannot overwrite live state.
    Archive paths, duplicate members and unexpected files fail closed.
    """

    archive = Path(archive_path)
    target = Path(target_dir)
    if target.exists():
        raise ValueError("target_dir must not already exist")
    if not archive.is_file():
        raise ValueError("archive_path must be an existing file")

    with TemporaryDirectory(prefix="autotrade-restore-", dir=target.parent) as temporary:
        stage = Path(temporary) / "state"
        stage.mkdir()

        with zipfile.ZipFile(archive, "r") as handle:
            manifest = _load_manifest(handle)
            members = handle.infolist()
            names = [member.filename for member in members if not member.is_dir()]
            if len(names) != len(set(names)):
                raise ValueError("Backup contains duplicate members")

            expected = {MANIFEST_NAME}
            records: dict[str, dict] = {}
            for item in manifest["files"]:
                if not isinstance(item, dict):
                    raise ValueError("Backup manifest entry is corrupt")
                path = _safe_relative(Path(str(item.get("path", ""))))
                if path in records:
                    raise ValueError("Backup manifest contains duplicate paths")
                if not isinstance(item.get("size"), int) or item["size"] < 0:
                    raise ValueError("Backup manifest size is corrupt")
                digest = item.get("sha256")
                if not isinstance(digest, str) or len(digest) != 64:
                    raise ValueError("Backup manifest hash is corrupt")
                records[path] = item
                expected.add(path)

            if set(names) != expected:
                raise ValueError("Backup contents do not match manifest")

            for name, item in records.items():
                info = handle.getinfo(name)
                if info.is_dir():
                    raise ValueError("Manifest entry points to a directory")
                destination = stage / name
                resolved_parent = destination.parent.resolve()
                if stage.resolve() not in (resolved_parent, *resolved_parent.parents):
                    raise ValueError("Backup path escapes restore directory")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with handle.open(info, "r") as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                if destination.stat().st_size != item["size"] or _sha256(destination) != item["sha256"]:
                    raise ValueError(f"Backup integrity check failed for {name}")

        journal = stage / "journal.sqlite3"
        if journal.exists():
            with sqlite3.connect(journal) as database:
                row = database.execute("PRAGMA integrity_check").fetchone()
                if row is None or row[0] != "ok":
                    raise ValueError("Restored SQLite journal failed integrity check")

        os.replace(stage, target)

    return manifest
