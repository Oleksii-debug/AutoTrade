from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path, PurePath
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.backup import (
    BACKUP_SCHEMA_VERSION,
    BackupCompatibilityError,
    BackupError,
    BackupIntegrityError,
    _safe_relative_path,
    create_backup,
    restore_backup,
    restore_requires_reconciliation,
    verify_backup,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.pipeline import run_vertical_slice


def _reseal_backup_manifest(backup: Path) -> None:
    manifest_path = backup / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest["files"]:
        path = backup / Path(*PurePath(item["path"]).parts)
        item["sha256"] = f"sha256:{sha256(path.read_bytes()).hexdigest()}"
        item["size_bytes"] = path.stat().st_size
    payload = (
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    manifest_path.write_bytes(payload)
    (backup / "backup-manifest.sha256").write_text(
        f"sha256:{sha256(payload).hexdigest()}\n",
        encoding="ascii",
    )


def _artifact_store(root: Path) -> str:
    payload = b"immutable-evidence"
    digest = sha256(payload).hexdigest()
    object_path = root / "objects" / "sha256" / digest[:2] / digest
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(payload)
    manifest_path = root / "manifests" / "sha256" / digest[:2] / f"{digest}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "algorithm": "sha256",
                "digest": digest,
                "size_bytes": len(payload),
                "media_type": "application/octet-stream",
                "rights_basis": "first-party-test-evidence",
            }
        ),
        encoding="utf-8",
    )
    return digest


