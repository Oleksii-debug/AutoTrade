import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
import zipfile

from mvp.autotrade_mvp.backup import create_backup, restore_backup
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


def event():
    payload = {"kind": "fill", "quantity": "1"}
    return {
        "event_id": "evt-1",
        "event_type": "ExecutionFillObserved",
        "aggregate_type": "account",
        "aggregate_id": "paper-1",
        "aggregate_version": 1,
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": "2026-09-24T16:00:00+00:00",
    }


class BackupRestoreTests(unittest.TestCase):
    def test_backup_restores_checkpoint_evidence_intents_and_sqlite(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / "checkpoint.json").write_text('{"schema_version":1}\n', encoding="utf-8")
            (state / "learning-evidence.jsonl").write_text('{"evidence_id":"e1"}\n', encoding="utf-8")
            intents = state / "order-intents"
            intents.mkdir()
            (intents / "o1.json").write_text('{"id":"o1"}\n', encoding="utf-8")

            store = JournalStore(state / "journal.sqlite3")
            store.append_event(event(), outbox_topic="events")

            archive = root / "backup.zip"
            manifest = create_backup(state, archive)
            self.assertEqual(
                [item["path"] for item in manifest["files"]],
                [
                    "checkpoint.json",
                    "journal.sqlite3",
                    "learning-evidence.jsonl",
                    "order-intents/o1.json",
                ],
            )

            restored = root / "restored"
            restore_backup(archive, restored)
            self.assertEqual(
                (restored / "checkpoint.json").read_text(encoding="utf-8"),
                '{"schema_version":1}\n',
            )
            reopened = JournalStore(restored / "journal.sqlite3")
            self.assertEqual(len(reopened.load_events("account", "paper-1")), 1)
            self.assertEqual(len(reopened.pending_outbox()), 1)

    def test_tampered_archive_fails_closed_without_target(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / "checkpoint.json").write_text('{"schema_version":1}\n', encoding="utf-8")
            archive = root / "backup.zip"
            create_backup(state, archive)

            tampered = root / "tampered.zip"
            with zipfile.ZipFile(archive, "r") as source, zipfile.ZipFile(tampered, "w") as target:
                for item in source.infolist():
                    content = source.read(item.filename)
                    if item.filename == "checkpoint.json":
                        content += b"x"
                    target.writestr(item, content)

            restored = root / "restored"
            with self.assertRaisesRegex(ValueError, "integrity"):
                restore_backup(tampered, restored)
            self.assertFalse(restored.exists())

    def test_existing_target_is_never_overwritten(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / "checkpoint.json").write_text("{}\n", encoding="utf-8")
            archive = root / "backup.zip"
            create_backup(state, archive)
            target = root / "live"
            target.mkdir()
            (target / "keep.txt").write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must not already exist"):
                restore_backup(archive, target)
            self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_live_wal_database_is_snapshotted_without_sidecars(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            db = state / "journal.sqlite3"
            store = JournalStore(db)
            store.append_event(event(), outbox_topic="events")
            with sqlite3.connect(db) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE IF NOT EXISTS probe(value TEXT)")
                connection.execute("INSERT INTO probe(value) VALUES ('present')")
                connection.commit()

            archive = root / "backup.zip"
            create_backup(state, archive)
            restored = root / "restored"
            restore_backup(archive, restored)

            with sqlite3.connect(restored / "journal.sqlite3") as connection:
                self.assertEqual(connection.execute("SELECT value FROM probe").fetchone()[0], "present")
            self.assertFalse((restored / "journal.sqlite3-wal").exists())
            self.assertFalse((restored / "journal.sqlite3-shm").exists())


if __name__ == "__main__":
    unittest.main()
