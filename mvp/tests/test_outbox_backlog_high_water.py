from __future__ import annotations

import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from mvp.autotrade_mvp import _persistence_impl as persistence_impl
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


def _event(
    event_id: str,
    *,
    aggregate_id: str,
    aggregate_version: int = 1,
) -> dict[str, object]:
    payload = {"kind": "outbox-high-water", "event_id": event_id}
    return {
        "event_id": event_id,
        "event_type": "OutboxHighWaterObserved",
        "aggregate_type": "outbox-high-water-test",
        "aggregate_id": aggregate_id,
        "aggregate_version": str(aggregate_version),
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": "2026-10-05T18:00:00+00:00",
    }


class OutboxBacklogHighWaterTests(unittest.TestCase):
    def test_high_water_replays_enqueue_and_delivery_transitions(self):
        with tempfile.TemporaryDirectory() as root:
            store = JournalStore(Path(root) / "journal.sqlite3")
            start = store.outbox_backlog_cut()
            self.assertEqual(
                start,
                {"transition_sequence": 0, "pending_count": 0},
            )

            store.append_event(
                _event("evt-high-water-a", aggregate_id="a"),
                outbox_topic="events",
            )
            store.append_event(
                _event("evt-high-water-b", aggregate_id="b"),
                outbox_topic="events",
            )

            pending = store.pending_outbox()
            self.assertEqual(len(pending), 2)
            for item in pending:
                self.assertTrue(
                    store.mark_outbox_delivered(
                        item["outbox_id"],
                        expected_envelope_hash=item["envelope_hash"],
                    )
                )

            evidence = store.outbox_backlog_high_water_since(
                start_transition_sequence=start["transition_sequence"],
                start_pending_count=start["pending_count"],
            )
            self.assertEqual(
                evidence,
                {
                    "start_transition_sequence": 0,
                    "end_transition_sequence": 4,
                    "start_pending_count": 0,
                    "end_pending_count": 0,
                    "high_water": 2,
                },
            )

    def test_tail_cut_avoids_full_transition_scan_and_is_replay_candidate(self):
        with tempfile.TemporaryDirectory() as root:
            store = JournalStore(Path(root) / "journal.sqlite3")
            start = store.outbox_backlog_cut()
            store.append_event(
                _event("evt-tail-cut", aggregate_id="tail-cut"),
                outbox_topic="events",
            )

            with patch.object(
                JournalStore,
                "_outbox_transition_sequence_value",
                side_effect=AssertionError("full transition scan executed"),
            ):
                tail = store.outbox_backlog_tail_cut(
                    start_transition_sequence=start["transition_sequence"],
                    start_pending_count=start["pending_count"],
                )

            self.assertEqual(
                tail,
                {"transition_sequence": 1, "pending_count": 1},
            )
            replay = store.outbox_backlog_high_water_since(
                start_transition_sequence=start["transition_sequence"],
                start_pending_count=start["pending_count"],
            )
            self.assertEqual(replay["end_transition_sequence"], 1)
            self.assertEqual(replay["end_pending_count"], 1)
            self.assertEqual(replay["high_water"], 1)

    def test_tail_cut_candidate_does_not_authenticate_tampered_transition(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "journal.sqlite3"
            store = JournalStore(path)
            start = store.outbox_backlog_cut()
            store.append_event(
                _event("evt-tail-tamper", aggregate_id="tail-tamper"),
                outbox_topic="events",
            )

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    UPDATE outbox_backlog_transitions
                    SET pending_count = 9
                    WHERE transition_sequence = 1
                    """
                )
                connection.commit()
            finally:
                connection.close()

            candidate = store.outbox_backlog_tail_cut(
                start_transition_sequence=start["transition_sequence"],
                start_pending_count=start["pending_count"],
            )
            self.assertEqual(
                candidate,
                {"transition_sequence": 1, "pending_count": 9},
            )
            with self.assertRaisesRegex(
                ValueError,
                "transition pending count conflicts with replayed authority",
            ):
                store.outbox_backlog_high_water_since(
                    start_transition_sequence=start["transition_sequence"],
                    start_pending_count=start["pending_count"],
                )

    def test_idempotent_enqueue_and_delivery_replays_do_not_duplicate_transitions(self):
        with tempfile.TemporaryDirectory() as root:
            store = JournalStore(Path(root) / "journal.sqlite3")
            start = store.outbox_backlog_cut()
            envelope = _event("evt-idempotent", aggregate_id="idempotent")

            first = store.append_event(envelope, outbox_topic="events")
            replay = store.append_event(envelope, outbox_topic="events")
            self.assertTrue(first.inserted)
            self.assertFalse(replay.inserted)

            pending = store.pending_outbox()[0]
            self.assertTrue(
                store.mark_outbox_delivered(
                    pending["outbox_id"],
                    expected_envelope_hash=pending["envelope_hash"],
                )
            )
            self.assertFalse(
                store.mark_outbox_delivered(
                    pending["outbox_id"],
                    expected_envelope_hash=pending["envelope_hash"],
                )
            )

            evidence = store.outbox_backlog_high_water_since(
                start_transition_sequence=start["transition_sequence"],
                start_pending_count=start["pending_count"],
            )
            self.assertEqual(evidence["end_transition_sequence"], 2)
            self.assertEqual(evidence["high_water"], 1)
            self.assertEqual(evidence["end_pending_count"], 0)

    def test_commit_command_event_batch_records_enqueue_transition(self):
        with tempfile.TemporaryDirectory() as root:
            store = JournalStore(Path(root) / "journal.sqlite3")
            start = store.outbox_backlog_cut()

            saved, inserted, appended = store.commit_command(
                command_id="cmd-high-water",
                actor="qualification",
                environment="PAPER",
                idempotency_key="key-high-water",
                request={"action": "QUALIFICATION.TEST"},
                result={"status": "RECORDED"},
                state_version=1,
                events=[
                    (
                        _event(
                            "evt-command-high-water",
                            aggregate_id="command",
                        ),
                        "events",
                    )
                ],
            )

            self.assertTrue(inserted)
            self.assertEqual(saved, {"status": "RECORDED"})
            self.assertEqual(len(appended), 1)
            evidence = store.outbox_backlog_high_water_since(
                start_transition_sequence=start["transition_sequence"],
                start_pending_count=start["pending_count"],
            )
            self.assertEqual(evidence["end_transition_sequence"], 1)
            self.assertEqual(evidence["end_pending_count"], 1)
            self.assertEqual(evidence["high_water"], 1)

    def test_v9_migration_uses_exact_pending_baseline_without_inventing_history(self):
        class LegacyV9Store(persistence_impl.JournalStore):
            SCHEMA_VERSION = 9

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "journal.sqlite3"
            legacy = LegacyV9Store(path)
            legacy.append_event(
                _event("evt-legacy-pending", aggregate_id="legacy"),
                outbox_topic="events",
            )
            self.assertEqual(legacy.current_schema_version(), 9)
            self.assertEqual(legacy.pending_outbox_count(), 1)

            store = JournalStore(path)
            self.assertEqual(store.current_schema_version(), 10)
            start = store.outbox_backlog_cut()
            self.assertEqual(
                start,
                {"transition_sequence": 0, "pending_count": 1},
            )
            self.assertEqual(
                store.outbox_backlog_tail_cut(
                    start_transition_sequence=start["transition_sequence"],
                    start_pending_count=start["pending_count"],
                ),
                start,
            )

            pending = store.pending_outbox()[0]
            self.assertTrue(
                store.mark_outbox_delivered(
                    pending["outbox_id"],
                    expected_envelope_hash=pending["envelope_hash"],
                )
            )

            evidence = store.outbox_backlog_high_water_since(
                start_transition_sequence=start["transition_sequence"],
                start_pending_count=start["pending_count"],
            )
            self.assertEqual(
                evidence,
                {
                    "start_transition_sequence": 0,
                    "end_transition_sequence": 1,
                    "start_pending_count": 1,
                    "end_pending_count": 0,
                    "high_water": 1,
                },
            )

    def test_out_of_band_delivery_without_transition_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "journal.sqlite3"
            store = JournalStore(path)
            store.append_event(
                _event("evt-out-of-band", aggregate_id="out-of-band"),
                outbox_topic="events",
            )
            start = store.outbox_backlog_cut()
            self.assertEqual(start["pending_count"], 1)

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE outbox SET delivered_at = ? WHERE event_id = ?",
                    (
                        "2026-10-05T18:01:00+00:00",
                        "evt-out-of-band",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "outbox backlog durable state changed without a canonical transition",
            ):
                store.outbox_backlog_high_water_since(
                    start_transition_sequence=start["transition_sequence"],
                    start_pending_count=start["pending_count"],
                )

    def test_tampered_transition_pending_count_fails_replay(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "journal.sqlite3"
            store = JournalStore(path)
            start = store.outbox_backlog_cut()
            store.append_event(
                _event("evt-tampered-transition", aggregate_id="tampered"),
                outbox_topic="events",
            )

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    UPDATE outbox_backlog_transitions
                    SET pending_count = 9
                    WHERE transition_sequence = 1
                    """
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "transition pending count conflicts with replayed authority",
            ):
                store.outbox_backlog_high_water_since(
                    start_transition_sequence=start["transition_sequence"],
                    start_pending_count=start["pending_count"],
                )


if __name__ == "__main__":
    unittest.main()
