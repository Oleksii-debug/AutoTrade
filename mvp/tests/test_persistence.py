from tempfile import TemporaryDirectory
import json
import sqlite3
import unittest

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


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
            self.assertEqual(len(store.pending_outbox()), 1)

            replayed, inserted, appended = store.commit_command(
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
            self.assertEqual(len(store.load_events("account", "paper-1")), 2)

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

    def test_v1_database_upgrades_atomically_without_losing_events(self):
        class LegacyJournalStore(JournalStore):
            SCHEMA_VERSION = 1

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            legacy = LegacyJournalStore(path)
            legacy.append_event(event(), outbox_topic="events")
            self.assertEqual(legacy.current_schema_version(), 1)

            upgraded = JournalStore(path)
            self.assertEqual(upgraded.current_schema_version(), 3)
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
            self.assertEqual(upgraded.current_schema_version(), 3)
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

    def test_failed_migration_rolls_back_schema_and_data_changes(self):
        class BrokenMigrationStore(JournalStore):
            SCHEMA_VERSION = 4

            @classmethod
            def _migration_statements(cls, version):
                if version == 4:
                    return (
                        "CREATE TABLE migration_probe(value TEXT NOT NULL)",
                        "CREATE TABL definitely_invalid(statement TEXT)",
                    )
                return super()._migration_statements(version)

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            healthy = JournalStore(path)
            healthy.append_event(event())
            self.assertEqual(healthy.current_schema_version(), 3)

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

            self.assertEqual(versions, [1, 2, 3])
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
                connection.execute(
                    "UPDATE outbox SET payload_json = ? WHERE event_id = ?",
                    (json.dumps(envelope, sort_keys=True, separators=(",", ":")), "evt-1"),
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
