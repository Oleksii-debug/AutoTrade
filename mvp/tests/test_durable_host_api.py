from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.authority import AuthorityPolicy, AuthorityService
from mvp.autotrade_mvp.durable_host_api import JournalBackedHostCommandStore
from mvp.autotrade_mvp.host_api import EventGap, HostCommandStore
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


class JournalBackedHostApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = f"{self.directory.name}/journal.sqlite3"
        self.sessions = {("session-a", "alice"), ("session-b", "bob")}

    def store(
        self,
        *,
        max_events=100,
        account_id="paper-account-1",
        environment="PAPER",
        now="2026-09-24T18:00:00Z",
    ):
        return JournalBackedHostCommandStore(
            JournalStore(self.path),
            account_id=account_id,
            environment=environment,
            session_validator=lambda session, actor, origin, action: (session, actor) in self.sessions,
            max_events=max_events,
            request_origin_provider=lambda: "https://local.autotrade.invalid",
            now=lambda: now,
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

    @staticmethod
    def authority_policy(policy_id, *, protection_only=False):
        return AuthorityPolicy.create(
            policy_id=policy_id,
            account_id="paper-account-1",
            environments={"PAPER"},
            instruments={
                ("11111111-1111-4111-8111-111111111111", 1),
            },
            actions={"ORDER.SUBMIT"},
            max_notional="1000",
            valid_from="2029-01-01T00:00:00Z",
            expires_at="2035-01-01T00:00:00Z",
            autonomous=True,
            protection_only=protection_only,
            version=1,
        )

    def test_v2_scope_is_required_canonical_and_matches_active_host(self):
        store = self.store()
        missing_account = self.command()
        missing_account.pop("account_id")
        with self.assertRaisesRegex(ValueError, "account_id"):
            store.submit(missing_account)
        missing_environment = self.command()
        missing_environment.pop("environment")
        with self.assertRaisesRegex(ValueError, "environment"):
            store.submit(missing_environment)
        with self.assertRaisesRegex(ValueError, "canonical Environment"):
            store.submit(self.command(environment="paper"))
        with self.assertRaisesRegex(ValueError, "active host"):
            store.submit(self.command(account_id="other-account"))
        with self.assertRaisesRegex(ValueError, "active host"):
            store.submit(self.command(environment="LIVE"))

        accepted = store.submit(self.command())
        self.assertEqual(accepted.status, "ACCEPTED")
        event = store.events_after(0)[0]
        self.assertEqual(event.payload["account_id"], "paper-account-1")
        self.assertEqual(event.payload["environment"], "PAPER")
        snapshot = store.snapshot()
        self.assertEqual(snapshot["account_id"], "paper-account-1")
        self.assertEqual(snapshot["environment"], "PAPER")

    def test_unknown_or_noncanonical_action_is_rejected_before_durable_mutation(self):
        store = self.store()
        for action in (
            "FUTURE_PRIVILEGED_ACTION",
            "block_new_exposure",
            " BLOCK_NEW_EXPOSURE",
        ):
            with self.subTest(action=action), self.assertRaisesRegex(
                ValueError,
                "host action",
            ):
                store.submit(self.command(action=action))
            self.assertEqual(store.state_version, 0)
            self.assertEqual(store.events_after(0), ())

    def test_same_v2_command_has_identical_in_memory_and_durable_admission(self):
        command = self.command()
        memory = HostCommandStore(
            account_id="paper-account-1",
            environment="PAPER",
            session_validator=lambda session, actor, origin, action: (session, actor) in self.sessions,
            request_origin_provider=lambda: "https://local.autotrade.invalid",
            now=lambda: "2026-09-24T18:00:00Z",
        )
        self.assertEqual(memory.submit(command), self.store().submit(command))

    def test_restart_rejects_journal_from_another_active_account_scope(self):
        first = self.store()
        accepted = first.submit(self.command())
        wrong_scope = self.store(account_id="other-account")
        with self.assertRaisesRegex(ValueError, "journal command scope"):
            wrong_scope.get_operation(accepted.operation_id)

    def test_durable_idempotency_scope_includes_actor_and_environment(self):
        store = self.store()
        first = store.submit(self.command(key="shared-key"))
        self.assertEqual(first.status, "ACCEPTED")
        second = store.submit(
            self.command(
                command_id="22222222-2222-2222-2222-222222222222",
                key="shared-key",
                version="1",
                actor="bob",
                session="session-b",
            )
        )
        self.assertEqual(second.status, "ACCEPTED")
        self.assertEqual(store.state_version, 2)

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
        first.submit(self.command(payload={"reason": "reason-a"}))

        restarted = self.store()
        conflict = restarted.submit(self.command(payload={"reason": "reason-b"}))
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

    def test_restart_rejects_operation_update_without_accepted_origin(self):
        journal = JournalStore(self.path)
        journal.append_event(
            {
                "event_id": "forged-update",
                "event_type": "OPERATION_UPDATED",
                "aggregate_type": JournalBackedHostCommandStore.AGGREGATE_TYPE,
                "aggregate_id": JournalBackedHostCommandStore.AGGREGATE_ID,
                "aggregate_version": "1",
                "payload": {
                    "operation_id": "ghost-operation",
                    "phase": "SUCCEEDED",
                    "remaining_uncertainty": [],
                },
                "payload_hash": payload_digest(
                    {
                        "operation_id": "ghost-operation",
                        "phase": "SUCCEEDED",
                        "remaining_uncertainty": [],
                    }
                ),
                "committed_at": "2026-09-24T18:00:00Z",
            }
        )
        restarted = self.store()
        with self.assertRaisesRegex(
            ValueError,
            "cannot precede COMMAND_ACCEPTED",
        ):
            restarted.snapshot()

    def test_restart_rejects_terminal_rewrite_in_journal_history(self):
        store = self.store(now="2030-01-01T00:00:00Z")
        accepted = store.submit(self.command())
        store.execute_authority_operation(accepted.operation_id)

        journal = JournalStore(self.path)
        payload = {
            "operation_id": accepted.operation_id,
            "phase": "FAILED",
            "remaining_uncertainty": [],
        }
        journal.append_event(
            {
                "event_id": "forged-terminal-rewrite",
                "event_type": "OPERATION_UPDATED",
                "aggregate_type": JournalBackedHostCommandStore.AGGREGATE_TYPE,
                "aggregate_id": JournalBackedHostCommandStore.AGGREGATE_ID,
                "aggregate_version": "3",
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "committed_at": "2026-09-24T18:00:01Z",
            }
        )
        with self.assertRaisesRegex(ValueError, "rewrites a terminal"):
            self.store().snapshot()

    def test_restart_rejects_malformed_persisted_operation_arrays(self):
        cases = (
            ("remaining_uncertainty", [123]),
            ("affected_refs", [123]),
            ("evidence", ["not-an-object"]),
        )
        for field, bad_value in cases:
            with self.subTest(field=field), TemporaryDirectory() as directory:
                path = f"{directory}/journal.sqlite3"
                journal = JournalStore(path)
                store = JournalBackedHostCommandStore(
                    journal,
                    account_id="paper-account-1",
                    environment="PAPER",
                    session_validator=lambda session, actor, origin, action: (
                        session,
                        actor,
                    ) in self.sessions,
                    request_origin_provider=lambda: "https://local.autotrade.invalid",
                    now=lambda: "2026-09-24T18:00:00Z",
                )
                accepted = store.submit(self.command())
                payload = {
                    "operation_id": accepted.operation_id,
                    "phase": "RUNNING",
                    "remaining_uncertainty": ["provider_response_pending"],
                    "affected_refs": ["order:provider-1"],
                    "evidence": [{"kind": "provider-observation"}],
                }
                payload[field] = bad_value
                journal.append_event(
                    {
                        "event_id": f"malformed-{field}",
                        "event_type": "OPERATION_UPDATED",
                        "aggregate_type": JournalBackedHostCommandStore.AGGREGATE_TYPE,
                        "aggregate_id": JournalBackedHostCommandStore.AGGREGATE_ID,
                        "aggregate_version": "2",
                        "payload": payload,
                        "payload_hash": payload_digest(payload),
                        "committed_at": "2026-09-24T18:00:01Z",
                    }
                )
                restarted = JournalBackedHostCommandStore(
                    JournalStore(path),
                    account_id="paper-account-1",
                    environment="PAPER",
                    session_validator=lambda session, actor, origin, action: (
                        session,
                        actor,
                    ) in self.sessions,
                    request_origin_provider=lambda: "https://local.autotrade.invalid",
                    now=lambda: "2026-09-24T18:00:02Z",
                )
                with self.assertRaisesRegex(ValueError, "Host journal"):
                    restarted.snapshot()

    def test_restart_rejects_non_object_command_acceptance_evidence(self):
        journal = JournalStore(self.path)
        action_payload = {
            "schema_version": 1,
            "expected_authority_epoch": "0",
            "expected_authority_version": "0",
            "reason": "operator_block_new_exposure",
            "target_policies": [],
        }
        payload = {
            "command_id": "manual-command",
            "operation_id": "manual-operation",
            "action": "BLOCK_NEW_EXPOSURE",
            "action_payload": action_payload,
            "action_payload_hash": payload_digest(action_payload),
            "actor": "alice",
            "account_id": "paper-account-1",
            "environment": "PAPER",
            "phase": "QUEUED",
            "started_at": "2026-09-24T18:00:00Z",
            "updated_at": "2026-09-24T18:00:00Z",
            "affected_refs": [],
            "evidence": ["silently-droppable-before-hardening"],
            "remaining_uncertainty": ["financial_outcome_not_completed"],
        }
        journal.append_event(
            {
                "event_id": "malformed-command-evidence",
                "event_type": "COMMAND_ACCEPTED",
                "aggregate_type": JournalBackedHostCommandStore.AGGREGATE_TYPE,
                "aggregate_id": JournalBackedHostCommandStore.AGGREGATE_ID,
                "aggregate_version": "1",
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "committed_at": "2026-09-24T18:00:00Z",
            }
        )
        with self.assertRaisesRegex(ValueError, "evidence"):
            self.store().snapshot()

    def test_terminal_transition_is_persisted_and_cannot_be_rewritten(self):
        store = self.store(now="2030-01-01T00:00:00Z")
        accepted = store.submit(self.command())
        completed = store.execute_authority_operation(accepted.operation_id)
        self.assertEqual(completed.phase, "SUCCEEDED")

        restarted = self.store()
        same = restarted.update_operation(accepted.operation_id, "SUCCEEDED")
        self.assertEqual(same, completed)
        with self.assertRaisesRegex(ValueError, "Terminal"):
            restarted.update_operation(accepted.operation_id, "FAILED")

    def test_unknown_survives_restart_then_resolves_from_external_evidence(self):
        store = self.store()
        accepted = store.submit(self.command())
        unknown = store.update_operation(
            accepted.operation_id,
            "UNKNOWN",
            remaining_uncertainty=("provider_outcome_unresolved",),
        )
        self.assertEqual(unknown.phase, "UNKNOWN")

        restarted = self.store()
        recovered = restarted.get_operation(accepted.operation_id)
        self.assertEqual(recovered.phase, "UNKNOWN")
        self.assertEqual(
            recovered.remaining_uncertainty,
            ("provider_outcome_unresolved",),
        )
        with self.assertRaisesRegex(ValueError, "only resolve"):
            restarted.update_operation(
                accepted.operation_id,
                "WAITING_EXTERNAL",
                remaining_uncertainty=("provider_outcome_unresolved",),
            )

        resolved = restarted.execute_authority_operation(accepted.operation_id)
        self.assertEqual(resolved.phase, "SUCCEEDED")
        self.assertEqual(resolved.remaining_uncertainty, ())

        final = self.store().get_operation(accepted.operation_id)
        self.assertEqual(final.phase, "SUCCEEDED")
        self.assertEqual(final.remaining_uncertainty, ())

    def test_unknown_requires_uncertainty_and_terminal_cannot_hide_it(self):
        store = self.store()
        accepted = store.submit(self.command())
        with self.assertRaisesRegex(ValueError, "preserve remaining uncertainty"):
            store.update_operation(accepted.operation_id, "UNKNOWN")
        with self.assertRaisesRegex(ValueError, "cannot retain unresolved uncertainty"):
            store.update_operation(
                accepted.operation_id,
                "CANCELLED",
                remaining_uncertainty=("cancel_ack_not_completion",),
            )

    def test_block_new_exposure_persists_canonical_targets_and_evidence(self):
        journal = JournalStore(self.path)
        authority = AuthorityService(journal)
        authority.register_policy(self.authority_policy("exposure-policy"))
        authority.register_policy(
            self.authority_policy("protection-policy", protection_only=True)
        )
        store = self.store(now="2030-01-01T00:00:00Z")

        accepted = store.submit(
            self.command(payload={"reason": "operator emergency block"})
        )
        event = store.events_after(0)[0]
        action_payload = event.payload["action_payload"]
        self.assertEqual(
            [item["policy_id"] for item in action_payload["target_policies"]],
            ["exposure-policy"],
        )
        self.assertNotIn("session", action_payload)
        self.assertNotIn("actor", action_payload)
        self.assertTrue(
            str(event.payload["action_payload_hash"]).startswith("sha256:")
        )

        completed = store.execute_authority_operation(accepted.operation_id)
        self.assertEqual(completed.phase, "SUCCEEDED")
        self.assertEqual(
            completed.affected_refs,
            ("authority-policy:exposure-policy",),
        )
        self.assertEqual(
            completed.evidence[0]["event_type"],
            "AuthorityPolicyRevoked",
        )

        restored = AuthorityService(JournalStore(self.path)).export_state()
        revoked = {item["policy_id"] for item in restored["revocations"]}
        self.assertEqual(revoked, {"exposure-policy"})
        self.assertEqual(
            self.store(now="2030-01-01T00:00:01Z")
            .get_operation(accepted.operation_id)
            .phase,
            "SUCCEEDED",
        )

    def test_revoke_authority_includes_protection_policies(self):
        journal = JournalStore(self.path)
        authority = AuthorityService(journal)
        authority.register_policy(self.authority_policy("exposure-policy"))
        authority.register_policy(
            self.authority_policy("protection-policy", protection_only=True)
        )
        store = self.store(now="2030-01-01T00:00:00Z")
        accepted = store.submit(self.command(action="REVOKE_AUTHORITY"))
        event = store.events_after(0)[0]
        self.assertEqual(
            [
                item["policy_id"]
                for item in event.payload["action_payload"]["target_policies"]
            ],
            ["exposure-policy", "protection-policy"],
        )
        completed = store.execute_authority_operation(accepted.operation_id)
        self.assertEqual(completed.phase, "SUCCEEDED")
        self.assertEqual(len(completed.evidence), 2)
        restored = AuthorityService(JournalStore(self.path)).export_state()
        self.assertEqual(
            {item["policy_id"] for item in restored["revocations"]},
            {"exposure-policy", "protection-policy"},
        )

    def test_set_authority_uses_host_scope_and_survives_restart(self):
        store = self.store(now="2030-01-01T00:00:00Z")
        payload = {
            "policy_id": "operator-policy",
            "environments": ["PAPER"],
            "instruments": [
                {
                    "instrument_id": "11111111-1111-4111-8111-111111111111",
                    "version": 1,
                }
            ],
            "actions": ["ORDER.SUBMIT"],
            "max_notional": "250.00",
            "valid_from": "2030-01-01T00:00:00Z",
            "expires_at": "2035-01-01T00:00:00Z",
            "autonomous": False,
            "protection_only": False,
            "version": 1,
        }
        command = self.command(action="SET_AUTHORITY", payload=payload)
        accepted = store.submit(command)
        event = store.events_after(0)[0]
        canonical = event.payload["action_payload"]["policy"]
        self.assertEqual(canonical["account_id"], "paper-account-1")
        self.assertEqual(canonical["max_notional"], "250")
        self.assertNotIn("session", canonical)

        completed = store.execute_authority_operation(accepted.operation_id)
        self.assertEqual(completed.phase, "SUCCEEDED")
        authority = AuthorityService(JournalStore(self.path)).export_state()
        self.assertEqual(authority["policies"][0], canonical)

        restarted = self.store(now="2030-01-01T00:00:01Z")
        self.assertEqual(
            restarted.execute_authority_operation(accepted.operation_id),
            completed,
        )
        self.assertEqual(
            len(JournalStore(self.path).load_events("authority_state", "canonical")),
            1,
        )
        self.assertEqual(restarted.submit(command), accepted)

    def test_set_authority_cannot_cross_host_environment(self):
        store = self.store(now="2030-01-01T00:00:00Z")
        payload = {
            "policy_id": "cross-environment",
            "environments": ["PAPER", "LIVE"],
            "instruments": [
                {
                    "instrument_id": "11111111-1111-4111-8111-111111111111",
                    "version": 1,
                }
            ],
            "actions": ["ORDER.SUBMIT"],
            "max_notional": "250",
            "valid_from": "2030-01-01T00:00:00Z",
            "expires_at": "2035-01-01T00:00:00Z",
            "autonomous": False,
            "protection_only": False,
            "version": 1,
        }
        with self.assertRaisesRegex(ValueError, "exactly match"):
            store.submit(self.command(action="SET_AUTHORITY", payload=payload))
        self.assertEqual(store.state_version, 0)

    def test_unknown_payload_fields_never_enter_durable_operator_event(self):
        store = self.store(now="2030-01-01T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            store.submit(
                self.command(
                    payload={
                        "reason": "block",
                        "session_secret": "must-not-be-persisted",
                    }
                )
            )
        self.assertEqual(store.state_version, 0)
        self.assertEqual(store.events_after(0), ())

    def test_authority_change_after_acceptance_fails_closed(self):
        journal = JournalStore(self.path)
        authority = AuthorityService(journal)
        authority.register_policy(self.authority_policy("first-policy"))
        store = self.store(now="2030-01-01T00:00:00Z")
        accepted = store.submit(self.command())

        newer = AuthorityService(JournalStore(self.path))
        newer.register_policy(self.authority_policy("later-policy"))
        failed = store.execute_authority_operation(accepted.operation_id)
        self.assertEqual(failed.phase, "FAILED")
        self.assertEqual(
            failed.evidence[0]["reason_code"],
            "authority_state_changed",
        )
        restored = AuthorityService(JournalStore(self.path)).export_state()
        self.assertEqual(restored["revocations"], [])

    def test_authority_commit_then_host_restart_resumes_without_duplicate(self):
        journal = JournalStore(self.path)
        AuthorityService(journal).register_policy(
            self.authority_policy("exposure-policy")
        )
        store = self.store(now="2030-01-01T00:00:00Z")
        accepted = store.submit(self.command())
        event = store.events_after(0)[0]

        from mvp.autotrade_mvp.operator_authority_commands import (
            execute_operator_authority_action,
        )

        execute_operator_authority_action(
            journal,
            event.payload["action"],
            event.payload["action_payload"],
            event.payload["action_payload_hash"],
            event.payload["account_id"],
            event.payload["environment"],
            event.payload["started_at"],
        )
        authority_event_count = len(
            journal.load_events("authority_state", "canonical")
        )

        restarted = self.store(now="2030-01-01T00:00:01Z")
        completed = restarted.execute_authority_operation(accepted.operation_id)
        self.assertEqual(completed.phase, "SUCCEEDED")
        self.assertEqual(
            len(journal.load_events("authority_state", "canonical")),
            authority_event_count,
        )

    def test_success_cannot_be_fabricated_without_authority_evidence(self):
        store = self.store(now="2030-01-01T00:00:00Z")
        accepted = store.submit(self.command())
        with self.assertRaisesRegex(ValueError, "evidence"):
            store.update_operation(accepted.operation_id, "SUCCEEDED")
        self.assertEqual(
            store.get_operation(accepted.operation_id).phase,
            "QUEUED",
        )

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

    def test_true_concurrent_commit_fence_returns_state_conflict(self):
        first = self.store()
        original_commit = first._journal.commit_command
        raced = False

        def race_then_commit(*args, **kwargs):
            nonlocal raced
            if not raced:
                raced = True
                competing = self.store()
                winner = competing.submit(
                    self.command(
                        command_id="44444444-4444-4444-4444-444444444444",
                        key="key-race-winner",
                        version="0",
                        actor="bob",
                        session="session-b",
                    )
                )
                self.assertEqual(winner.status, "ACCEPTED")
            return original_commit(*args, **kwargs)

        with patch.object(
            first._journal,
            "commit_command",
            side_effect=race_then_commit,
        ):
            loser = first.submit(self.command())

        self.assertEqual(loser.status, "CONFLICT")
        self.assertEqual(loser.reason_codes, ("stale_state_version",))
        self.assertEqual(loser.state_version, "1")
        restarted = self.store()
        self.assertEqual(restarted.state_version, 1)
        self.assertEqual(len(restarted.events_after(0)), 1)

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
