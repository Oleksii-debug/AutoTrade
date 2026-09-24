from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.journal import JournalConflict, SqliteJournal


class JournalTests(unittest.TestCase):
    def test_command_replay_is_idempotent_and_survives_reopen(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            request = {"symbol": "SIM", "side": "BUY", "quantity": "1"}
            events = [
                {
                    "event_type": "OrderIntentAccepted",
                    "payload": {"client_order_id": "order-1"},
                },
                {
                    "event_type": "ExecutionFilled",
                    "payload": {"fill_id": "fill-1", "price": "100"},
                },
            ]
            outbox = [
                {
                    "event_index": 0,
                    "destination": "simulated-provider",
                    "payload": {"client_order_id": "order-1"},
                }
            ]

            with SqliteJournal(path) as journal:
                first = journal.append("cmd-1", request, events, outbox=outbox)
                second = journal.append("cmd-1", request, events, outbox=outbox)
                self.assertEqual(first, second)
                self.assertEqual(journal.command_count(), 1)
                self.assertEqual(journal.event_count(), 2)
                self.assertEqual(len(journal.pending_outbox()), 1)

            with SqliteJournal(path) as journal:
                self.assertEqual(journal.command_count(), 1)
                self.assertEqual([e.event_type for e in journal.read_events()],
                                 ["OrderIntentAccepted", "ExecutionFilled"])
                pending = journal.pending_outbox()
                self.assertEqual(len(pending), 1)
                self.assertTrue(journal.mark_outbox_published(pending[0].outbox_id))
                self.assertFalse(journal.mark_outbox_published(pending[0].outbox_id))
                self.assertEqual(journal.pending_outbox(), [])

    def test_changed_replay_is_rejected_without_new_events(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            with SqliteJournal(path) as journal:
                journal.append(
                    "cmd-1",
                    {"quantity": "1"},
                    [{"event_type": "Accepted", "payload": {"quantity": "1"}}],
                )
                with self.assertRaises(JournalConflict):
                    journal.append(
                        "cmd-1",
                        {"quantity": "2"},
                        [{"event_type": "Accepted", "payload": {"quantity": "2"}}],
                    )
                self.assertEqual(journal.command_count(), 1)
                self.assertEqual(journal.event_count(), 1)

    def test_outbox_is_committed_with_its_event(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            with SqliteJournal(path) as journal:
                sequences = journal.append(
                    "cmd-1",
                    {"action": "send"},
                    [{"event_type": "IntentReady", "payload": {"id": "x"}}],
                    outbox=[
                        {
                            "event_index": 0,
                            "destination": "provider",
                            "payload": {"id": "x"},
                        }
                    ],
                )
                pending = journal.pending_outbox()
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0].event_sequence, sequences[0])

    def test_invalid_request_creates_no_partial_state(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            with SqliteJournal(path) as journal:
                with self.assertRaises(ValueError):
                    journal.append(
                        "cmd-1",
                        {"action": "send"},
                        [{"event_type": "IntentReady", "payload": {"id": "x"}}],
                        outbox=[
                            {
                                "event_index": 9,
                                "destination": "provider",
                                "payload": {"id": "x"},
                            }
                        ],
                    )
                self.assertEqual(journal.command_count(), 0)
                self.assertEqual(journal.event_count(), 0)

    def test_unknown_schema_fails_closed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA user_version = 99")
            connection.close()
            with self.assertRaisesRegex(ValueError, "schema version"):
                SqliteJournal(path)


if __name__ == "__main__":
    unittest.main()
