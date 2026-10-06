from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import (
    JournalStore,
    canonical_json,
    payload_digest,
)


def _event(event_id: str = "evt-json-ingress") -> dict[str, object]:
    payload: dict[str, object] = {"kind": "fill", "quantity": "1"}
    return {
        "event_id": event_id,
        "event_type": "ExecutionFillObserved",
        "aggregate_type": "account",
        "aggregate_id": "paper-json-ingress",
        "aggregate_version": "1",
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": "2026-10-06T01:00:00+00:00",
    }


class _HostileDict(dict):
    def get(self, *args, **kwargs):
        raise AssertionError("hostile dict get dispatched")

    def items(self):
        raise AssertionError("hostile dict items dispatched")

    def __iter__(self):
        raise AssertionError("hostile dict iter dispatched")


class _HostileList(list):
    def __iter__(self):
        raise AssertionError("hostile list iter dispatched")


class _HostileText(str):
    def __str__(self):
        raise AssertionError("hostile text stringification dispatched")


class PersistenceJsonAuthorityIngressTests(unittest.TestCase):
    def test_outer_event_subclass_is_rejected_before_virtual_get(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            hostile = _HostileDict(_event())
            with self.assertRaisesRegex(TypeError, "event envelope must be an exact dict"):
                store.append_event(hostile)
            self.assertEqual(store.current_journal_sequence(), 0)

    def test_nested_executable_json_container_is_rejected_before_callback(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            candidate = _event()
            candidate["payload"] = _HostileDict({"kind": "fill", "quantity": "1"})
            candidate["payload_hash"] = "sha256:" + "0" * 64
            with self.assertRaisesRegex(
                TypeError,
                "persistent JSON values must use exact built-in JSON containers and scalars",
            ):
                store.append_event(candidate)
            self.assertEqual(store.current_journal_sequence(), 0)

    def test_commit_command_rejects_executable_request_before_any_durable_mutation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            with self.assertRaisesRegex(
                TypeError,
                "persistent JSON values must use exact built-in JSON containers and scalars",
            ):
                store.commit_command(
                    command_id="cmd-json-ingress",
                    actor="operator",
                    environment="PAPER",
                    idempotency_key="key-json-ingress",
                    request=_HostileDict({"action": "ORDER.SUBMIT"}),
                    result={"status": "ACCEPTED"},
                    state_version=1,
                    events=[(_event(), None)],
                )
            self.assertEqual(
                store.whole_store_state_counts(),
                {
                    "events": 0,
                    "outbox": 0,
                    "command_dedupe": 0,
                    "projection_checkpoints": 0,
                    "global_projection_checkpoints": 0,
                },
            )

    def test_projection_checkpoint_rejects_executable_state_before_write(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            with self.assertRaisesRegex(
                TypeError,
                "persistent JSON values must use exact built-in JSON containers and scalars",
            ):
                store.save_projection_checkpoint(
                    projection_name="account-state",
                    aggregate_type="account",
                    aggregate_id="paper-json-ingress",
                    aggregate_version=0,
                    state=_HostileList([{"cash": "100"}]),
                )
            self.assertEqual(store.whole_store_state_counts()["projection_checkpoints"], 0)

    def test_scalar_subclass_is_rejected_at_durable_event_ingress(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            candidate = _event("evt-hostile-scalar")
            candidate["payload"] = {"value": _HostileText("authority")}
            candidate["payload_hash"] = "sha256:" + "0" * 64
            with self.assertRaisesRegex(
                TypeError,
                "persistent JSON values must use exact built-in JSON containers and scalars",
            ):
                store.append_event(candidate)
            self.assertEqual(store.current_journal_sequence(), 0)

    def test_exact_tuple_keeps_legacy_json_array_semantics(self):
        self.assertEqual(
            canonical_json(("a", {"b": 1}, [True, None])),
            '["a",{"b":1},[true,null]]',
        )

    def test_first_event_claim_rejects_nested_executable_graph_without_claim(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            candidate = _event("evt-first-json-ingress")
            candidate["payload"] = _HostileList([{"kind": "bootstrap"}])
            candidate["payload_hash"] = "sha256:" + "0" * 64
            with self.assertRaisesRegex(
                TypeError,
                "persistent JSON values must use exact built-in JSON containers and scalars",
            ):
                store.claim_first_event(candidate)
            self.assertEqual(store.current_journal_sequence(), 0)


if __name__ == "__main__":
    unittest.main()
