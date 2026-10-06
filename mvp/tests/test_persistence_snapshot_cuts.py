"""Snapshot-cut regressions for WP-05 recovery reads."""

from __future__ import annotations

import sqlite3
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore, canonical_json, payload_digest


def event(event_id: str = "evt-cut-1", version: int = 1) -> dict[str, object]:
    payload = {"kind": "fill", "quantity": str(version)}
    return {
        "event_id": event_id,
        "event_type": "ExecutionFillObserved",
        "aggregate_type": "account",
        "aggregate_id": "paper-1",
        "aggregate_version": str(version),
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": f"2026-10-06T13:00:{version:02d}+00:00",
    }


class PersistenceSnapshotCutTests(unittest.TestCase):
    def test_journal_tail_rejects_cut_ahead_of_same_snapshot(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event())

            with self.assertRaisesRegex(ValueError, "ahead of the journal"):
                store.load_events_after_journal_sequence(2)

    def test_projection_checkpoint_cannot_become_valid_from_later_journal_write(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event())

            state = {"net_quantity": "2"}
            state_json = canonical_json(state)
            state_hash = payload_digest(
                {
                    "projection_name": "position",
                    "aggregate_type": "account",
                    "aggregate_id": "paper-1",
                    "aggregate_version": 2,
                    "state": state,
                }
            )
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    INSERT INTO projection_checkpoints(
                        projection_name, aggregate_type, aggregate_id,
                        aggregate_version, state_json, state_hash, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "position",
                        "account",
                        "paper-1",
                        2,
                        state_json,
                        state_hash,
                        "2026-10-06T13:01:00+00:00",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            original = store._aggregate_version_value
            raced = False

            def race(connection, aggregate_type, aggregate_id):
                nonlocal raced
                if not raced:
                    raced = True
                    JournalStore(path).append_event(event("evt-cut-2", 2))
                return original(connection, aggregate_type, aggregate_id)

            store._aggregate_version_value = race
            with self.assertRaisesRegex(ValueError, "ahead of the journal"):
                store.load_projection_checkpoint(
                    projection_name="position",
                    aggregate_type="account",
                    aggregate_id="paper-1",
                )
            self.assertTrue(raced)
            self.assertEqual(JournalStore(path).current_journal_sequence(), 2)

    def test_global_checkpoint_cannot_become_valid_from_later_journal_write(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event())

            state = {"portfolio": "two-event-cut"}
            state_json = canonical_json(state)
            state_hash = payload_digest(
                {
                    "projection_name": "portfolio",
                    "journal_sequence": 2,
                    "state": state,
                }
            )
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    INSERT INTO global_projection_checkpoints(
                        projection_name, journal_sequence, state_json, state_hash, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "portfolio",
                        2,
                        state_json,
                        state_hash,
                        "2026-10-06T13:02:00+00:00",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            original = store._journal_sequence_value
            raced = False

            def race(connection):
                nonlocal raced
                if not raced:
                    raced = True
                    JournalStore(path).append_event(event("evt-cut-global-2", 2))
                return original(connection)

            store._journal_sequence_value = race
            with self.assertRaisesRegex(ValueError, "ahead of the journal"):
                store.load_global_projection_checkpoint(projection_name="portfolio")
            self.assertTrue(raced)
            self.assertEqual(JournalStore(path).current_journal_sequence(), 2)


if __name__ == "__main__":
    unittest.main()
