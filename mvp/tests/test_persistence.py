from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


def event(event_id="evt-1", version=1, payload=None):
    payload = {"kind": "fill", "quantity": "1"} if payload is None else payload
    return {
        "event_id": event_id,
        "event_type": "ExecutionFillObserved",
        "aggregate_type": "account",
        "aggregate_id": "paper-1",
        "aggregate_version": version,
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": "2026-09-24T16:00:00+00:00",
    }


class JournalStoreTests(unittest.TestCase):
    def test_event_and_outbox_commit_atomically_and_replay_idempotently(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            first = store.append_event(event(), outbox_topic="events")
            again = store.append_event(event(), outbox_topic="events")
            self.assertTrue(first.inserted)
            self.assertFalse(again.inserted)
            self.assertEqual(len(store.load_events("account", "paper-1")), 1)
            pending = store.pending_outbox()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["event_id"], "evt-1")
            self.assertTrue(store.mark_outbox_delivered(pending[0]["outbox_id"]))
            self.assertFalse(store.mark_outbox_delivered(pending[0]["outbox_id"]))
            self.assertEqual(store.pending_outbox(), [])

    def test_payload_tamper_and_version_gap_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            bad = event()
            bad["payload_hash"] = "sha256:" + "0" * 64
            with self.assertRaisesRegex(ValueError, "payload_hash"):
                store.append_event(bad)
            store.append_event(event())
            with self.assertRaisesRegex(ValueError, "must be 2"):
                store.append_event(event("evt-3", 3))

    def test_event_id_conflict_is_rejected(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            store.append_event(event())
            conflicting = event(payload={"kind": "fill", "quantity": "2"})
            with self.assertRaisesRegex(ValueError, "conflicts"):
                store.append_event(conflicting)

    def test_command_dedupe_returns_prior_result_and_rejects_changed_request(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            result = {"status": "ACCEPTED", "state_version": 7}
            saved, inserted = store.record_command(
                command_id="cmd-1",
                idempotency_key="key-1",
                request={"action": "AUTHORITY.REVOKE", "payload": {"policy_id": "p1"}},
                result=result,
                state_version=7,
            )
            self.assertTrue(inserted)
            self.assertEqual(saved, result)
            replayed, inserted = store.record_command(
                command_id="cmd-other",
                idempotency_key="key-1",
                request={"action": "AUTHORITY.REVOKE", "payload": {"policy_id": "p1"}},
                result={"status": "SHOULD_NOT_REPLACE"},
                state_version=99,
            )
            self.assertFalse(inserted)
            self.assertEqual(replayed, result)
            with self.assertRaisesRegex(ValueError, "different request"):
                store.record_command(
                    command_id="cmd-2",
                    idempotency_key="key-1",
                    request={"action": "AUTHORITY.REVOKE", "payload": {"policy_id": "p2"}},
                    result=result,
                    state_version=8,
                )

    def test_non_finite_payload_is_rejected(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            with self.assertRaises(ValueError):
                store.append_event(event(payload={"price": float("nan")}))

    def test_reopen_preserves_events_outbox_and_dedupe(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            first = JournalStore(path)
            first.append_event(event(), outbox_topic="events")
            first.record_command(
                command_id="cmd-1",
                idempotency_key="key-1",
                request={"action": "A"},
                result={"status": "ACCEPTED"},
                state_version=1,
            )
            reopened = JournalStore(path)
            self.assertEqual(len(reopened.load_events("account", "paper-1")), 1)
            self.assertEqual(len(reopened.pending_outbox()), 1)
            replayed, inserted = reopened.record_command(
                command_id="cmd-x",
                idempotency_key="key-1",
                request={"action": "A"},
                result={"status": "OTHER"},
                state_version=2,
            )
            self.assertFalse(inserted)
            self.assertEqual(replayed, {"status": "ACCEPTED"})


if __name__ == "__main__":
    unittest.main()