class BackupRestoreTests(unittest.TestCase):
    def _build_sources(self, root: Path) -> tuple[Path, Path]:
        state = root / "state"
        artifacts = root / "artifacts"
        run_vertical_slice([100, 101, 102, 103], state)
        _artifact_store(artifacts)
        return state, artifacts

    def test_manifest_paths_reject_windows_and_noncanonical_forms(self):
        self.assertEqual(
            _safe_relative_path("state/journal.sqlite3").as_posix(),
            "state/journal.sqlite3",
        )
        invalid_paths = (
            "state\\\\journal.sqlite3",
            "C:/state/journal.sqlite3",
            "C:state/journal.sqlite3",
            "state//journal.sqlite3",
            "state/journal.sqlite3/",
        )
        for raw in invalid_paths:
            with self.subTest(raw=raw):
                with self.assertRaises(BackupIntegrityError):
                    _safe_relative_path(raw)

    def test_wal_active_backup_verifies_and_restore_is_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            journal = state / "journal.sqlite3"
            connection = sqlite3.connect(journal)
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
            try:
                backup = create_backup(state, artifacts, root / "backup")
            finally:
                connection.close()

            manifest = verify_backup(backup)
            self.assertEqual(manifest["schema_version"], BACKUP_SCHEMA_VERSION)
            self.assertTrue(manifest["reconciliation_required_after_restore"])
            self.assertFalse((backup / "state" / "journal.sqlite3-wal").exists())
            self.assertFalse((backup / "state" / "journal.sqlite3-shm").exists())

            restored = restore_backup(backup, root / "restored")
            self.assertTrue(restore_requires_reconciliation(restored))
            self.assertTrue((restored / "state" / "journal.sqlite3").is_file())
            self.assertTrue((restored / "artifacts" / "objects" / "sha256").is_dir())

    def test_journal_only_backup_declares_journal_only_consistency(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            (state / "checkpoint.json").unlink()
            (state / "learning-evidence.jsonl").unlink()
            backup = create_backup(state, artifacts, root / "backup")
            manifest = verify_backup(backup)
            self.assertEqual(
                manifest["runtime_consistency_check"],
                "JOURNAL_ONLY",
            )

    def test_partial_runtime_consistency_evidence_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            (state / "learning-evidence.jsonl").unlink()
            with self.assertRaisesRegex(
                BackupIntegrityError,
                "must be captured together",
            ):
                create_backup(state, artifacts, root / "backup")

    def test_resealed_manifest_cannot_lie_about_runtime_consistency_mode(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            manifest_path = backup / "backup-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["runtime_consistency_check"] = "JOURNAL_ONLY"
            payload = (
                json.dumps(
                    manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            manifest_path.write_bytes(payload)
            (backup / "backup-manifest.sha256").write_text(
                f"sha256:{sha256(payload).hexdigest()}\n",
                encoding="ascii",
            )
            with self.assertRaisesRegex(
                BackupIntegrityError,
                "claim does not match",
            ):
                verify_backup(backup)

    def test_logically_inconsistent_runtime_snapshot_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            evidence_path = state / "learning-evidence.jsonl"
            row = json.loads(evidence_path.read_text(encoding="utf-8"))
            row["risk_outcome"] = "tampered"
            evidence_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(BackupIntegrityError, "consistent snapshot"):
                create_backup(state, artifacts, root / "backup")
            self.assertFalse((root / "backup").exists())

    def test_missing_artifact_blob_invalidates_backup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            object_files = [
                path
                for path in (backup / "artifacts" / "objects" / "sha256").rglob("*")
                if path.is_file()
            ]
            self.assertEqual(len(object_files), 1)
            object_files[0].unlink()
            with self.assertRaisesRegex(BackupIntegrityError, "missing"):
                verify_backup(backup)

    def test_nested_manifest_named_file_cannot_hide_outside_inventory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")

            hidden = backup / "state" / "backup-manifest.json"
            hidden.write_text('{"not":"inventory metadata"}\n', encoding="utf-8")

            with self.assertRaisesRegex(
                BackupIntegrityError,
                "untracked or missing payload files",
            ):
                verify_backup(backup)

    def test_nested_manifest_digest_named_file_cannot_hide_outside_inventory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")

            hidden = backup / "artifacts" / "backup-manifest.sha256"
            hidden.write_text("sha256:" + "0" * 64 + "\n", encoding="ascii")

            with self.assertRaisesRegex(
                BackupIntegrityError,
                "untracked or missing payload files",
            ):
                verify_backup(backup)

    def test_incompatible_backup_schema_is_rejected_even_with_matching_manifest_digest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            manifest_path = backup / "backup-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = 99
            payload = (
                json.dumps(
                    manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            manifest_path.write_bytes(payload)
            (backup / "backup-manifest.sha256").write_text(
                f"sha256:{sha256(payload).hexdigest()}\n",
                encoding="ascii",
            )
            with self.assertRaises(BackupCompatibilityError):
                verify_backup(backup)

    def test_incompatible_source_journal_schema_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            with closing(sqlite3.connect(state / "journal.sqlite3")) as connection:
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (99, "2026-09-24T00:00:00Z"),
                )
                connection.commit()
            with self.assertRaises(BackupCompatibilityError):
                create_backup(state, artifacts, root / "backup")

    def test_interrupted_restore_never_publishes_partial_destination(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            destination = root / "restored"
            original_copy = shutil.copy2
            calls = 0

            def interrupted(source, target, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated interruption")
                return original_copy(source, target, *args, **kwargs)

            with patch("mvp.autotrade_mvp.backup.shutil.copy2", side_effect=interrupted):
                with self.assertRaises(OSError):
                    restore_backup(backup, destination)
            self.assertFalse(destination.exists())
            self.assertFalse(any(root.glob(".autotrade-restore-*")))

    def test_source_artifact_manifest_with_missing_blob_fails_before_backup_commit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            for path in (artifacts / "objects" / "sha256").rglob("*"):
                if path.is_file():
                    path.unlink()
            with self.assertRaisesRegex(BackupIntegrityError, "missing"):
                create_backup(state, artifacts, root / "backup")
            self.assertFalse((root / "backup").exists())

    def test_backup_destination_cannot_be_inside_state_or_artifacts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            artifacts = root / "artifacts"
            state.mkdir()
            artifacts.mkdir()
            JournalStore(state / "journal.sqlite3")
            with self.assertRaisesRegex(BackupError, "outside source"):
                create_backup(state, artifacts, state / "order-intents" / "backup")
            with self.assertRaisesRegex(BackupError, "outside source"):
                create_backup(state, artifacts, artifacts / "nested-backup")

    def test_restore_destination_cannot_be_inside_backup_bundle(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            artifacts = root / "artifacts"
            state.mkdir()
            artifacts.mkdir()
            JournalStore(state / "journal.sqlite3")
            backup = create_backup(state, artifacts, root / "backup")
            with self.assertRaisesRegex(BackupError, "outside the backup"):
                restore_backup(backup, backup / "restored")

    def test_missing_or_corrupt_restore_marker_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(restore_requires_reconciliation(root))
            marker = root / "RESTORE_RECONCILIATION_REQUIRED.json"
            marker.write_text("not-json", encoding="utf-8")
            self.assertTrue(restore_requires_reconciliation(root))


    def test_resealed_artifact_manifest_with_wrong_declared_size_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            manifest_path = next(
                (backup / "artifacts" / "manifests" / "sha256").rglob("*.json")
            )
            artifact_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            artifact_manifest["size_bytes"] += 1
            manifest_path.write_text(json.dumps(artifact_manifest), encoding="utf-8")
            _reseal_backup_manifest(backup)

            with self.assertRaisesRegex(
                BackupIntegrityError, "does not match its object"
            ):
                verify_backup(backup)

    def test_resealed_artifact_manifest_with_noncanonical_path_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            old_path = next(
                (backup / "artifacts" / "manifests" / "sha256").rglob("*.json")
            )
            wrong_dir = old_path.parent.parent / "ff"
            wrong_dir.mkdir(parents=True, exist_ok=True)
            new_path = wrong_dir / old_path.name
            old_path.replace(new_path)

            outer = json.loads((backup / "backup-manifest.json").read_text(encoding="utf-8"))
            old_relative = old_path.relative_to(backup).as_posix()
            new_relative = new_path.relative_to(backup).as_posix()
            for item in outer["files"]:
                if item["path"] == old_relative:
                    item["path"] = new_relative
                    break
            payload = (
                json.dumps(
                    outer,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            (backup / "backup-manifest.json").write_bytes(payload)
            (backup / "backup-manifest.sha256").write_text(
                f"sha256:{sha256(payload).hexdigest()}\n",
                encoding="ascii",
            )

            with self.assertRaisesRegex(
                BackupIntegrityError, "outside the canonical inventory"
            ):
                verify_backup(backup)

    def test_resealed_inventory_kind_spoof_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            outer = json.loads((backup / "backup-manifest.json").read_text(encoding="utf-8"))
            object_item = next(
                item for item in outer["files"] if item["kind"] == "artifact-object"
            )
            object_item["kind"] = "runtime-state"
            payload = (
                json.dumps(
                    outer,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            (backup / "backup-manifest.json").write_bytes(payload)
            (backup / "backup-manifest.sha256").write_text(
                f"sha256:{sha256(payload).hexdigest()}\n",
                encoding="ascii",
            )

            with self.assertRaisesRegex(
                BackupIntegrityError, "kind does not match canonical path"
            ):
                verify_backup(backup)


    def test_resealed_backup_rejects_gapped_journal_migration_history(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            journal = backup / "state" / "journal.sqlite3"

            connection = sqlite3.connect(journal)
            try:
                current = int(
                    connection.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0]
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (current + 2, "2026-09-25T00:00:00Z"),
                )
                connection.commit()
            finally:
                connection.close()

            _reseal_backup_manifest(backup)
            with self.assertRaisesRegex(
                BackupIntegrityError, "migration history is not contiguous"
            ):
                verify_backup(backup)


if __name__ == "__main__":
    unittest.main()
