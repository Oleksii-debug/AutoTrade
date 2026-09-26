from tempfile import TemporaryDirectory
import json
import sqlite3
import unittest

from mvp.autotrade_mvp.persistence import (
    JournalStore,
    _event_envelope_digest,
    _outbox_envelope_digest,
    payload_digest,
)


def event(event_id="evt-1", version=1, payload=None):
    payload = {"kind": "fill", "quantity": "1"} if payload is None else payload
    return {
        "event_id": event_id,
        "event_type": "ExecutionFillObserved",
        "aggregate_type": "account",
        "aggregate_id": "paper-1",
        "aggregate_version": str(version),
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
            self.assertTrue(
                store.mark_outbox_delivered(
                    pending[0]["outbox_id"],
                    expected_envelope_hash=pending[0]["envelope_hash"],
                )
            )
            self.assertFalse(
                store.mark_outbox_delivered(
                    pending[0]["outbox_id"],
                    expected_envelope_hash=pending[0]["envelope_hash"],
                )
            )
            self.assertEqual(store.pending_outbox(), [])

    def test_explicit_journal_sequence_survives_vacuum_backup_and_reopen(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            backup_path = f"{directory}/backup.sqlite3"
            store = JournalStore(path)

            first = event("evt-seq-a", 1)
            first["aggregate_id"] = "paper-a"
            second = event("evt-seq-b", 1)
            second["aggregate_id"] = "paper-b"
            third = event("evt-seq-c", 2)
            third["aggregate_id"] = "paper-a"
            store.append_event(first)
            store.append_event(second)
            store.append_event(third)

            before = store.load_events_by_aggregate_type("account")
            self.assertEqual(
                [item["journal_sequence"] for item in before],
                [1, 2, 3],
            )
            self.assertEqual(
                [item["event_id"] for item in before],
                ["evt-seq-a", "evt-seq-b", "evt-seq-c"],
            )
            self.assertEqual(store.current_journal_sequence(), 3)

            connection = sqlite3.connect(path)
            try:
                connection.execute("VACUUM")
                destination = sqlite3.connect(backup_path)
                try:
                    connection.backup(destination)
                finally:
                    destination.close()
            finally:
                connection.close()

            for candidate_path in (path, backup_path):
                reopened = JournalStore(candidate_path)
                loaded = reopened.load_events_by_aggregate_type("account")
                self.assertEqual(
                    [item["journal_sequence"] for item in loaded],
                    [1, 2, 3],
                )
                self.assertEqual(
                    [item["event_id"] for item in loaded],
                    ["evt-seq-a", "evt-seq-b", "evt-seq-c"],
                )
                self.assertEqual(reopened.current_journal_sequence(), 3)

    def test_commit_command_rejects_stale_journal_cut_but_exact_replay_survives(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            initial_cut = store.current_journal_sequence()
            first = event("evt-cut-a", 1)
            saved, inserted, _appended = store.commit_command(
                actor="alice",
                environment="PAPER",
                command_id="cmd-cut-a",
                idempotency_key="key-cut-a",
                request={"action": "ORDER.SUBMIT", "intent_id": "cut-a"},
                result={"status": "ACCEPTED"},
                state_version=1,
                events=[(first, None)],
                expected_journal_sequence=initial_cut,
            )
            self.assertTrue(inserted)
            self.assertEqual(saved, {"status": "ACCEPTED"})

            unrelated = event("evt-cut-b", 1)
            unrelated["aggregate_id"] = "paper-b"
            store.append_event(unrelated)

            replayed, inserted, appended = store.commit_command(
                actor="alice",
                environment="PAPER",
                command_id="cmd-cut-replay",
                idempotency_key="key-cut-a",
                request={"action": "ORDER.SUBMIT", "intent_id": "cut-a"},
                result={"status": "MUST_NOT_REPLACE"},
                state_version=999,
                events=[(first, None)],
                expected_journal_sequence=initial_cut,
            )
            self.assertFalse(inserted)
            self.assertEqual(appended, ())
            self.assertEqual(replayed, {"status": "ACCEPTED"})

            stale_cut = store.current_journal_sequence()
            newer = event("evt-cut-c", 1)
            newer["aggregate_id"] = "paper-c"
            store.append_event(newer)
            candidate = event("evt-cut-d", 2)
            with self.assertRaisesRegex(ValueError, "journal sequence changed"):
                store.commit_command(
                    actor="alice",
                    environment="PAPER",
                    command_id="cmd-cut-stale",
                    idempotency_key="key-cut-stale",
                    request={"action": "ORDER.SUBMIT", "intent_id": "cut-stale"},
                    result={"status": "ACCEPTED"},
                    state_version=2,
                    events=[(candidate, None)],
                    expected_journal_sequence=stale_cut,
                )
            self.assertEqual(
                [item["event_id"] for item in store.load_events("account", "paper-1")],
                ["evt-cut-a"],
            )


    def test_load_events_preserves_verified_full_envelope_metadata(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            item = event()
            item.update(
                {
                    "environment": "PAPER",
                    "owner_epoch": "7",
                    "host_id": "host-a",
                    "correlation_id": "corr-a",
                    "causation_id": None,
                    "evidence_refs": ["artifact:proof"],
                }
            )
            store.append_event(item)
            loaded = store.load_events("account", "paper-1")[0]
            self.assertEqual(loaded["environment"], "PAPER")
            self.assertEqual(loaded["owner_epoch"], "7")
            self.assertEqual(loaded["host_id"], "host-a")
            self.assertEqual(loaded["correlation_id"], "corr-a")
            self.assertEqual(loaded["evidence_refs"], ["artifact:proof"])
            self.assertEqual(loaded["aggregate_version"], 1)

    def test_idempotent_event_replay_rejects_corrupted_outbox_hash(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event(), outbox_topic="events")

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE outbox SET envelope_hash = ? WHERE event_id = ?",
                    ("sha256:" + "0" * 64, "evt-1"),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "conflicts"):
                store.append_event(event(), outbox_topic="events")

    def test_delivery_ack_is_bound_to_verified_outbox_envelope_across_restart(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event(), outbox_topic="events")
            pending = store.pending_outbox()[0]

            connection = sqlite3.connect(path)
            try:
                payload = json.loads(
                    connection.execute(
                        "SELECT payload_json FROM outbox WHERE outbox_id = ?",
                        (pending["outbox_id"],),
                    ).fetchone()[0]
                )
                payload["aggregate_id"] = "tampered-account"
                payload_json = json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                replacement_hash = _outbox_envelope_digest(
                    "events",
                    payload_json,
                )
                connection.execute(
                    "UPDATE outbox SET payload_json = ?, envelope_hash = ? "
                    "WHERE outbox_id = ?",
                    (
                        payload_json,
                        replacement_hash,
                        pending["outbox_id"],
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            reopened = JournalStore(path)
            with self.assertRaisesRegex(ValueError, "acknowledgement is stale"):
                reopened.mark_outbox_delivered(
                    pending["outbox_id"],
                    expected_envelope_hash=pending["envelope_hash"],
                )

            connection = sqlite3.connect(path)
            try:
                delivered_at = connection.execute(
                    "SELECT delivered_at FROM outbox WHERE outbox_id = ?",
                    (pending["outbox_id"],),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertIsNone(delivered_at)


    def test_topic_only_tamper_fails_pending_read_and_stale_delivery_ack(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event(), outbox_topic="events")
            pending = store.pending_outbox()[0]

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE outbox SET topic = ? WHERE outbox_id = ?",
                    ("rerouted.events", pending["outbox_id"]),
                )
                connection.commit()
            finally:
                connection.close()

            reopened = JournalStore(path)
            with self.assertRaisesRegex(ValueError, "envelope hash"):
                reopened.pending_outbox()
            with self.assertRaisesRegex(ValueError, "envelope hash"):
                reopened.mark_outbox_delivered(
                    pending["outbox_id"],
                    expected_envelope_hash=pending["envelope_hash"],
                )

    def test_current_v5_extra_envelope_field_fails_even_with_recomputed_hash(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event(), outbox_topic="events")
            pending = store.pending_outbox()[0]

            connection = sqlite3.connect(path)
            try:
                raw = connection.execute(
                    "SELECT payload_json FROM outbox WHERE outbox_id = ?",
                    (pending["outbox_id"],),
                ).fetchone()[0]
                payload = json.loads(raw)
                payload["unexpected"] = "forged"
                payload_json = json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                forged_hash = _outbox_envelope_digest("events", payload_json)
                connection.execute(
                    "UPDATE outbox SET payload_json = ?, envelope_hash = ? "
                    "WHERE outbox_id = ?",
                    (payload_json, forged_hash, pending["outbox_id"]),
                )
                connection.commit()
            finally:
                connection.close()

            reopened = JournalStore(path)
            with self.assertRaisesRegex(ValueError, "authoritative journal event"):
                reopened.pending_outbox()
            with self.assertRaisesRegex(ValueError, "authoritative journal event"):
                reopened.mark_outbox_delivered(
                    pending["outbox_id"],
                    expected_envelope_hash=forged_hash,
                )

    def test_delivery_ack_revalidates_authoritative_event(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event(), outbox_topic="events")
            pending = store.pending_outbox()[0]

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE events SET payload_json = ? WHERE event_id = ?",
                    ('{"kind":"fill","quantity":"999"}', "evt-1"),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "payload hash"):
                JournalStore(path).mark_outbox_delivered(
                    pending["outbox_id"],
                    expected_envelope_hash=pending["envelope_hash"],
                )

            connection = sqlite3.connect(path)
            try:
                delivered_at = connection.execute(
                    "SELECT delivered_at FROM outbox WHERE outbox_id = ?",
                    (pending["outbox_id"],),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertIsNone(delivered_at)

    def test_outbox_envelope_metadata_tamper_fails_closed(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event(), outbox_topic="events")

            connection = sqlite3.connect(path)
            try:
                row = connection.execute(
                    "SELECT outbox_id, payload_json FROM outbox"
                ).fetchone()
                payload = json.loads(row[1])
                payload["committed_at"] = "2099-01-01T00:00:00Z"
                connection.execute(
                    "UPDATE outbox SET payload_json = ? WHERE outbox_id = ?",
                    (
                        json.dumps(
                            payload,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ),
                        row[0],
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "outbox envelope hash",
            ):
                store.pending_outbox()

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

    def test_aggregate_version_rejects_noncanonical_sequence_values(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            for invalid in (True, 1, 1.0, "01", "-1", "1.0"):
                item = event()
                item["aggregate_version"] = invalid
                with self.assertRaisesRegex(ValueError, "canonical integer sequence string"):
                    store.append_event(item)

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
                actor="alice",
                environment="PAPER",
                command_id="cmd-1",
                idempotency_key="key-1",
                request={"action": "AUTHORITY.REVOKE", "payload": {"policy_id": "p1"}},
                result=result,
                state_version=7,
            )
            self.assertTrue(inserted)
            self.assertEqual(saved, result)
            replayed, inserted = store.record_command(
                actor="alice",
                environment="PAPER",
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
                    actor="alice",
                    environment="PAPER",
                    command_id="cmd-2",
                    idempotency_key="key-1",
                    request={"action": "AUTHORITY.REVOKE", "payload": {"policy_id": "p2"}},
                    result=result,
                    state_version=8,
                )

    def test_idempotency_key_is_scoped_by_actor_and_environment(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            request = {"action": "A"}
            first, inserted = store.record_command(
                actor="alice",
                environment="PAPER",
                command_id="cmd-a",
                idempotency_key="shared-key",
                request=request,
                result={"scope": "alice-paper"},
                state_version=1,
            )
            self.assertTrue(inserted)
            self.assertEqual(first, {"scope": "alice-paper"})

            other_actor, inserted = store.record_command(
                actor="bob",
                environment="PAPER",
                command_id="cmd-b",
                idempotency_key="shared-key",
                request=request,
                result={"scope": "bob-paper"},
                state_version=1,
            )
            self.assertTrue(inserted)
            self.assertEqual(other_actor, {"scope": "bob-paper"})

            other_environment, inserted = store.record_command(
                actor="alice",
                environment="LIVE",
                command_id="cmd-c",
                idempotency_key="shared-key",
                request=request,
                result={"scope": "alice-live"},
                state_version=1,
            )
            self.assertTrue(inserted)
            self.assertEqual(other_environment, {"scope": "alice-live"})

            replayed, inserted = store.record_command(
                actor="alice",
                environment="PAPER",
                command_id="cmd-retry",
                idempotency_key="shared-key",
                request=request,
                result={"scope": "must-not-replace"},
                state_version=99,
            )
            self.assertFalse(inserted)
            self.assertEqual(replayed, {"scope": "alice-paper"})

    def test_environment_scope_is_canonical_and_rejects_unknown_values(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            result, inserted = store.record_command(
                actor="alice",
                environment=" paper ",
                command_id="cmd-paper",
                idempotency_key="key",
                request={"action": "A"},
                result={"ok": True},
                state_version=1,
            )
            self.assertTrue(inserted)
            self.assertEqual(result, {"ok": True})
            replayed, inserted = store.record_command(
                actor="alice",
                environment="PAPER",
                command_id="cmd-retry",
                idempotency_key="key",
                request={"action": "A"},
                result={"ok": False},
                state_version=2,
            )
            self.assertFalse(inserted)
            self.assertEqual(replayed, {"ok": True})
            with self.assertRaisesRegex(ValueError, "environment must be"):
                store.record_command(
                    actor="alice",
                    environment="UNKNOWN_ENV",
                    command_id="cmd-bad-env",
                    idempotency_key="bad-env",
                    request={"action": "A"},
                    result={"ok": False},
                    state_version=1,
                )

    def test_command_state_version_rejects_boolean(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            with self.assertRaisesRegex(ValueError, "non-negative integer"):
                store.record_command(
                    actor="alice",
                    environment="PAPER",
                    command_id="cmd-bool",
                    idempotency_key="key-bool",
                    request={"action": "A"},
                    result={"status": "ACCEPTED"},
                    state_version=True,
                )

    def test_atomic_command_rejects_noncanonical_event_version(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            item = event()
            item["aggregate_version"] = 1
            with self.assertRaisesRegex(ValueError, "canonical integer sequence string"):
                store.commit_command(
                    actor="alice",
                    environment="PAPER",
                    command_id="cmd-version",
                    idempotency_key="key-version",
                    request={"action": "A"},
                    result={"status": "ACCEPTED"},
                    state_version=1,
                    events=[(item, "events")],
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
                actor="alice",
                environment="PAPER",
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
                actor="alice",
                environment="PAPER",
                command_id="cmd-x",
                idempotency_key="key-1",
                request={"action": "A"},
                result={"status": "OTHER"},
                state_version=2,
            )
            self.assertFalse(inserted)
            self.assertEqual(replayed, {"status": "ACCEPTED"})

    def test_command_events_and_outbox_commit_in_one_transaction(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            first = event()
            second = event("evt-2", 2, {"kind": "fee", "amount": "0.1"})
            result = {"status": "ACCEPTED", "state_version": 2}

            saved, inserted, appended = store.commit_command(
                actor="alice",
                environment="PAPER",
                command_id="cmd-atomic",
                idempotency_key="key-atomic",
                request={"action": "ORDER.SUBMIT", "intent_id": "i1"},
                result=result,
                state_version=2,
                events=[(first, "events"), (second, None)],
            )

            self.assertTrue(inserted)
            self.assertEqual(saved, result)
            self.assertEqual([item.event_id for item in appended], ["evt-1", "evt-2"])
            self.assertEqual(len(store.load_events("account", "paper-1")), 2)

            # The atomic path must persist the same integrity evidence as
            # append_event.  Reopening here exercises the crash/restart
            # boundary instead of relying on in-process state.
            reopened = JournalStore(path)
            pending = reopened.pending_outbox()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["event_id"], "evt-1")
            self.assertEqual(pending[0]["topic"], "events")

            replayed, inserted, appended = reopened.commit_command(
                actor="alice",
                environment="PAPER",
                command_id="cmd-retry",
                idempotency_key="key-atomic",
                request={"action": "ORDER.SUBMIT", "intent_id": "i1"},
                result={"status": "MUST_NOT_REPLACE"},
                state_version=999,
                events=[(first, "events"), (second, None)],
            )
            self.assertFalse(inserted)
            self.assertEqual(replayed, result)
            self.assertEqual(appended, ())
            self.assertEqual(len(reopened.pending_outbox()), 1)
            self.assertEqual(len(store.load_events("account", "paper-1")), 2)

    def test_atomic_command_rolls_back_if_outbox_write_fails_then_retries_after_restart(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    CREATE TRIGGER reject_outbox_insert
                    BEFORE INSERT ON outbox
                    BEGIN
                        SELECT RAISE(ABORT, 'simulated outbox failure');
                    END
                    """
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "simulated outbox failure",
            ):
                store.commit_command(
                    actor="alice",
                    environment="PAPER",
                    command_id="cmd-outbox-failure",
                    idempotency_key="key-outbox-failure",
                    request={"action": "ORDER.SUBMIT", "intent_id": "i-failure"},
                    result={"status": "ACCEPTED"},
                    state_version=1,
                    events=[(event(), "events")],
                )

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM command_dedupe").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0],
                    0,
                )
                connection.execute("DROP TRIGGER reject_outbox_insert")
                connection.commit()
            finally:
                connection.close()

            reopened = JournalStore(path)
            saved, inserted, appended = reopened.commit_command(
                actor="alice",
                environment="PAPER",
                command_id="cmd-outbox-failure",
                idempotency_key="key-outbox-failure",
                request={"action": "ORDER.SUBMIT", "intent_id": "i-failure"},
                result={"status": "ACCEPTED"},
                state_version=1,
                events=[(event(), "events")],
            )
            self.assertTrue(inserted)
            self.assertEqual(saved, {"status": "ACCEPTED"})
            self.assertEqual([item.event_id for item in appended], ["evt-1"])
            self.assertEqual(len(reopened.pending_outbox()), 1)

    def test_atomic_command_rolls_back_on_event_version_gap(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            with self.assertRaisesRegex(ValueError, "aggregate_version must be 1"):
                store.commit_command(
                    actor="alice",
                    environment="PAPER",
                    command_id="cmd-bad",
                    idempotency_key="key-bad",
                    request={"action": "ORDER.SUBMIT"},
                    result={"status": "ACCEPTED"},
                    state_version=1,
                    events=[(event("evt-gap", 2), "events")],
                )

            self.assertEqual(store.load_events("account", "paper-1"), [])
            self.assertEqual(store.pending_outbox(), [])
            saved, inserted = store.record_command(
                actor="alice",
                environment="PAPER",
                command_id="cmd-bad",
                idempotency_key="key-bad",
                request={"action": "ORDER.SUBMIT"},
                result={"status": "RETRY"},
                state_version=1,
            )
            self.assertTrue(inserted)
            self.assertEqual(saved, {"status": "RETRY"})


    def test_partial_preexisting_table_cannot_be_misclassified_as_migrated_schema(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "CREATE TABLE command_dedupe(command_id TEXT PRIMARY KEY)"
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaises((ValueError, sqlite3.OperationalError)):
                JournalStore(path)

            connection = sqlite3.connect(path)
            try:
                migration_table = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'schema_migrations'"
                ).fetchone()
            finally:
                connection.close()
            self.assertIsNone(migration_table)

    def test_partial_schema_with_columns_but_missing_unique_invariant_is_rejected(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    CREATE TABLE events(
                        event_id TEXT PRIMARY KEY,
                        event_type TEXT NOT NULL,
                        aggregate_type TEXT NOT NULL,
                        aggregate_id TEXT NOT NULL,
                        aggregate_version INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        payload_hash TEXT NOT NULL,
                        committed_at TEXT NOT NULL
                    )
                    """
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "missing unique constraint"):
                JournalStore(path)

            connection = sqlite3.connect(path)
            try:
                migration_table = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'schema_migrations'"
                ).fetchone()
            finally:
                connection.close()
            self.assertIsNone(migration_table)

    def test_partial_unique_index_does_not_satisfy_identity_contract(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    CREATE TABLE events(
                        event_id TEXT PRIMARY KEY,
                        event_type TEXT NOT NULL,
                        aggregate_type TEXT NOT NULL,
                        aggregate_id TEXT NOT NULL,
                        aggregate_version INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        payload_hash TEXT NOT NULL,
                        committed_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE UNIQUE INDEX forged_events_identity
                    ON events(aggregate_type, aggregate_id, aggregate_version)
                    WHERE 0
                    """
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "aggregate_type,aggregate_id,aggregate_version",
            ):
                JournalStore(path)

            connection = sqlite3.connect(path)
            try:
                migration_table = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'schema_migrations'"
                ).fetchone()
                index_rows = {
                    row[1]: (bool(row[2]), bool(row[4]))
                    for row in connection.execute("PRAGMA index_list(events)")
                }
            finally:
                connection.close()

            # Initialization is atomic: the adversarial pre-existing index remains
            # visible as partial, while migration metadata/DDL is rolled back.
            self.assertIsNone(migration_table)
            self.assertEqual(index_rows["forged_events_identity"], (True, True))

    def test_partial_schema_with_matching_names_but_weakened_column_contract_is_rejected(self):
        cases = (
            ("payload_hash BLOB NOT NULL", "invalid declared type"),
            ("payload_hash TEXT", "invalid NOT NULL contract"),
        )
        for payload_hash_column, expected_error in cases:
            with self.subTest(payload_hash_column=payload_hash_column):
                with TemporaryDirectory() as directory:
                    path = f"{directory}/journal.sqlite3"
                    connection = sqlite3.connect(path)
                    try:
                        connection.execute(
                            f"""
                            CREATE TABLE events(
                                event_id TEXT PRIMARY KEY,
                                event_type TEXT NOT NULL,
                                aggregate_type TEXT NOT NULL,
                                aggregate_id TEXT NOT NULL,
                                aggregate_version INTEGER NOT NULL,
                                payload_json TEXT NOT NULL,
                                {payload_hash_column},
                                committed_at TEXT NOT NULL,
                                UNIQUE (aggregate_type, aggregate_id, aggregate_version)
                            )
                            """
                        )
                        connection.commit()
                    finally:
                        connection.close()

                    with self.assertRaisesRegex(ValueError, expected_error):
                        JournalStore(path)

                    connection = sqlite3.connect(path)
                    try:
                        migration_table = connection.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type = 'table' AND name = 'schema_migrations'"
                        ).fetchone()
                        event_columns = {
                            row[1]: (str(row[2]).upper(), bool(row[3]))
                            for row in connection.execute("PRAGMA table_info(events)")
                        }
                    finally:
                        connection.close()

                    # Initialization is one transaction. Detection of the
                    # weakened pre-existing contract must not leave migration
                    # metadata or partially upgraded columns behind.
                    self.assertIsNone(migration_table)
                    expected_type = "BLOB" if "BLOB" in payload_hash_column else "TEXT"
                    expected_not_null = payload_hash_column.endswith("NOT NULL")
                    self.assertEqual(
                        event_columns["payload_hash"],
                        (expected_type, expected_not_null),
                    )
                    self.assertNotIn("envelope_json", event_columns)
                    self.assertNotIn("journal_sequence", event_columns)

    def test_partial_schema_with_unrecorded_extra_column_is_rejected(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    CREATE TABLE events(
                        event_id TEXT PRIMARY KEY,
                        event_type TEXT NOT NULL,
                        aggregate_type TEXT NOT NULL,
                        aggregate_id TEXT NOT NULL,
                        aggregate_version INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        payload_hash TEXT NOT NULL,
                        committed_at TEXT NOT NULL,
                        unexpected_shadow TEXT,
                        UNIQUE (aggregate_type, aggregate_id, aggregate_version)
                    )
                    """
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "has unexpected columns"):
                JournalStore(path)

            connection = sqlite3.connect(path)
            try:
                migration_table = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'schema_migrations'"
                ).fetchone()
                event_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(events)")
                }
            finally:
                connection.close()

            self.assertIsNone(migration_table)
            self.assertIn("unexpected_shadow", event_columns)
            self.assertNotIn("envelope_json", event_columns)
            self.assertNotIn("journal_sequence", event_columns)

    def test_v1_database_upgrades_atomically_without_losing_events(self):
        class LegacyJournalStore(JournalStore):
            SCHEMA_VERSION = 1

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            legacy = LegacyJournalStore(path)
            legacy.append_event(event(), outbox_topic="events")
            self.assertEqual(legacy.current_schema_version(), 1)

            upgraded = JournalStore(path)
            self.assertEqual(upgraded.current_schema_version(), 7)
            self.assertEqual(
                upgraded.load_events("account", "paper-1")[0]["event_id"],
                "evt-1",
            )
            self.assertEqual(upgraded.pending_outbox()[0]["event_id"], "evt-1")

    def test_legacy_unscoped_idempotency_key_fails_closed_after_upgrade(self):
        class LegacyJournalStore(JournalStore):
            SCHEMA_VERSION = 1

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            legacy = LegacyJournalStore(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    INSERT INTO command_dedupe(
                        command_id, idempotency_key, request_hash,
                        result_json, state_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-command",
                        "legacy-key",
                        "sha256:" + "0" * 64,
                        '{"status":"UNKNOWN_LEGACY"}',
                        0,
                        "2026-09-24T16:00:00Z",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            upgraded = JournalStore(path)
            self.assertEqual(upgraded.current_schema_version(), 7)
            with self.assertRaisesRegex(ValueError, "legacy unscoped"):
                upgraded.record_command(
                    actor="alice",
                    environment="PAPER",
                    command_id="new-command",
                    idempotency_key="legacy-key",
                    request={"action": "A"},
                    result={"status": "ACCEPTED"},
                    state_version=1,
                )

            saved, inserted = upgraded.record_command(
                actor="alice",
                environment="PAPER",
                command_id="new-command",
                idempotency_key="new-key",
                request={"action": "A"},
                result={"status": "ACCEPTED"},
                state_version=1,
            )
            self.assertTrue(inserted)
            self.assertEqual(saved, {"status": "ACCEPTED"})

    def test_v4_upgrade_hashes_command_results_and_repairs_missing_outbox_hash(self):
        class V4JournalStore(JournalStore):
            SCHEMA_VERSION = 4

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            legacy = V4JournalStore(path)
            legacy.append_event(event(), outbox_topic="events")

            request = {"action": "A"}
            result_json = '{"status":"ACCEPTED"}'
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    INSERT INTO command_dedupe(
                        command_id, actor, environment, idempotency_key,
                        request_hash, result_json, state_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "v4-command",
                        "alice",
                        "PAPER",
                        "v4-key",
                        payload_digest(request),
                        result_json,
                        1,
                        "2026-09-24T16:00:00Z",
                    ),
                )
                connection.execute(
                    "UPDATE outbox SET envelope_hash = NULL WHERE event_id = ?",
                    ("evt-1",),
                )
                connection.commit()
            finally:
                connection.close()

            upgraded = JournalStore(path)
            self.assertEqual(upgraded.current_schema_version(), 7)
            replayed, inserted = upgraded.record_command(
                actor="alice",
                environment="PAPER",
                command_id="ignored-on-replay",
                idempotency_key="v4-key",
                request=request,
                result={"status": "MUST_NOT_REPLACE"},
                state_version=999,
            )
            self.assertFalse(inserted)
            self.assertEqual(replayed, {"status": "ACCEPTED"})
            self.assertEqual(upgraded.pending_outbox()[0]["event_id"], "evt-1")


    def test_v4_non_null_payload_hash_upgrades_to_topic_bound_v5_hash(self):
        class V4JournalStore(JournalStore):
            SCHEMA_VERSION = 4

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            legacy = V4JournalStore(path)
            legacy_event = event()
            legacy_event.update(
                {
                    "schema_version": "1.0.0",
                    "host_id": "legacy-host",
                    "owner_epoch": "1",
                    "environment": "SIMULATION",
                    "occurred_at": "2026-09-24T16:00:00+00:00",
                    "observed_at": "2026-09-24T16:00:00+00:00",
                    "correlation_id": "corr-legacy-1",
                    "causation_id": None,
                    "evidence_refs": [],
                }
            )
            legacy.append_event(legacy_event, outbox_topic="events")

            connection = sqlite3.connect(path)
            try:
                legacy_row = connection.execute(
                    "SELECT topic, payload_json FROM outbox WHERE event_id = ?",
                    ("evt-1",),
                ).fetchone()
                legacy_payload = legacy_row[1]
                legacy_hash = payload_digest(json.loads(legacy_payload))
                connection.execute(
                    "UPDATE outbox SET envelope_hash = ? WHERE event_id = ?",
                    (legacy_hash, "evt-1"),
                )
                connection.commit()
            finally:
                connection.close()

            upgraded = JournalStore(path)
            pending = upgraded.pending_outbox()[0]
            expected_v5 = _outbox_envelope_digest("events", legacy_payload)
            self.assertEqual(pending["envelope_hash"], expected_v5)
            self.assertNotEqual(pending["envelope_hash"], legacy_hash)
            self.assertEqual(pending["payload"]["host_id"], "legacy-host")
            self.assertEqual(pending["payload"]["schema_version"], "1.0.0")
            connection = sqlite3.connect(path)
            try:
                event_envelope_json, event_envelope_hash = connection.execute(
                    "SELECT envelope_json, envelope_hash FROM events WHERE event_id = ?",
                    ("evt-1",),
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(
                event_envelope_hash,
                _event_envelope_digest(event_envelope_json),
            )
            self.assertTrue(
                upgraded.mark_outbox_delivered(
                    pending["outbox_id"],
                    expected_envelope_hash=expected_v5,
                )
            )

    def test_v4_upgrade_rejects_malformed_command_result_before_hash_backfill(self):
        class V4JournalStore(JournalStore):
            SCHEMA_VERSION = 4

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            V4JournalStore(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    INSERT INTO command_dedupe(
                        command_id, actor, environment, idempotency_key,
                        request_hash, result_json, state_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "v4-corrupt-command",
                        "alice",
                        "PAPER",
                        "v4-corrupt-key",
                        payload_digest({"action": "A"}),
                        '{"status":',
                        1,
                        "2026-09-24T16:00:00Z",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "legacy command result is not valid JSON"):
                JournalStore(path)

            connection = sqlite3.connect(path)
            try:
                versions = [
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(command_dedupe)")
                }
            finally:
                connection.close()
            self.assertEqual(versions, [1, 2, 3, 4])
            self.assertNotIn("result_hash", columns)

    def test_v4_upgrade_rejects_unverifiable_extra_hashless_outbox_fields(self):
        class V4JournalStore(JournalStore):
            SCHEMA_VERSION = 4

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            legacy = V4JournalStore(path)
            legacy.append_event(event(), outbox_topic="events")

            connection = sqlite3.connect(path)
            try:
                payload = json.loads(
                    connection.execute(
                        "SELECT payload_json FROM outbox WHERE event_id = ?",
                        ("evt-1",),
                    ).fetchone()[0]
                )
                # The legacy journal row does not contain this field. With no
                # pre-existing envelope_hash there is no authority from which
                # migration can prove its historical value.
                payload["host_id"] = "possibly-tampered-host"
                connection.execute(
                    "UPDATE outbox SET payload_json = ?, envelope_hash = NULL "
                    "WHERE event_id = ?",
                    (
                        json.dumps(
                            payload,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ),
                        "evt-1",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "not exactly reconstructable"):
                JournalStore(path)

            connection = sqlite3.connect(path)
            try:
                versions = [
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                envelope_hash = connection.execute(
                    "SELECT envelope_hash FROM outbox WHERE event_id = ?",
                    ("evt-1",),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(versions, [1, 2, 3, 4])
            self.assertIsNone(envelope_hash)

    def test_v4_upgrade_rejects_mismatched_hashless_outbox_before_backfill(self):
        class V4JournalStore(JournalStore):
            SCHEMA_VERSION = 4

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            legacy = V4JournalStore(path)
            legacy.append_event(event(), outbox_topic="events")

            connection = sqlite3.connect(path)
            try:
                payload = json.loads(
                    connection.execute(
                        "SELECT payload_json FROM outbox WHERE event_id = ?",
                        ("evt-1",),
                    ).fetchone()[0]
                )
                payload["aggregate_id"] = "tampered-account"
                connection.execute(
                    "UPDATE outbox SET payload_json = ?, envelope_hash = NULL "
                    "WHERE event_id = ?",
                    (
                        json.dumps(
                            payload,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ),
                        "evt-1",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "legacy outbox payload is not exactly reconstructable from authoritative journal event",
            ):
                JournalStore(path)

            connection = sqlite3.connect(path)
            try:
                versions = [
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                envelope_hash = connection.execute(
                    "SELECT envelope_hash FROM outbox WHERE event_id = ?",
                    ("evt-1",),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(versions, [1, 2, 3, 4])
            self.assertIsNone(envelope_hash)

    def test_command_result_tamper_fails_closed_on_replay(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.record_command(
                actor="alice",
                environment="PAPER",
                command_id="cmd-integrity",
                idempotency_key="key-integrity",
                request={"action": "A"},
                result={"status": "ACCEPTED"},
                state_version=1,
            )

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE command_dedupe SET result_json = ? WHERE command_id = ?",
                    ('{"status":"FABRICATED"}', "cmd-integrity"),
                )
                connection.commit()
            finally:
                connection.close()

            reopened = JournalStore(path)
            with self.assertRaisesRegex(ValueError, "command result hash"):
                reopened.record_command(
                    actor="alice",
                    environment="PAPER",
                    command_id="cmd-replay",
                    idempotency_key="key-integrity",
                    request={"action": "A"},
                    result={"status": "OTHER"},
                    state_version=2,
                )

    def test_failed_migration_rolls_back_schema_and_data_changes(self):
        class BrokenMigrationStore(JournalStore):
            SCHEMA_VERSION = 8

            @classmethod
            def _migration_statements(cls, version):
                if version == 8:
                    return (
                        "CREATE TABLE migration_probe(value TEXT NOT NULL)",
                        "CREATE TABL definitely_invalid(statement TEXT)",
                    )
                return super()._migration_statements(version)

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            healthy = JournalStore(path)
            healthy.append_event(event())
            self.assertEqual(healthy.current_schema_version(), 7)

            with self.assertRaises(sqlite3.OperationalError):
                BrokenMigrationStore(path)

            connection = sqlite3.connect(path)
            try:
                versions = [
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                probe = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'migration_probe'"
                ).fetchone()
                event_count = connection.execute(
                    "SELECT COUNT(*) FROM events"
                ).fetchone()[0]
            finally:
                connection.close()

            self.assertEqual(versions, [1, 2, 3, 4, 5, 6, 7])
            self.assertIsNone(probe)
            self.assertEqual(event_count, 1)

    def test_projection_checkpoint_is_derived_fail_closed_state(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event())
            store.append_event(event("evt-2", 2, {"kind": "fill", "quantity": "2"}))

            state = {"net_quantity": "3"}
            self.assertTrue(
                store.save_projection_checkpoint(
                    projection_name="position",
                    aggregate_type="account",
                    aggregate_id="paper-1",
                    aggregate_version=2,
                    state=state,
                )
            )
            self.assertFalse(
                store.save_projection_checkpoint(
                    projection_name="position",
                    aggregate_type="account",
                    aggregate_id="paper-1",
                    aggregate_version=2,
                    state=state,
                )
            )
            with self.assertRaisesRegex(ValueError, "cannot outrun"):
                store.save_projection_checkpoint(
                    projection_name="position",
                    aggregate_type="account",
                    aggregate_id="paper-1",
                    aggregate_version=3,
                    state={"net_quantity": "999"},
                )
            with self.assertRaisesRegex(ValueError, "cannot regress"):
                store.save_projection_checkpoint(
                    projection_name="position",
                    aggregate_type="account",
                    aggregate_id="paper-1",
                    aggregate_version=1,
                    state={"net_quantity": "1"},
                )

            reopened = JournalStore(path)
            checkpoint = reopened.load_projection_checkpoint(
                projection_name="position",
                aggregate_type="account",
                aggregate_id="paper-1",
            )
            self.assertEqual(checkpoint["state"], state)
            self.assertEqual(checkpoint["aggregate_version"], 2)

            rebuilt = sum(
                int(item["payload"]["quantity"])
                for item in reopened.load_events("account", "paper-1")
                if item["payload"].get("kind") == "fill"
            )
            self.assertEqual(str(rebuilt), checkpoint["state"]["net_quantity"])

    def test_global_projection_checkpoint_replays_exact_multi_aggregate_tail(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event())
            second = event("evt-2", 1, {"kind": "fill", "quantity": "2"})
            second["aggregate_id"] = "paper-2"
            second["payload_hash"] = payload_digest(second["payload"])
            store.append_event(second)

            cut = store.current_journal_sequence()
            self.assertEqual(cut, 2)
            self.assertTrue(
                store.save_global_projection_checkpoint(
                    projection_name="portfolio",
                    journal_sequence=cut,
                    state={"paper-1": "1", "paper-2": "2"},
                )
            )

            third = event("evt-3", 2, {"kind": "fill", "quantity": "3"})
            store.append_event(third)

            reopened = JournalStore(path)
            checkpoint = reopened.load_global_projection_checkpoint(
                projection_name="portfolio"
            )
            self.assertEqual(checkpoint["journal_sequence"], 2)
            self.assertEqual(
                checkpoint["state"],
                {"paper-1": "1", "paper-2": "2"},
            )
            tail = reopened.load_events_after_journal_sequence(
                checkpoint["journal_sequence"]
            )
            self.assertEqual(
                [item["journal_sequence"] for item in tail],
                [3],
            )
            rebuilt = dict(checkpoint["state"])
            for item in tail:
                if item["payload"].get("kind") == "fill":
                    rebuilt[item["aggregate_id"]] = str(
                        int(rebuilt.get(item["aggregate_id"], "0"))
                        + int(item["payload"]["quantity"])
                    )
            self.assertEqual(
                rebuilt,
                {"paper-1": "4", "paper-2": "2"},
            )

    def test_global_projection_checkpoint_is_monotonic_and_fail_closed(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event())
            self.assertTrue(
                store.save_global_projection_checkpoint(
                    projection_name="portfolio",
                    journal_sequence=1,
                    state={"net": "1"},
                )
            )
            self.assertFalse(
                store.save_global_projection_checkpoint(
                    projection_name=" portfolio ",
                    journal_sequence=1,
                    state={"net": "1"},
                )
            )
            with self.assertRaisesRegex(ValueError, "cannot outrun"):
                store.save_global_projection_checkpoint(
                    projection_name="portfolio",
                    journal_sequence=2,
                    state={"net": "2"},
                )
            with self.assertRaisesRegex(ValueError, "cannot regress or change"):
                store.save_global_projection_checkpoint(
                    projection_name="portfolio",
                    journal_sequence=1,
                    state={"net": "999"},
                )

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE global_projection_checkpoints SET state_json = ? "
                    "WHERE projection_name = ?",
                    ('{"net":"999"}', "portfolio"),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "hash does not match"):
                JournalStore(path).load_global_projection_checkpoint(
                    projection_name="portfolio"
                )

    def test_global_projection_checkpoint_cut_tamper_fails_closed(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event())
            second = event("evt-2", 2, {"kind": "fill", "quantity": "2"})
            store.append_event(second)
            self.assertTrue(
                store.save_global_projection_checkpoint(
                    projection_name="portfolio",
                    journal_sequence=2,
                    state={"net": "3"},
                )
            )

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE global_projection_checkpoints "
                    "SET journal_sequence = 1 WHERE projection_name = ?",
                    ("portfolio",),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "hash does not match identity, cut, and state",
            ):
                JournalStore(path).load_global_projection_checkpoint(
                    projection_name="portfolio"
                )

    def test_global_projection_checkpoint_fractional_cut_tamper_fails_closed(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event())
            self.assertTrue(
                store.save_global_projection_checkpoint(
                    projection_name="portfolio",
                    journal_sequence=1,
                    state={"net": "1"},
                )
            )

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE global_projection_checkpoints "
                    "SET journal_sequence = 1.5 WHERE projection_name = ?",
                    ("portfolio",),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "journal_sequence is not a canonical integer",
            ):
                JournalStore(path).load_global_projection_checkpoint(
                    projection_name="portfolio"
                )

            with self.assertRaisesRegex(
                ValueError,
                "journal_sequence is not a canonical integer",
            ):
                store.save_global_projection_checkpoint(
                    projection_name="portfolio",
                    journal_sequence=1,
                    state={"net": "1"},
                )

    def test_v6_upgrade_adds_global_projection_checkpoint_without_losing_journal(self):
        class V6JournalStore(JournalStore):
            SCHEMA_VERSION = 6

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            legacy = V6JournalStore(path)
            legacy.append_event(event(), outbox_topic="events")
            self.assertEqual(legacy.current_schema_version(), 6)

            upgraded = JournalStore(path)
            self.assertEqual(upgraded.current_schema_version(), 7)
            self.assertEqual(
                [item["event_id"] for item in upgraded.load_events_after_journal_sequence(0)],
                ["evt-1"],
            )
            self.assertEqual(len(upgraded.pending_outbox()), 1)
            self.assertTrue(
                upgraded.save_global_projection_checkpoint(
                    projection_name="portfolio",
                    journal_sequence=1,
                    state={"net": "1"},
                )
            )

    def test_projection_checkpoint_identity_cannot_split_on_whitespace(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event())
            state = {"net_quantity": "1"}

            self.assertTrue(
                store.save_projection_checkpoint(
                    projection_name=" position ",
                    aggregate_type=" account ",
                    aggregate_id=" paper-1 ",
                    aggregate_version=1,
                    state=state,
                )
            )
            self.assertFalse(
                store.save_projection_checkpoint(
                    projection_name="position",
                    aggregate_type="account",
                    aggregate_id="paper-1",
                    aggregate_version=1,
                    state=state,
                )
            )

            checkpoint = store.load_projection_checkpoint(
                projection_name=" position ",
                aggregate_type=" account ",
                aggregate_id=" paper-1 ",
            )
            self.assertEqual(checkpoint["projection_name"], "position")
            self.assertEqual(checkpoint["aggregate_type"], "account")
            self.assertEqual(checkpoint["aggregate_id"], "paper-1")

            connection = sqlite3.connect(path)
            try:
                count = connection.execute(
                    "SELECT COUNT(*) FROM projection_checkpoints"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 1)

    def test_projection_checkpoint_tamper_is_detected(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event())
            store.save_projection_checkpoint(
                projection_name="position",
                aggregate_type="account",
                aggregate_id="paper-1",
                aggregate_version=1,
                state={"net_quantity": "1"},
            )

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE projection_checkpoints SET state_json = ? "
                    "WHERE projection_name = 'position'",
                    ('{"net_quantity":"1000000"}',),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "hash does not match"):
                JournalStore(path).load_projection_checkpoint(
                    projection_name="position",
                    aggregate_type="account",
                    aggregate_id="paper-1",
                )

    def test_projection_checkpoint_version_tamper_fails_closed(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event())
            store.append_event(
                event("evt-2", 2, {"kind": "fill", "quantity": "2"})
            )
            store.save_projection_checkpoint(
                projection_name="position",
                aggregate_type="account",
                aggregate_id="paper-1",
                aggregate_version=2,
                state={"net_quantity": "3"},
            )

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE projection_checkpoints "
                    "SET aggregate_version = 1 "
                    "WHERE projection_name = 'position'"
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "hash does not match identity, version, and state",
            ):
                JournalStore(path).load_projection_checkpoint(
                    projection_name="position",
                    aggregate_type="account",
                    aggregate_id="paper-1",
                )

    def test_projection_checkpoint_fractional_version_tamper_fails_closed(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event())
            store.save_projection_checkpoint(
                projection_name="position",
                aggregate_type="account",
                aggregate_id="paper-1",
                aggregate_version=1,
                state={"net_quantity": "1"},
            )

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE projection_checkpoints "
                    "SET aggregate_version = 1.5 "
                    "WHERE projection_name = 'position'"
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "aggregate_version is not a canonical integer",
            ):
                JournalStore(path).load_projection_checkpoint(
                    projection_name="position",
                    aggregate_type="account",
                    aggregate_id="paper-1",
                )

    def test_projection_checkpoint_identity_tamper_fails_closed(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event())
            store.save_projection_checkpoint(
                projection_name="position",
                aggregate_type="account",
                aggregate_id="paper-1",
                aggregate_version=1,
                state={"net_quantity": "1"},
            )

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE projection_checkpoints "
                    "SET projection_name = 'position-renamed' "
                    "WHERE projection_name = 'position'"
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "hash does not match identity, version, and state",
            ):
                JournalStore(path).load_projection_checkpoint(
                    projection_name="position-renamed",
                    aggregate_type="account",
                    aggregate_id="paper-1",
                )

    def test_v7_upgrade_invalidates_unbound_projection_checkpoint(self):
        class V7JournalStore(JournalStore):
            SCHEMA_VERSION = 7

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            legacy = V7JournalStore(path)
            legacy.append_event(event())
            legacy.save_projection_checkpoint(
                projection_name="position",
                aggregate_type="account",
                aggregate_id="paper-1",
                aggregate_version=1,
                state={"net_quantity": "1"},
            )
            self.assertEqual(legacy.current_schema_version(), 7)
            self.assertIsNotNone(
                legacy.load_projection_checkpoint(
                    projection_name="position",
                    aggregate_type="account",
                    aggregate_id="paper-1",
                )
            )

            upgraded = JournalStore(path)
            self.assertEqual(upgraded.current_schema_version(), 8)
            self.assertIsNone(
                upgraded.load_projection_checkpoint(
                    projection_name="position",
                    aggregate_type="account",
                    aggregate_id="paper-1",
                )
            )
            self.assertEqual(
                [item["event_id"] for item in upgraded.load_events("account", "paper-1")],
                ["evt-1"],
            )
            self.assertTrue(
                upgraded.save_projection_checkpoint(
                    projection_name="position",
                    aggregate_type="account",
                    aggregate_id="paper-1",
                    aggregate_version=1,
                    state={"net_quantity": "1"},
                )
            )
            rebuilt = upgraded.load_projection_checkpoint(
                projection_name="position",
                aggregate_type="account",
                aggregate_id="paper-1",
            )
            self.assertEqual(rebuilt["aggregate_version"], 1)
            self.assertEqual(rebuilt["state"], {"net_quantity": "1"})


    def test_event_retry_rejects_changed_committed_at(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            original = event()
            store.append_event(original)

            changed = dict(original)
            changed["committed_at"] = "2026-09-25T01:00:00+00:00"
            with self.assertRaisesRegex(ValueError, "publication intent"):
                store.append_event(changed)

            persisted = store.get_event(original["event_id"])
            self.assertEqual(persisted["committed_at"], original["committed_at"])

    def test_event_retry_rejects_changed_outbox_intent(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            original = event()
            store.append_event(original)

            with self.assertRaisesRegex(ValueError, "publication intent"):
                store.append_event(original, outbox_topic="events")
            self.assertEqual(store.pending_outbox(), [])

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            original = event()
            store.append_event(original, outbox_topic="events")

            with self.assertRaisesRegex(ValueError, "publication intent"):
                store.append_event(original)
            self.assertEqual(len(store.pending_outbox()), 1)

    def test_event_retry_rejects_changed_outbox_topic(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            original = event()
            store.append_event(original, outbox_topic="events")

            with self.assertRaisesRegex(ValueError, "publication intent"):
                store.append_event(original, outbox_topic="other-events")

            pending = store.pending_outbox()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["topic"], "events")



    def test_non_outbox_event_envelope_integrity_is_verified_on_reads(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            envelope = event()
            envelope.update(
                {
                    "schema_version": "1.0.0",
                    "host_id": "host-a",
                    "owner_epoch": "1",
                    "environment": "SIMULATION",
                    "occurred_at": "2026-09-24T16:00:00+00:00",
                    "observed_at": "2026-09-24T16:00:00+00:00",
                    "correlation_id": "corr-1",
                    "causation_id": None,
                    "evidence_refs": [],
                }
            )
            store.append_event(envelope)

            self.assertEqual(store.get_event("evt-1")["event_id"], "evt-1")
            self.assertEqual(len(store.load_events("account", "paper-1")), 1)

            connection = sqlite3.connect(path)
            try:
                raw = connection.execute(
                    "SELECT envelope_json FROM events WHERE event_id = ?",
                    ("evt-1",),
                ).fetchone()[0]
                mutated = json.loads(raw)
                mutated["host_id"] = "host-forged"
                mutated_json = json.dumps(
                    mutated,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                connection.execute(
                    "UPDATE events SET envelope_json = ? WHERE event_id = ?",
                    (mutated_json, "evt-1"),
                )
                connection.commit()
            finally:
                connection.close()

            reopened = JournalStore(path)
            with self.assertRaisesRegex(ValueError, "event envelope hash"):
                reopened.get_event("evt-1")
            with self.assertRaisesRegex(ValueError, "event envelope hash"):
                reopened.load_events("account", "paper-1")

            connection = sqlite3.connect(path)
            try:
                mutated = json.loads(mutated_json)
                mutated["event_type"] = "ForgedEventType"
                forged_json = json.dumps(
                    mutated,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                connection.execute(
                    "UPDATE events SET envelope_json = ?, envelope_hash = ? "
                    "WHERE event_id = ?",
                    (
                        forged_json,
                        _event_envelope_digest(forged_json),
                        "evt-1",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "conflicts with core journal event"):
                reopened.get_event("evt-1")

    def test_event_reads_fail_closed_after_payload_tamper(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event(), outbox_topic="events")

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE events SET payload_json = ? WHERE event_id = ?",
                    ('{"kind":"tampered","quantity":"999"}', "evt-1"),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "payload hash"):
                store.get_event("evt-1")
            with self.assertRaisesRegex(ValueError, "payload hash"):
                store.load_events("account", "paper-1")
            with self.assertRaisesRegex(ValueError, "payload hash"):
                store.pending_outbox()

    def test_pending_outbox_fails_closed_when_envelope_diverges_from_event(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.append_event(event(), outbox_topic="events")

            connection = sqlite3.connect(path)
            try:
                raw = connection.execute(
                    "SELECT payload_json FROM outbox WHERE event_id = ?",
                    ("evt-1",),
                ).fetchone()[0]
                envelope = json.loads(raw)
                envelope["aggregate_id"] = "other-account"
                forged_payload = json.dumps(
                    envelope,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                connection.execute(
                    "UPDATE outbox SET payload_json = ?, envelope_hash = ? "
                    "WHERE event_id = ?",
                    (
                        forged_payload,
                        _outbox_envelope_digest("events", forged_payload),
                        "evt-1",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "outbox payload does not match authoritative journal event",
            ):
                store.pending_outbox()

if __name__ == "__main__":
    unittest.main()
