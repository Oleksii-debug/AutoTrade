from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.durable_host_api import JournalBackedHostCommandStore
from mvp.autotrade_mvp.host_api import EventGap
from mvp.autotrade_mvp.persistence import JournalStore


class JournalBackedHostApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = f"{self.directory.name}/journal.sqlite3"
        self.sessions = {("session-a", "alice"), ("session-b", "bob")}

    def store(self, *, max_events=100):
        return JournalBackedHostCommandStore(
            JournalStore(self.path),
            session_validator=lambda session, actor: (session, actor) in self.sessions,
            max_events=max_events,
            now=lambda: "2026-09-24T18:00:00Z",
        )

    @staticmethod
    def command(
        *,
        command_id="11111111-1111-1111-1111-111111111111",
        key="key-1",
        version="0",
        actor="alice",
        session="session-a",
        action="BLOCK_NEW_EXPOSURE",
        payload=None,
    ):
        return {
            "command_id": command_id,
            "expected_state_version": version,
            "idempotency_key": key,
            "actor": actor,
            "session": session,
            "action": action,
            "payload": payload or {},
        }

    def test_accepted_command_and_operation_survive_restart(self):
        first = self.store()
        accepted = first.submit(self.command())
        self.assertEqual(accepted.status, "ACCEPTED")
        self.assertEqual(accepted.state_version, "1")

        restarted = self.store()
        self.assertEqual(restarted.snapshot()["state_version"], "1")
        operation = restarted.get_operation(accepted.operation_id)
        self.assertEqual(operation.phase, "QUEUED")
        self.assertIn(
            "financial_outcome_not_completed",
            operation.remaining_uncertainty,
        )

        running = restarted.update_operation(
            accepted.operation_id,
            "RUNNING",
            remaining_uncertainty=("provider_response_pending",),
        )
        self.assertEqual(running.state_version, "2")

        again = self.store()
        projected = again.get_operation(accepted.operation_id)
        self.assertEqual(projected.phase, "RUNNING")
        self.assertEqual(
            projected.remaining_uncertainty,
            ("provider_response_pending",),
        )

    def test_exact_retry_after_restart_returns_original_result_without_new_event(self):
        first = self.store()
        command = self.command()
        accepted = first.submit(command)
        self.assertEqual(first.cursor, 1)

        restarted = self.store()
        retried = restarted.submit(command)
        self.assertEqual(retried, accepted)
        self.assertEqual(restarted.cursor, 1)
        self.assertEqual(len(restarted.events_after(0)), 1)

    def test_changed_payload_under_same_idempotency_key_conflicts_after_restart(self):
        first = self.store()
        first.submit(self.command(payload={"scope": "A"}))

        restarted = self.store()
        conflict = restarted.submit(self.command(payload={"scope": "B"}))
        self.assertEqual(conflict.status, "CONFLICT")
        self.assertIn("idempotency_key_conflict", conflict.reason_codes)
        self.assertEqual(restarted.state_version, 1)

    def test_same_command_id_under_new_key_conflicts_after_restart(self):
        first = self.store()
        first.submit(self.command(key="key-a"))

        restarted = self.store()
        conflict = restarted.submit(
            self.command(key="key-b", payload={"different": True})
        )
        self.assertEqual(conflict.status, "CONFLICT")
        self.assertIn("command_id_conflict", conflict.reason_codes)
        self.assertEqual(restarted.state_version, 1)

    def test_stale_state_conflict_is_durable_and_does_not_emit_financial_event(self):
        first = self.store()
        first.submit(self.command())
        stale_command = self.command(
            command_id="22222222-2222-2222-2222-222222222222",
            key="key-2",
            version="0",
            actor="bob",
            session="session-b",
        )
        stale = first.submit(stale_command)
        self.assertEqual(stale.status, "CONFLICT")
        self.assertIn("stale_state_version", stale.reason_codes)
        self.assertEqual(first.cursor, 1)

        restarted = self.store()
        retried = restarted.submit(stale_command)
        self.assertEqual(retried, stale)
        self.assertEqual(restarted.cursor, 1)

    def test_unauthorized_session_never_reaches_journal(self):
        store = self.store()
        with self.assertRaises(PermissionError):
            store.submit(self.command(session="forged"))
        self.assertEqual(store.state_version, 0)
        self.assertEqual(store.cursor, 0)

    def test_terminal_transition_is_persisted_and_cannot_be_rewritten(self):
        store = self.store()
        accepted = store.submit(self.command())
        completed = store.update_operation(accepted.operation_id, "SUCCEEDED")
        self.assertEqual(completed.phase, "SUCCEEDED")

        restarted = self.store()
        same = restarted.update_operation(accepted.operation_id, "SUCCEEDED")
        self.assertEqual(same, completed)
        with self.assertRaisesRegex(ValueError, "Terminal"):
            restarted.update_operation(accepted.operation_id, "FAILED")

    def test_retention_gap_is_explicit_but_full_snapshot_remains_current(self):
        store = self.store(max_events=2)
        accepted = store.submit(self.command())
        store.update_operation(accepted.operation_id, "RUNNING")
        store.update_operation(accepted.operation_id, "WAITING_EXTERNAL")
        self.assertEqual(store.snapshot()["event_cursor"], "3")
        with self.assertRaises(EventGap):
            store.events_after(0)
        recent = store.events_after(1)
        self.assertEqual([event.cursor for event in recent], [2, 3])

    def test_two_sessions_cannot_commit_against_same_stale_state(self):
        store = self.store()
        alice = store.submit(self.command())
        self.assertEqual(alice.status, "ACCEPTED")
        bob = store.submit(
            self.command(
                command_id="33333333-3333-3333-3333-333333333333",
                key="key-bob",
                version="0",
                actor="bob",
                session="session-b",
            )
        )
        self.assertEqual(bob.status, "CONFLICT")
        self.assertIn("stale_state_version", bob.reason_codes)
        self.assertEqual(store.state_version, 1)


if __name__ == "__main__":
    unittest.main()
