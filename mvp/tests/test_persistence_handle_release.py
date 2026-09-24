from pathlib import Path
import os
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


class PersistenceHandleReleaseTests(unittest.TestCase):
    def test_database_file_can_be_removed_immediately_after_operations(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            payload = {"kind": "fill", "quantity": "1"}
            store.append_event(
                {
                    "event_id": "evt-release-1",
                    "event_type": "ExecutionFillObserved",
                    "aggregate_type": "account",
                    "aggregate_id": "paper-release",
                    "aggregate_version": 1,
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": "2026-09-24T16:00:00+00:00",
                },
                outbox_topic="events",
            )
            store.load_events("account", "paper-release")
            pending = store.pending_outbox()
            store.mark_outbox_delivered(pending[0]["outbox_id"])
            store.record_command(
                command_id="cmd-release-1",
                idempotency_key="key-release-1",
                request={"action": "A"},
                result={"status": "OK"},
                state_version=1,
            )

            os.remove(path)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
