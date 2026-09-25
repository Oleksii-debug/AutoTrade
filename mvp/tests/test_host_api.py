import unittest

from mvp.autotrade_mvp.host_api import EventGap, HostCommandStore, operation_result_payload


class HostCommandStateTests(unittest.TestCase):
    def setUp(self):
        self.sessions = {("session-a", "alice"), ("session-b", "bob")}
        self.store = HostCommandStore(
            account_id="paper-account-1",
            environment="PAPER",
            session_validator=lambda session, actor: (session, actor) in self.sessions,
            max_events=3,
        )

    @staticmethod
    def command(
        *,
        command_id="11111111-1111-1111-1111-111111111111",
        key="key-1",
        version="0",
        actor="alice",
        session="session-a",
        account_id="paper-account-1",
        environment="PAPER",
        action="BLOCK_NEW_EXPOSURE",
        payload=None,
    ):
        return {
            "command_id": command_id,
            "expected_state_version": version,
            "idempotency_key": key,
            "actor": actor,
            "session": session,
            "account_id": account_id,
            "environment": environment,
            "action": action,
            "payload": payload or {},
        }

    def test_v2_scope_is_required_canonical_and_matches_active_host(self):
        missing_account = self.command()
        missing_account.pop("account_id")
        with self.assertRaisesRegex(ValueError, "account_id"):
            self.store.submit(missing_account)

        missing_environment = self.command()
        missing_environment.pop("environment")
        with self.assertRaisesRegex(ValueError, "environment"):
            self.store.submit(missing_environment)

        with self.assertRaisesRegex(ValueError, "canonical Environment"):
            self.store.submit(self.command(environment="paper"))
        with self.assertRaisesRegex(ValueError, "active host"):
            self.store.submit(self.command(account_id="other-account"))
        with self.assertRaisesRegex(ValueError, "active host"):
            self.store.submit(self.command(environment="LIVE"))

        accepted = self.store.submit(self.command())
        self.assertEqual(accepted.status, "ACCEPTED")
        event = self.store.events_after(0)[0]
        self.assertEqual(event.payload["account_id"], "paper-account-1")
        self.assertEqual(event.payload["environment"], "PAPER")
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["account_id"], "paper-account-1")
        self.assertEqual(snapshot["environment"], "PAPER")

    def test_idempotency_scope_includes_actor_and_environment(self):
        first = self.store.submit(self.command(key="shared-key"))
        self.assertEqual(first.status, "ACCEPTED")
        second = self.store.submit(
            self.command(
                command_id="22222222-2222-2222-2222-222222222222",
                key="shared-key",
                version="1",
                actor="bob",
                session="session-b",
            )
        )
        self.assertEqual(second.status, "ACCEPTED")
        self.assertEqual(self.store.state_version, 2)

    def test_acceptance_is_not_reported_as_financial_completion(self):
        result = self.store.submit(self.command())
        self.assertEqual(result.status, "ACCEPTED")
        operation = self.store.get_operation(result.operation_id)
        self.assertEqual(operation.phase, "QUEUED")
        self.assertIn("financial_outcome_not_completed", operation.remaining_uncertainty)

    def test_identical_retry_is_idempotent_without_new_event_or_version(self):
        command = self.command()
        first = self.store.submit(command)
        second = self.store.submit(command)
        self.assertEqual(first, second)
        self.assertEqual(self.store.state_version, 1)
        self.assertEqual(self.store.cursor, 1)

    def test_changed_payload_under_same_idempotency_key_conflicts(self):
        self.store.submit(self.command(payload={"scope": "A"}))
        conflict = self.store.submit(self.command(payload={"scope": "B"}))
        self.assertEqual(conflict.status, "CONFLICT")
        self.assertIn("idempotency_key_conflict", conflict.reason_codes)
        self.assertEqual(self.store.state_version, 1)

    def test_same_command_identifier_with_different_request_conflicts(self):
        first = self.command(key="key-a")
        self.store.submit(first)
        changed = self.command(key="key-b", payload={"different": True})
        conflict = self.store.submit(changed)
        self.assertEqual(conflict.status, "CONFLICT")
        self.assertIn("command_id_conflict", conflict.reason_codes)

    def test_stale_state_prevents_two_sessions_lost_update(self):
        accepted = self.store.submit(self.command())
        self.assertEqual(accepted.state_version, "1")
        stale = self.store.submit(
            self.command(
                command_id="22222222-2222-2222-2222-222222222222",
                key="key-2",
                version="0",
                actor="bob",
                session="session-b",
            )
        )
        self.assertEqual(stale.status, "CONFLICT")
        self.assertIn("stale_state_version", stale.reason_codes)
        self.assertEqual(stale.state_version, "1")

    def test_unauthorized_session_is_rejected_before_mutation(self):
        with self.assertRaises(PermissionError):
            self.store.submit(self.command(session="forged"))
        self.assertEqual(self.store.state_version, 0)
        self.assertEqual(self.store.cursor, 0)

    def test_internal_operation_version_does_not_drift_canonical_payload(self):
        accepted = self.store.submit(self.command())
        operation = self.store.get_operation(accepted.operation_id)
        self.assertEqual(operation.state_version, "1")
        payload = operation_result_payload(operation)
        self.assertNotIn("state_version", payload)
        self.assertEqual(
            set(payload),
            {
                "operation_id",
                "phase",
                "started_at",
                "updated_at",
                "affected_refs",
                "evidence",
                "remaining_uncertainty",
            },
        )

    def test_operation_completion_is_separate_versioned_transition(self):
        accepted = self.store.submit(self.command())
        completed = self.store.update_operation(accepted.operation_id, "SUCCEEDED")
        self.assertEqual(completed.phase, "SUCCEEDED")
        self.assertEqual(completed.state_version, "2")
        self.assertEqual(self.store.snapshot()["state_version"], "2")
        with self.assertRaises(ValueError):
            self.store.update_operation(accepted.operation_id, "FAILED")

    def test_unknown_preserves_uncertainty_and_can_only_resolve_terminally(self):
        accepted = self.store.submit(self.command())
        unknown = self.store.update_operation(
            accepted.operation_id,
            "UNKNOWN",
            remaining_uncertainty=("provider_outcome_unresolved",),
        )
        self.assertEqual(unknown.phase, "UNKNOWN")
        self.assertEqual(
            unknown.remaining_uncertainty,
            ("provider_outcome_unresolved",),
        )
        event = self.store.events_after("1")[0]
        self.assertEqual(
            event.payload["remaining_uncertainty"],
            ["provider_outcome_unresolved"],
        )
        with self.assertRaisesRegex(ValueError, "only resolve"):
            self.store.update_operation(
                accepted.operation_id,
                "RUNNING",
                remaining_uncertainty=("still_unknown",),
            )

        resolved = self.store.update_operation(accepted.operation_id, "SUCCEEDED")
        self.assertEqual(resolved.phase, "SUCCEEDED")
        self.assertEqual(resolved.remaining_uncertainty, ())

    def test_unknown_requires_uncertainty_and_terminal_cannot_hide_it(self):
        accepted = self.store.submit(self.command())
        with self.assertRaisesRegex(ValueError, "preserve remaining uncertainty"):
            self.store.update_operation(accepted.operation_id, "UNKNOWN")
        with self.assertRaisesRegex(ValueError, "cannot retain unresolved uncertainty"):
            self.store.update_operation(
                accepted.operation_id,
                "FAILED",
                remaining_uncertainty=("provider_outcome_unresolved",),
            )

    def test_resumable_events_return_only_newer_items(self):
        accepted = self.store.submit(self.command())
        self.store.update_operation(accepted.operation_id, "RUNNING")
        self.store.update_operation(accepted.operation_id, "WAITING_EXTERNAL")
        events = self.store.events_after("1")
        self.assertEqual([item.cursor for item in events], [2, 3])

    def test_event_retention_gap_requires_resnapshot(self):
        accepted = self.store.submit(self.command())
        self.store.update_operation(accepted.operation_id, "RUNNING")
        self.store.update_operation(accepted.operation_id, "WAITING_EXTERNAL")
        self.store.update_operation(accepted.operation_id, "SUCCEEDED")
        with self.assertRaises(EventGap):
            self.store.events_after("0")
        self.assertEqual(self.store.snapshot()["event_cursor"], "4")

    def test_future_cursor_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.events_after("1")


if __name__ == "__main__":
    unittest.main()
