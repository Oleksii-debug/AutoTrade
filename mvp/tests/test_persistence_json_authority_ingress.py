from __future__ import annotations

from tempfile import TemporaryDirectory
import json
import sqlite3
import unittest

from mvp.autotrade_mvp.persistence import (
    JournalStore,
    canonical_json,
    payload_digest,
    _event_envelope_digest,
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

    def __len__(self):
        raise AssertionError("hostile list length dispatched")


class _HostileTuple(tuple):
    def __iter__(self):
        raise AssertionError("hostile tuple iter dispatched")


class _HostileText(str):
    def __str__(self):
        raise AssertionError("hostile text stringification dispatched")

    def strip(self, *args, **kwargs):
        raise AssertionError("hostile text strip dispatched")

    def upper(self, *args, **kwargs):
        raise AssertionError("hostile text upper dispatched")


class _HostilePathLike:
    def __fspath__(self):
        raise AssertionError("hostile path conversion dispatched")


class _HostileInt(int):
    def __int__(self):
        raise AssertionError("hostile int conversion dispatched")

    def __index__(self):
        raise AssertionError("hostile int index dispatched")

    def __lt__(self, other):
        raise AssertionError("hostile int less-than dispatched")

    def __gt__(self, other):
        raise AssertionError("hostile int greater-than dispatched")


class PersistenceJsonAuthorityIngressTests(unittest.TestCase):
    def test_journal_store_rejects_executable_pathlike_before_filesystem_authority(self):
        hostile = _HostilePathLike()
        with self.assertRaisesRegex(
            TypeError,
            "journal database path must be exact text or exact platform Path",
        ):
            JournalStore(hostile)

    def test_sequence_text_subclass_is_not_durable_sequence_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            candidate = _event("evt-hostile-sequence")
            candidate["aggregate_version"] = _HostileText("1")
            with self.assertRaisesRegex(
                ValueError,
                "aggregate_version must be a positive canonical integer sequence string",
            ):
                store.append_event(candidate)
            self.assertEqual(store.current_journal_sequence(), 0)

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

    def test_load_command_event_batch_rejects_executable_request_before_authority_read(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            request = {"action": "ORDER.SUBMIT"}
            store.commit_command(
                command_id="cmd-load-hostile-request",
                actor="operator",
                environment="PAPER",
                idempotency_key="key-load-hostile-request",
                request=request,
                result={"status": "ACCEPTED"},
                state_version=1,
                events=[(_event("evt-load-hostile-request"), None)],
            )
            before = store.whole_store_state_counts()
            with self.assertRaisesRegex(
                TypeError,
                "persistent JSON values must use exact built-in JSON containers and scalars",
            ):
                store.load_command_event_batch(
                    command_id="cmd-load-hostile-request",
                    actor="operator",
                    environment="PAPER",
                    idempotency_key="key-load-hostile-request",
                    request=_HostileDict(request),
                )
            self.assertEqual(store.whole_store_state_counts(), before)

    def test_record_command_rejects_executable_state_version_before_write(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            before = store.whole_store_state_counts()
            with self.assertRaisesRegex(
                ValueError,
                "state_version must be a non-negative integer",
            ):
                store.record_command(
                    command_id="cmd-hostile-state-version",
                    actor="operator",
                    environment="PAPER",
                    idempotency_key="key-hostile-state-version",
                    request={"action": "TEST"},
                    result={"status": "ACCEPTED"},
                    state_version=_HostileInt(0),
                )
            self.assertEqual(store.whole_store_state_counts(), before)

    def test_commit_command_rejects_executable_state_version_before_write(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            before = store.whole_store_state_counts()
            with self.assertRaisesRegex(
                ValueError,
                "state_version must be a non-negative integer",
            ):
                store.commit_command(
                    command_id="cmd-hostile-commit-state-version",
                    actor="operator",
                    environment="PAPER",
                    idempotency_key="key-hostile-commit-state-version",
                    request={"action": "ORDER.SUBMIT"},
                    result={"status": "ACCEPTED"},
                    state_version=_HostileInt(0),
                    events=[(_event("evt-hostile-commit-state-version"), None)],
                )
            self.assertEqual(store.whole_store_state_counts(), before)

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

    def test_commit_command_rejects_executable_events_container_before_iteration(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            events = _HostileList([(_event("evt-hostile-events"), None)])
            with self.assertRaisesRegex(TypeError, "events must be an exact list"):
                store.commit_command(
                    command_id="cmd-hostile-events",
                    actor="operator",
                    environment="PAPER",
                    idempotency_key="key-hostile-events",
                    request={"action": "ORDER.SUBMIT"},
                    result={"status": "ACCEPTED"},
                    state_version=1,
                    events=events,
                )
            self.assertEqual(store.current_journal_sequence(), 0)

    def test_commit_command_rejects_executable_event_tuple_before_unpack(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            item = _HostileTuple((_event("evt-hostile-tuple"), None))
            with self.assertRaisesRegex(
                TypeError,
                "event batch entries must be exact 2-tuples",
            ):
                store.commit_command(
                    command_id="cmd-hostile-tuple",
                    actor="operator",
                    environment="PAPER",
                    idempotency_key="key-hostile-tuple",
                    request={"action": "ORDER.SUBMIT"},
                    result={"status": "ACCEPTED"},
                    state_version=1,
                    events=[item],
                )
            self.assertEqual(store.current_journal_sequence(), 0)

    def test_record_command_preserves_first_call_result_object(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            result = ("ACCEPTED", {"reason": "test"})
            saved, inserted = store.record_command(
                command_id="cmd-result-compat",
                actor="operator",
                environment="PAPER",
                idempotency_key="key-result-compat",
                request={"action": "TEST"},
                result=result,
                state_version=0,
            )
            self.assertTrue(inserted)
            self.assertIs(saved, result)

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

    def test_cyclic_payload_fails_closed_before_event_write(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            cycle = []
            cycle.append(cycle)
            candidate = _event("evt-cycle")
            candidate["payload"] = cycle
            candidate["payload_hash"] = "sha256:" + "0" * 64
            with self.assertRaisesRegex(ValueError, "circular reference"):
                store.append_event(candidate)
            self.assertEqual(store.current_journal_sequence(), 0)

    def test_projection_rejects_executable_aggregate_version_before_write(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            with self.assertRaisesRegex(
                ValueError,
                "aggregate_version must be a non-negative integer",
            ):
                store.save_projection_checkpoint(
                    projection_name="hostile-version-projection",
                    aggregate_type="account",
                    aggregate_id="paper-json-ingress",
                    aggregate_version=_HostileInt(0),
                    state={"cash": "100"},
                )
            self.assertEqual(store.whole_store_state_counts()["projection_checkpoints"], 0)

    def test_pending_outbox_rejects_executable_limit_before_query(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            with self.assertRaisesRegex(ValueError, "limit must be between 1 and 1000"):
                store.pending_outbox(limit=_HostileInt(1))

    def test_command_text_subclasses_cannot_dispatch_strip_or_upper(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            result, inserted = store.record_command(
                command_id=_HostileText(" cmd-text "),
                actor=_HostileText(" operator "),
                environment=_HostileText(" paper "),
                idempotency_key=_HostileText(" key-text "),
                request={"action": "TEST"},
                result={"status": "ACCEPTED"},
                state_version=0,
            )
            self.assertTrue(inserted)
            self.assertEqual(result, {"status": "ACCEPTED"})

    def test_append_event_rejects_text_that_would_diverge_from_envelope_bytes(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            candidate = _event(" evt-noncanonical ")
            with self.assertRaisesRegex(
                ValueError,
                "event_id must be canonical non-empty text",
            ):
                store.append_event(candidate)
            self.assertEqual(store.current_journal_sequence(), 0)
            self.assertIsNone(store.get_event("evt-noncanonical"))

    def test_append_event_rejects_noncanonical_outbox_route_before_write(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            candidate = _event("evt-outbox-route")
            with self.assertRaisesRegex(
                ValueError,
                "outbox_topic must be canonical non-empty text",
            ):
                store.append_event(candidate, outbox_topic=" fills ")
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

    def test_commit_command_rejects_executable_outbox_topic_before_write(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            with self.assertRaisesRegex(
                ValueError,
                "outbox_topic must be canonical non-empty text",
            ):
                store.commit_command(
                    command_id="cmd-hostile-topic",
                    actor="operator",
                    environment="PAPER",
                    idempotency_key="key-hostile-topic",
                    request={"action": "ORDER.SUBMIT"},
                    result={"status": "ACCEPTED"},
                    state_version=1,
                    events=[(_event("evt-hostile-topic"), _HostileText("fills"))],
                )
            self.assertEqual(store.current_journal_sequence(), 0)

    def test_commit_command_rejects_noncanonical_event_text_before_command_write(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            candidate = _event("evt-noncanonical-batch")
            candidate["event_type"] = " ExecutionFillObserved "
            with self.assertRaisesRegex(
                ValueError,
                "event_type must be canonical non-empty text",
            ):
                store.commit_command(
                    command_id="cmd-noncanonical-event",
                    actor="operator",
                    environment="PAPER",
                    idempotency_key="key-noncanonical-event",
                    request={"action": "ORDER.SUBMIT"},
                    result={"status": "ACCEPTED"},
                    state_version=1,
                    events=[(candidate, None)],
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

    def test_first_event_claim_rejects_noncanonical_core_text_before_claim(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            candidate = _event("evt-first-canonical")
            candidate["aggregate_id"] = " paper-json-ingress "
            with self.assertRaisesRegex(
                ValueError,
                "aggregate_id must be canonical non-empty text",
            ):
                store.claim_first_event(candidate)
            self.assertEqual(store.current_journal_sequence(), 0)

    def test_restart_rejects_rehashed_noncanonical_event_text(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            candidate = _event("evt-restart-canonical")
            store.append_event(candidate)

            connection = sqlite3.connect(path)
            try:
                raw_envelope = connection.execute(
                    "SELECT envelope_json FROM events WHERE event_id = ?",
                    ("evt-restart-canonical",),
                ).fetchone()[0]
                envelope = json.loads(raw_envelope)
                envelope["event_type"] = " ExecutionFillObserved "
                replacement_json = canonical_json(envelope)
                replacement_hash = _event_envelope_digest(replacement_json)
                connection.execute(
                    "UPDATE events SET event_type = ?, envelope_json = ?, envelope_hash = ? "
                    "WHERE event_id = ?",
                    (
                        " ExecutionFillObserved ",
                        replacement_json,
                        replacement_hash,
                        "evt-restart-canonical",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            reopened = JournalStore(path)
            with self.assertRaisesRegex(
                ValueError,
                "event_type must be canonical non-empty text",
            ):
                reopened.get_event("evt-restart-canonical")

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
