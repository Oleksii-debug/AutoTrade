"""Whole-store first-event authority regressions for JournalStore."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


NOW = "2026-09-30T12:00:00Z"


def owner_event(event_id: str, aggregate_id: str) -> dict:
    payload = {"schema_version": "1.0.0", "owner": aggregate_id}
    return {
        "event_id": event_id,
        "event_type": "TestStoreOwned",
        "schema_version": "1.0.0",
        "aggregate_type": "test_store_owner",
        "aggregate_id": aggregate_id,
        "aggregate_version": "1",
        "host_id": "test-host",
        "owner_epoch": "1",
        "environment": "SIMULATION",
        "occurred_at": NOW,
        "observed_at": NOW,
        "committed_at": NOW,
        "correlation_id": event_id + "-correlation",
        "causation_id": None,
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "evidence_refs": [],
    }


class FirstEventClaimTests(unittest.TestCase):
    def test_empty_store_claim_is_exact_global_sequence_one(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            event = owner_event("owner-1", "session-1")

            result = store.claim_first_event(event)

            self.assertTrue(result.inserted)
            self.assertEqual(result.aggregate_version, 1)
            self.assertEqual(store.current_journal_sequence(), 1)
            persisted = store.get_event("owner-1")
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted["aggregate_version"], 1)
            self.assertEqual(persisted["journal_sequence"], 1)
            self.assertEqual(persisted["payload"], event["payload"])

    def test_existing_event_blocks_claim_without_mutating_store(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            foreign = owner_event("foreign-1", "foreign")
            foreign["aggregate_type"] = "foreign"
            store.append_event(foreign)
            before = store.load_events_by_aggregate_type("foreign")

            with self.assertRaisesRegex(ValueError, "durable business state"):
                store.claim_first_event(owner_event("owner-1", "session-1"))

            self.assertEqual(store.load_events_by_aggregate_type("foreign"), before)
            self.assertEqual(
                store.load_events_by_aggregate_type("test_store_owner"), []
            )
            self.assertEqual(store.current_journal_sequence(), 1)

    def test_command_only_state_blocks_claim_even_with_empty_journal(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            store.record_command(
                command_id="command-1",
                actor="test",
                environment="SIMULATION",
                idempotency_key="idem-1",
                request={"operation": "test"},
                result={"ok": True},
                state_version=0,
            )
            self.assertEqual(store.current_journal_sequence(), 0)

            with self.assertRaisesRegex(ValueError, "durable business state"):
                store.claim_first_event(owner_event("owner-1", "session-1"))

            self.assertEqual(store.current_journal_sequence(), 0)
            self.assertEqual(
                store.load_events_by_aggregate_type("test_store_owner"), []
            )

    def test_projection_only_state_blocks_claim_even_with_empty_journal(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            self.assertTrue(store.save_projection_checkpoint(
                projection_name="test-projection",
                aggregate_type="test-source",
                aggregate_id="source-1",
                aggregate_version=0,
                state={"empty_cut": True},
            ))
            self.assertEqual(store.current_journal_sequence(), 0)

            with self.assertRaisesRegex(ValueError, "durable business state"):
                store.claim_first_event(owner_event("owner-1", "session-1"))

            self.assertEqual(store.current_journal_sequence(), 0)
            self.assertEqual(
                store.load_events_by_aggregate_type("test_store_owner"), []
            )

    def test_two_writers_cannot_both_become_first_owner(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            first = JournalStore(path)
            second = JournalStore(path)
            barrier = Barrier(2)

            def attempt(store: JournalStore, event: dict) -> str:
                barrier.wait()
                try:
                    store.claim_first_event(event)
                    return "won"
                except ValueError as error:
                    self.assertIn("durable business state", str(error))
                    return "blocked"

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda args: attempt(*args),
                    (
                        (first, owner_event("owner-a", "session-a")),
                        (second, owner_event("owner-b", "session-b")),
                    ),
                ))

            self.assertEqual(sorted(results), ["blocked", "won"])
            self.assertEqual(first.current_journal_sequence(), 1)
            owners = first.load_events_by_aggregate_type("test_store_owner")
            self.assertEqual(len(owners), 1)
            self.assertEqual(owners[0]["journal_sequence"], 1)
            self.assertEqual(owners[0]["aggregate_version"], 1)


if __name__ == "__main__":
    unittest.main()
