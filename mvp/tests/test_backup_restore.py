import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.backup import (
    BACKUP_SCHEMA_VERSION,
    BackupError,
    BackupIntegrityError,
    create_backup_bundle,
    restore_backup_bundle,
    restore_requires_reconciliation,
    verify_backup_bundle,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.pipeline import run_vertical_slice


class BackupRestoreTests(unittest.TestCase):
    def test_journal_and_runtime_state_survive_clean_restore(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            run_vertical_slice([100, 101, 102, 103], state)
            self.assertEqual(len(JournalStore(state / "journal.sqlite3").load_events("simulation_portfolio", "SIM")), 1)

            bundle = create_backup_bundle(state, root / "bundle")
            manifest = verify_backup_bundle(bundle)
            self.assertEqual(manifest["schema_version"], BACKUP_SCHEMA_VERSION)

            restored = restore_backup_bundle(bundle, root / "restored")
            restored_state = restored / "state"
            self.assertEqual(
                len(JournalStore(restored_state / "journal.sqlite3").load_events("simulation_portfolio", "SIM")),
                1,
            )
            self.assertTrue((restored_state / "checkpoint.json").is_file())
            self.assertTrue((restored_state / "learning-evidence.jsonl").is_file())
            self.assertTrue(restore_requires_reconciliation(restored_state))

    def test_artifact_payload_is_included_and_verified(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / "checkpoint.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
            artifacts = root / "artifacts"
            artifact = artifacts / "objects" / "aa" / ("a" * 64)
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"immutable artifact")

            bundle = create_backup_bundle(state, root / "bundle", artifact_store_dir=artifacts)
            manifest = verify_backup_bundle(bundle)
            self.assertTrue(any(row["scope"] == "artifacts" for row in manifest["entries"]))

            restored = restore_backup_bundle(bundle, root / "restored")
            restored_artifact = restored / "artifacts" / "objects" / "aa" / ("a" * 64)
            self.assertEqual(restored_artifact.read_bytes(), b"immutable artifact")

    def test_tampered_payload_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / "checkpoint.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
            bundle = create_backup_bundle(state, root / "bundle")
            (bundle / "state" / "checkpoint.json").write_text("tampered", encoding="utf-8")
            with self.assertRaises(BackupIntegrityError):
                verify_backup_bundle(bundle)

    def test_incompatible_manifest_schema_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / "checkpoint.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
            bundle = create_backup_bundle(state, root / "bundle")
            manifest_path = bundle / "backup-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = BACKUP_SCHEMA_VERSION + 1
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(BackupIntegrityError):
                verify_backup_bundle(bundle)

    def test_interrupted_restore_never_publishes_partial_destination(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / "checkpoint.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
            (state / "learning-evidence.jsonl").write_text("{}\n", encoding="utf-8")
            bundle = create_backup_bundle(state, root / "bundle")
            destination = root / "restored"

            with patch("mvp.autotrade_mvp.backup._copy_file", side_effect=OSError("simulated copy failure")):
                with self.assertRaises(OSError):
                    restore_backup_bundle(bundle, destination)

            self.assertFalse(destination.exists())
            self.assertEqual(list(root.glob(".restored.*.staging")), [])

    def test_existing_restore_destination_is_never_overwritten(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / "checkpoint.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
            bundle = create_backup_bundle(state, root / "bundle")
            destination = root / "restored"
            destination.mkdir()
            sentinel = destination / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            with self.assertRaises(BackupError):
                restore_backup_bundle(bundle, destination)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_restore_gate_fails_closed_if_missing_or_corrupt(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir()
            self.assertTrue(restore_requires_reconciliation(state))
            (state / "restore-gate.json").write_text("not-json", encoding="utf-8")
            self.assertTrue(restore_requires_reconciliation(state))


if __name__ == "__main__":
    unittest.main()
