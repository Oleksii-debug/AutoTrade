"""Exact outbox authority survives batching, concurrent delivery and corruption."""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


def event(identity):
    payload = {"identity": identity}
    return {
        "event_id": identity, "event_type": "Observed",
        "aggregate_type": "batch-test", "aggregate_id": identity,
        "aggregate_version": "1", "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": "2026-10-15T00:00:00Z",
    }


class OutboxBatchSnapshotTests(unittest.TestCase):
    def test_batch_retains_one_snapshot_across_concurrent_delivery(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            for identity in ("first", "second"):
                store.append_event(event(identity), outbox_topic="test.events")
            decode = JournalStore._decode_event_row
            advanced = False

            def decode_and_advance(row):
                nonlocal advanced
                result = decode(row)
                if not advanced:
                    advanced = True
                    # A separate WAL writer commits between the two reads.
                    with sqlite3.connect(store.path) as writer:
                        writer.execute(
                            "UPDATE outbox SET delivered_at = ? WHERE event_id = ?",
                            ("2026-10-15T01:00:00Z", "second"),
                        )
                return result

            requests = (("first", "test.events"), ("second", "test.events"))
            with patch.object(JournalStore, "_decode_event_row", staticmethod(decode_and_advance)):
                states = store.outbox_delivery_states(requests)
            self.assertEqual([state["delivered"] for state in states], [False, False])
            self.assertTrue(store.outbox_delivery_states(requests)[1]["delivered"])

    def test_delivered_corruption_is_not_hidden_by_batch_or_cache(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            store.append_event(event("first"), outbox_topic="test.events")
            requests = (("first", "test.events"),)
            state = store.outbox_delivery_states(requests)[0]
            store.mark_outbox_delivered(state["outbox_id"], expected_envelope_hash=state["envelope_hash"])
            self.assertTrue(store.outbox_delivery_states(requests)[0]["delivered"])
            with sqlite3.connect(store.path) as writer:
                writer.execute("UPDATE outbox SET envelope_hash = 'corrupt'")
            with self.assertRaisesRegex(ValueError, "hash"):
                store.outbox_delivery_states(requests)

    def test_absence_means_no_publication_and_requires_existing_event(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            store.append_event(event("unpublished"))
            store.append_event(event("published"), outbox_topic="test.events")
            self.assertEqual(store.outbox_delivery_states((("unpublished", None),)), (None,))
            for request, message in (
                (("published", None), "forbids"),
                (("missing", None), "missing"),
                (("unpublished", "test.events"), "missing"),
                (("published", "wrong.route"), "topic"),
            ):
                with self.subTest(request=request), self.assertRaisesRegex(ValueError, message):
                    store.outbox_delivery_states((request,))
            self.assertEqual(store.pending_outbox_count(), 1)

    def test_many_publications_open_one_connection_and_preserve_order(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            identities = tuple(f"event-{index}" for index in range(40))
            for identity in identities:
                store.append_event(event(identity), outbox_topic="test.events")
            with patch.object(JournalStore, "_connect", wraps=JournalStore._connect) as connect:
                states = store.outbox_delivery_states(tuple((identity, "test.events") for identity in identities))
            self.assertEqual(connect.call_count, 1)
            self.assertEqual(tuple(state["event_id"] for state in states), identities)
            self.assertFalse(any(state["delivered"] for state in states))

    def test_executable_container_is_rejected_before_iteration(self):
        class HostileTuple(tuple):
            def __iter__(self):
                raise AssertionError("caller code dispatched")
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            for requests in (HostileTuple(), (HostileTuple(("event", None)),)):
                with self.assertRaises(TypeError):
                    store.outbox_delivery_states(requests)
