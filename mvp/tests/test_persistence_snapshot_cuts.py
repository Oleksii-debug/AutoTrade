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

    def test_journal_tail_page_does_not_include_append_after_snapshot_cut(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event())

            original = store._journal_sequence_value
            raced = False

            def race(connection):
                nonlocal raced
                current = original(connection)
                if not raced:
                    raced = True
                    JournalStore(path).append_event(event("evt-cut-later", 2))
                return current

            store._journal_sequence_value = race
            page = store.load_events_after_journal_sequence(0)

            self.assertTrue(raced)
            self.assertEqual(
                [item["event_id"] for item in page],
                ["evt-cut-1"],
            )
            self.assertEqual(JournalStore(path).current_journal_sequence(), 2)

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

    def test_projection_checkpoint_rejects_non_text_durable_authority(self):
        for column in ("state_json", "state_hash", "updated_at"):
            with self.subTest(column=column), TemporaryDirectory() as directory:
                path = f"{directory}/journal.sqlite3"
                store = JournalStore(path)
                store.append_event(event())
                self.assertTrue(
                    store.save_projection_checkpoint(
                        projection_name="position",
                        aggregate_type="account",
                        aggregate_id="paper-1",
                        aggregate_version=1,
                        state={"net_quantity": "1"},
                    )
                )

                connection = sqlite3.connect(path)
                try:
                    connection.execute(
                        f"UPDATE projection_checkpoints "
                        f"SET {column} = CAST({column} AS BLOB) "
                        "WHERE projection_name = ? AND aggregate_type = ? "
                        "AND aggregate_id = ?",
                        ("position", "account", "paper-1"),
                    )
                    connection.commit()
                finally:
                    connection.close()

                with self.assertRaisesRegex(
                    ValueError,
                    f"projection checkpoint {column} must be canonical non-empty text",
                ):
                    JournalStore(path).load_projection_checkpoint(
                        projection_name="position",
                        aggregate_type="account",
                        aggregate_id="paper-1",
                    )

    def test_global_checkpoint_rejects_non_text_durable_authority(self):
        for column in ("state_json", "state_hash", "updated_at"):
            with self.subTest(column=column), TemporaryDirectory() as directory:
                path = f"{directory}/journal.sqlite3"
                store = JournalStore(path)
                store.append_event(event())
                self.assertTrue(
                    store.save_global_projection_checkpoint(
                        projection_name="portfolio",
                        journal_sequence=1,
                        state={"paper-1": "1"},
                    )
                )

                connection = sqlite3.connect(path)
                try:
                    connection.execute(
                        f"UPDATE global_projection_checkpoints "
                        f"SET {column} = CAST({column} AS BLOB) "
                        "WHERE projection_name = ?",
                        ("portfolio",),
                    )
                    connection.commit()
                finally:
                    connection.close()

                with self.assertRaisesRegex(
                    ValueError,
                    f"global projection checkpoint {column} must be canonical non-empty text",
                ):
                    JournalStore(path).load_global_projection_checkpoint(
                        projection_name="portfolio"
                    )

    def test_projection_rebuild_from_zero_equals_checkpoint_plus_tail_after_restart(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)

            events = [
                event("evt-rebuild-a1", 1),
                event("evt-rebuild-a2", 2),
                event("evt-rebuild-a3", 3),
            ]
            second_account = event("evt-rebuild-b1", 1)
            second_account["aggregate_id"] = "paper-2"
            second_account["payload"] = {"kind": "fill", "quantity": "7"}
            second_account["payload_hash"] = payload_digest(second_account["payload"])

            store.append_event(events[0])
            store.append_event(second_account)
            store.append_event(events[1])

            checkpoint_cut = store.current_journal_sequence()
            self.assertEqual(checkpoint_cut, 3)
            checkpoint_state = {"paper-1": "3", "paper-2": "7"}
            self.assertTrue(
                store.save_global_projection_checkpoint(
                    projection_name="portfolio-equivalence",
                    journal_sequence=checkpoint_cut,
                    state=checkpoint_state,
                )
            )

            store.append_event(events[2])
            reopened = JournalStore(path)

            def reduce_fill(state, item):
                if item["payload"].get("kind") != "fill":
                    return
                aggregate_id = item["aggregate_id"]
                state[aggregate_id] = str(
                    int(state.get(aggregate_id, "0"))
                    + int(item["payload"]["quantity"])
                )

            from_zero = {}
            for item in reopened.load_events_after_journal_sequence(0):
                reduce_fill(from_zero, item)

            checkpoint = reopened.load_global_projection_checkpoint(
                projection_name="portfolio-equivalence"
            )
            from_checkpoint = dict(checkpoint["state"])
            for item in reopened.load_events_after_journal_sequence(
                checkpoint["journal_sequence"]
            ):
                reduce_fill(from_checkpoint, item)

            self.assertEqual(from_zero, {"paper-1": "6", "paper-2": "7"})
            self.assertEqual(from_checkpoint, from_zero)


if __name__ == "__main__":
    unittest.main()
