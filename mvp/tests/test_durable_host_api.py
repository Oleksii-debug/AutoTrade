from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.authority import (
    AuthorityConflict,
    AuthorityPolicy,
    AuthorityService,
)
from mvp.autotrade_mvp.durable_host_api import JournalBackedHostCommandStore
from mvp.autotrade_mvp.host_api import EventGap, HostCommandStore
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


class JournalBackedHostApiTests(unittest.TestCase):
    def test_operator_policy_payload_keeps_exact_high_precision_notional(self):
        from mvp.autotrade_mvp.operator_authority_commands import (
            _policy_from_mapping,
            _policy_payload,
        )

        values = (
            "1000.000000000000000000000000000001",
            "1000.000000000000000000000000000002",
        )
        payloads = []
        for value in values:
            base = self.authority_policy("exact-notional")
            policy = AuthorityPolicy.create(
                policy_id=base.policy_id,
                account_id=base.account_id,
                environments=base.environments,
                instruments=base.instruments,
                actions=base.actions,
                max_notional=value,
                valid_from=base.valid_from,
                expires_at=base.expires_at,
                autonomous=base.autonomous,
                protection_only=base.protection_only,
                version=base.version,
            )
            payload = _policy_payload(policy)
            self.assertEqual(payload["max_notional"], value)
            self.assertEqual(_policy_from_mapping(payload).max_notional, policy.max_notional)
            payloads.append(payload)
        self.assertNotEqual(payload_digest(payloads[0]), payload_digest(payloads[1]))

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
    def authority_policy(
        policy_id,
        *,
        protection_only=False,
        environment="PAPER",
    ):
        return AuthorityPolicy.create(
            policy_id=policy_id,
            account_id="paper-account-1",
            environments={environment},
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
            "RESTORE_NEW_EXPOSURE",
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
        durable = self.store()
        self.assertEqual(memory.submit(command), durable.submit(command))

        padded_memory = HostCommandStore(
            account_id="  paper-account-1  ",
            environment="PAPER",
            session_validator=lambda session, actor, origin, action: (session, actor) in self.sessions,
            request_origin_provider=lambda: "https://local.autotrade.invalid",
            now=lambda: "2026-09-24T18:00:00Z",
        )
        padded_durable = self.store(account_id="  paper-account-1  ")
        self.assertEqual(padded_memory.account_id, "paper-account-1")
        self.assertEqual(padded_durable.account_id, "paper-account-1")
        self.assertEqual(
            padded_memory.submit(command),
            padded_durable.submit(command),
        )

    def test_whitespace_only_account_scope_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "account_id must be a non-empty string"):
            self.store(account_id="   ")

    def test_account_scope_is_canonicalized_before_aggregate_identity(self):
        canonical = self.store(account_id="paper-account")
        padded = self.store(account_id="  paper-account  ")

        self.assertEqual(padded.account_id, "paper-account")
        self.assertEqual(padded.aggregate_id, canonical.aggregate_id)
        result = padded.submit(self.command(account_id="paper-account"))
        self.assertEqual(result.state_version, "1")
        self.assertEqual(canonical.snapshot()["state_version"], "1")

    def test_two_account_scopes_share_one_journal_without_state_collision(self):
        first = self.store()
        first_result = first.submit(self.command())
        second = self.store(account_id="other-account")
        second_result = second.submit(
            self.command(
                command_id="22222222-2222-2222-2222-222222222222",
                key="other-account-key",
                account_id="other-account",
            )
        )

        self.assertEqual(first_result.state_version, "1")
        self.assertEqual(second_result.state_version, "1")
        self.assertNotEqual(first.aggregate_id, second.aggregate_id)
        self.assertEqual(first.snapshot()["state_version"], "1")
        self.assertEqual(second.snapshot()["state_version"], "1")
        with self.assertRaises(KeyError):
            second.get_operation(first_result.operation_id)
        with self.assertRaises(KeyError):
            first.get_operation(second_result.operation_id)

    def test_two_account_scopes_can_reuse_actor_and_idempotency_key(self):
        first = self.store()
        second = self.store(account_id="other-account")
        first_command = self.command(key="shared-account-key")
        second_command = self.command(
            key="shared-account-key",
            account_id="other-account",
        )

        first_result = first.submit(first_command)
        second_result = second.submit(second_command)

        self.assertEqual(first_command["command_id"], second_command["command_id"])
        self.assertEqual(first_result.status, "ACCEPTED")
        self.assertEqual(second_result.status, "ACCEPTED")
        self.assertEqual(first_result.state_version, "1")
        self.assertEqual(second_result.state_version, "1")
        self.assertNotEqual(first_result.operation_id, second_result.operation_id)
        self.assertEqual(first.submit(first_command), first_result)
        self.assertEqual(second.submit(second_command), second_result)

    def test_legacy_unscoped_host_journal_requires_explicit_migration(self):
        journal = JournalStore(self.path)
        payload = {"legacy": True}
        journal.append_event(
            {
                "event_id": "legacy-host-event",
                "event_type": "LEGACY_HOST_EVENT",
                "aggregate_type": "HOST_CONTROL",
                "aggregate_id": "host",
                "aggregate_version": "1",
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "committed_at": "2026-09-24T17:59:00Z",
            }
        )
        with self.assertRaisesRegex(ValueError, "legacy unscoped host journal"):
            self.store()

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
        first.submit(self.command(payload={"reason_code": "EMERGENCY_STOP"}))

        restarted = self.store()
        conflict = restarted.submit(self.command(payload={"reason_code": "POLICY_REVIEW"}))
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

    def test_legacy_accepted_authority_command_remains_visible_but_not_executable(self):
        journal = JournalStore(self.path)
        aggregate_id = self.store().aggregate_id
        payload = {
            "command_id": "11111111-1111-1111-1111-111111111111",
            "operation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "action": "BLOCK_NEW_EXPOSURE",
            "actor": "alice",
            "account_id": "paper-account-1",
            "environment": "PAPER",
            "phase": "QUEUED",
            "started_at": "2026-09-24T18:00:00Z",
            "updated_at": "2026-09-24T18:00:00Z",
            "affected_refs": [],
            "evidence": [],
            "remaining_uncertainty": ["financial_outcome_not_completed"],
        }
        journal.append_event(
            {
                "event_id": "legacy-accepted",
                "event_type": "COMMAND_ACCEPTED",
                "aggregate_type": JournalBackedHostCommandStore.AGGREGATE_TYPE,
                "aggregate_id": aggregate_id,
                "aggregate_version": "1",
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "committed_at": "2026-09-24T18:00:00Z",
            }
        )

        restarted = self.store()
        operation = restarted.get_operation(payload["operation_id"])
        self.assertEqual(operation.phase, "QUEUED")
        self.assertEqual(restarted.snapshot()["operations"][payload["operation_id"]], "QUEUED")
        with self.assertRaisesRegex(ValueError, "predates durable action payload"):
            restarted.execute_authority_operation(payload["operation_id"])
        self.assertEqual(restarted.state_version, 1)

    def test_legacy_unverifiable_success_is_projected_as_unknown(self):
        journal = JournalStore(self.path)
        aggregate_id = self.store().aggregate_id
        operation_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        accepted = {
            "command_id": "11111111-1111-1111-1111-111111111111",
            "operation_id": operation_id,
            "action": "BLOCK_NEW_EXPOSURE",
            "actor": "alice",
            "account_id": "paper-account-1",
            "environment": "PAPER",
            "phase": "QUEUED",
            "started_at": "2026-09-24T18:00:00Z",
            "updated_at": "2026-09-24T18:00:00Z",
            "affected_refs": [],
            "evidence": [],
            "remaining_uncertainty": ["financial_outcome_not_completed"],
        }
        succeeded = {
            "operation_id": operation_id,
            "phase": "SUCCEEDED",
            "started_at": "2026-09-24T18:00:00Z",
            "updated_at": "2026-09-24T18:00:01Z",
            "affected_refs": ["legacy:unverified"],
            "evidence": [{"kind": "legacy-unverified"}],
            "remaining_uncertainty": [],
        }
        for version, event_id, event_type, payload in (
            (1, "legacy-accepted", "COMMAND_ACCEPTED", accepted),
            (2, "legacy-succeeded", "OPERATION_UPDATED", succeeded),
        ):
            journal.append_event(
                {
                    "event_id": event_id,
                    "event_type": event_type,
                    "aggregate_type": JournalBackedHostCommandStore.AGGREGATE_TYPE,
                    "aggregate_id": aggregate_id,
                    "aggregate_version": str(version),
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": f"2026-09-24T18:00:0{version - 1}Z",
                }
            )

        restarted = self.store()
        operation = restarted.get_operation(operation_id)
        self.assertEqual(operation.phase, "UNKNOWN")
        self.assertEqual(
            operation.remaining_uncertainty,
            (JournalBackedHostCommandStore.LEGACY_AUTHORITY_UNCERTAINTY,),
        )
        events = restarted.events_after(0)
        self.assertEqual(events[1].payload["phase"], "UNKNOWN")
        self.assertEqual(
            events[1].payload["remaining_uncertainty"],
            [JournalBackedHostCommandStore.LEGACY_AUTHORITY_UNCERTAINTY],
        )
        self.assertEqual(journal.next_aggregate_version(
            JournalBackedHostCommandStore.AGGREGATE_TYPE,
            aggregate_id,
        ), 3)

    def test_partial_legacy_authority_payload_binding_is_corruption(self):
        journal = JournalStore(self.path)
        aggregate_id = self.store().aggregate_id
        payload = {
            "command_id": "11111111-1111-1111-1111-111111111111",
            "operation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "action": "BLOCK_NEW_EXPOSURE",
            "action_payload": {},
            "actor": "alice",
            "account_id": "paper-account-1",
            "environment": "PAPER",
            "phase": "QUEUED",
            "started_at": "2026-09-24T18:00:00Z",
            "updated_at": "2026-09-24T18:00:00Z",
            "affected_refs": [],
            "evidence": [],
            "remaining_uncertainty": ["financial_outcome_not_completed"],
        }
        journal.append_event(
            {
                "event_id": "partial-binding",
                "event_type": "COMMAND_ACCEPTED",
                "aggregate_type": JournalBackedHostCommandStore.AGGREGATE_TYPE,
                "aggregate_id": aggregate_id,
                "aggregate_version": "1",
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "committed_at": "2026-09-24T18:00:00Z",
            }
        )
        with self.assertRaisesRegex(ValueError, "binding is incomplete"):
            self.store().snapshot()

    def test_restart_rejects_action_payload_bound_to_different_command_identity(self):
        journal = JournalStore(self.path)
        aggregate_id = self.store().aggregate_id
        action_payload = {
            "schema_version": 1,
            "command_id": "22222222-2222-4222-8222-222222222222",
            "account_id": "paper-account-1",
            "environment": "PAPER",
            "expected_authority_epoch": "0",
            "expected_authority_version": "0",
            "reason_code": "OPERATOR_REQUEST",
            "target_policies": [],
        }
        payload = {
            "command_id": "11111111-1111-4111-8111-111111111111",
            "operation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
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
            "evidence": [],
            "remaining_uncertainty": ["financial_outcome_not_completed"],
        }
        journal.append_event(
            {
                "event_id": "mismatched-action-command",
                "event_type": "COMMAND_ACCEPTED",
                "aggregate_type": JournalBackedHostCommandStore.AGGREGATE_TYPE,
                "aggregate_id": aggregate_id,
                "aggregate_version": "1",
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "committed_at": "2026-09-24T18:00:00Z",
            }
        )
        with self.assertRaisesRegex(ValueError, "command identity"):
            self.store().snapshot()


    def test_restart_rejects_operation_update_without_accepted_origin(self):
        journal = JournalStore(self.path)
        aggregate_id = self.store().aggregate_id
        journal.append_event(
            {
                "event_id": "forged-update",
                "event_type": "OPERATION_UPDATED",
                "aggregate_type": JournalBackedHostCommandStore.AGGREGATE_TYPE,
                "aggregate_id": aggregate_id,
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
        aggregate_id = store.aggregate_id
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
                "aggregate_id": store.aggregate_id,
                "aggregate_version": str(
                    journal.next_aggregate_version(
                        JournalBackedHostCommandStore.AGGREGATE_TYPE,
                        aggregate_id,
                    )
                ),
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
                        "aggregate_id": store.aggregate_id,
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
            "command_id": "manual-command",
            "account_id": "paper-account-1",
            "environment": "PAPER",
            "expected_authority_epoch": "0",
            "expected_authority_version": "0",
            "reason_code": "OPERATOR_REQUEST",
            "target_policies": [],
        }
        aggregate_id = self.store().aggregate_id
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
                "aggregate_id": aggregate_id,
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

    def test_block_new_exposure_persists_scope_block_without_revoking_policies(self):
        journal = JournalStore(self.path)
        authority = AuthorityService(journal)
        authority.register_policy(self.authority_policy("exposure-policy"))
        authority.register_policy(
            self.authority_policy("protection-policy", protection_only=True)
        )
        store = self.store(now="2030-01-01T00:00:00Z")

        accepted = store.submit(
            self.command(payload={"reason_code": "EMERGENCY_STOP"})
        )
        event = store.events_after(0)[0]
        action_payload = event.payload["action_payload"]
        self.assertEqual(action_payload["target_policies"], [])
        self.assertNotIn("session", action_payload)
        self.assertNotIn("actor", action_payload)
        self.assertTrue(
            str(event.payload["action_payload_hash"]).startswith("sha256:")
        )

        completed = store.execute_authority_operation(accepted.operation_id)
        self.assertEqual(completed.phase, "SUCCEEDED")
        self.assertEqual(
            completed.affected_refs,
            ("authority-new-exposure-block:paper-account-1:PAPER",),
        )
        self.assertEqual(
            [item["event_type"] for item in completed.evidence],
            ["AuthorityNewExposureBlocked"],
        )

        restored_service = AuthorityService(JournalStore(self.path))
        self.assertTrue(
            restored_service.is_new_exposure_blocked(
                "paper-account-1",
                "PAPER",
            )
        )
        restored = restored_service.export_state()
        self.assertEqual(restored["revocations"], [])
        self.assertEqual(
            restored["new_exposure_blocks"],
            [
                {
                    "account_id": "paper-account-1",
                    "environment": "PAPER",
                    "command_id": "11111111-1111-1111-1111-111111111111",
                    "reason": "host_operator_command:BLOCK_NEW_EXPOSURE:EMERGENCY_STOP",
                    "blocked_at": "2030-01-01T00:00:00Z",
                }
            ],
        )
        self.assertEqual(
            self.store(now="2030-01-01T00:00:01Z")
            .get_operation(accepted.operation_id)
            .phase,
            "SUCCEEDED",
        )

    def test_block_restart_restore_preserves_existing_policy_identity(self):
        journal = JournalStore(self.path)
        original_policy = self.authority_policy(
            "restorable-policy",
            environment="SIMULATION",
        )
        AuthorityService(journal).register_policy(original_policy)

        host = self.store(
            environment="SIMULATION",
            now="2030-01-01T00:00:00Z",
        )
        blocked = host.submit(
            self.command(
                environment="SIMULATION",
                payload={"reason_code": "EMERGENCY_STOP"},
            )
        )
        self.assertEqual(
            host.execute_authority_operation(blocked.operation_id).phase,
            "SUCCEEDED",
        )

        after_block = AuthorityService(JournalStore(self.path))
        state = after_block.export_state()
        self.assertEqual(state["revocations"], [])
        self.assertTrue(
            after_block.is_new_exposure_blocked(
                "paper-account-1",
                "SIMULATION",
            )
        )
        denied = after_block._admit_unverified(
            admission_id="existing-policy-blocked",
            policy_id="restorable-policy",
            intent_hash="existing-policy-risk",
            account_id="paper-account-1",
            environment="SIMULATION",
            instrument_id="11111111-1111-4111-8111-111111111111",
            instrument_version=1,
            action="ORDER.SUBMIT",
            notional="10",
            state_version=1,
            risk_admitted=True,
            risk_reducing=False,
            now="2030-01-01T00:00:01Z",
        )
        self.assertEqual(denied.outcome, "REJECTED")
        self.assertEqual(denied.reason, "new_exposure_blocked")

        registered = next(
            item
            for item in state["policies"]
            if item["policy_id"] == "restorable-policy"
        )
        restore_payload = {
            key: value
            for key, value in registered.items()
            if key != "account_id"
        }
        restore_payload.update(
            {
                "restore_new_exposure": True,
                "reason_code": "POLICY_REVIEW",
            }
        )
        restarted_host = self.store(
            environment="SIMULATION",
            now="2030-01-01T00:00:02Z",
        )
        restore = restarted_host.submit(
            self.command(
                command_id="66666666-6666-4666-8666-666666666666",
                key="restore-existing-policy",
                version=restarted_host.snapshot()["state_version"],
                environment="SIMULATION",
                action="SET_AUTHORITY",
                payload=restore_payload,
            )
        )
        self.assertEqual(
            restarted_host.execute_authority_operation(restore.operation_id).phase,
            "SUCCEEDED",
        )

        restarted_authority = AuthorityService(JournalStore(self.path))
        restored_state = restarted_authority.export_state()
        self.assertEqual(restored_state["revocations"], [])
        self.assertFalse(
            restarted_authority.is_new_exposure_blocked(
                "paper-account-1",
                "SIMULATION",
            )
        )
        admitted = restarted_authority._admit_unverified(
            admission_id="existing-policy-restored",
            policy_id="restorable-policy",
            intent_hash="restored-existing-policy-risk",
            account_id="paper-account-1",
            environment="SIMULATION",
            instrument_id="11111111-1111-4111-8111-111111111111",
            instrument_version=1,
            action="ORDER.SUBMIT",
            notional="10",
            state_version=2,
            risk_admitted=True,
            risk_reducing=False,
            now="2030-01-01T00:00:03Z",
        )
        self.assertEqual(admitted.outcome, "ADMITTED")
        self.assertEqual(
            restarted_authority.dispatch_allowed(
                admitted.admission_id,
                intent_hash="restored-existing-policy-risk",
                account_id="paper-account-1",
                environment="SIMULATION",
                instrument_id="11111111-1111-4111-8111-111111111111",
                instrument_version=1,
                action="ORDER.SUBMIT",
                now="2030-01-01T00:00:04Z",
            ),
            (True, "allowed"),
        )

    def test_block_survives_restart_and_fresh_set_authority_cannot_reopen_risk(self):
        journal = JournalStore(self.path)
        AuthorityService(journal).register_policy(
            self.authority_policy(
                "protection-policy",
                protection_only=True,
                environment="SIMULATION",
            )
        )
        store = self.store(
            environment="SIMULATION",
            now="2030-01-01T00:00:00Z",
        )
        blocked_command = self.command(
            environment="SIMULATION",
            payload={"reason_code": "EMERGENCY_STOP"},
        )
        accepted = store.submit(blocked_command)
        blocked = store.execute_authority_operation(accepted.operation_id)
        self.assertEqual(blocked.phase, "SUCCEEDED")
        self.assertEqual(
            blocked.affected_refs,
            (
                "authority-new-exposure-block:paper-account-1:SIMULATION",
            ),
        )

        restarted_host = self.store(
            environment="SIMULATION",
            now="2030-01-01T00:00:01Z",
        )
        fresh_policy_payload = {
            "policy_id": "fresh-exposure-policy",
            "environments": ["SIMULATION"],
            "instruments": [
                {
                    "instrument_id": "11111111-1111-4111-8111-111111111111",
                    "version": 1,
                }
            ],
            "actions": ["ORDER.SUBMIT"],
            "max_notional": "1000",
            "valid_from": "2029-01-01T00:00:00Z",
            "expires_at": "2035-01-01T00:00:00Z",
            "autonomous": True,
            "protection_only": False,
            "version": 1,
        }
        set_command = self.command(
            command_id="22222222-2222-2222-2222-222222222222",
            key="set-after-block",
            version=restarted_host.snapshot()["state_version"],
            environment="SIMULATION",
            action="SET_AUTHORITY",
            payload=fresh_policy_payload,
        )
        set_accepted = restarted_host.submit(set_command)
        self.assertEqual(set_accepted.status, "ACCEPTED")
        set_completed = restarted_host.execute_authority_operation(
            set_accepted.operation_id
        )
        self.assertEqual(set_completed.phase, "SUCCEEDED")

        restored = AuthorityService(JournalStore(self.path))
        self.assertTrue(
            restored.is_new_exposure_blocked(
                "paper-account-1",
                "SIMULATION",
            )
        )
        rejected = restored._admit_unverified(
            admission_id="blocked-new-risk",
            policy_id="fresh-exposure-policy",
            intent_hash="fresh-risk-intent",
            account_id="paper-account-1",
            environment="SIMULATION",
            instrument_id="11111111-1111-4111-8111-111111111111",
            instrument_version=1,
            action="ORDER.SUBMIT",
            notional="10",
            state_version=1,
            risk_admitted=True,
            risk_reducing=False,
            now="2030-01-01T00:00:02Z",
        )
        self.assertEqual(rejected.outcome, "REJECTED")
        self.assertEqual(rejected.reason, "new_exposure_blocked")

        protective = restored._admit_unverified(
            admission_id="protective-risk-reduction",
            policy_id="protection-policy",
            intent_hash="protective-intent",
            account_id="paper-account-1",
            environment="SIMULATION",
            instrument_id="11111111-1111-4111-8111-111111111111",
            instrument_version=1,
            action="ORDER.SUBMIT",
            notional="10",
            state_version=2,
            risk_admitted=True,
            risk_reducing=True,
            now="2030-01-01T00:00:03Z",
        )
        self.assertEqual(protective.outcome, "ADMITTED")
        self.assertEqual(
            restored.dispatch_allowed(
                protective.admission_id,
                intent_hash="protective-intent",
                account_id="paper-account-1",
                environment="SIMULATION",
                instrument_id="11111111-1111-4111-8111-111111111111",
                instrument_version=1,
                action="ORDER.SUBMIT",
                now="2030-01-01T00:00:04Z",
            ),
            (True, "allowed"),
        )


        authority_event_count_before_restore = len(
            JournalStore(self.path).load_events("authority_state", "canonical")
        )
        restore_payload = dict(fresh_policy_payload)
        restore_payload.update(
            {
                "restore_new_exposure": True,
                "reason_code": "POLICY_REVIEW",
            }
        )
        restore_command = self.command(
            command_id="33333333-3333-3333-3333-333333333333",
            key="restore-after-reconciliation",
            version=restarted_host.snapshot()["state_version"],
            environment="SIMULATION",
            action="SET_AUTHORITY",
            payload=restore_payload,
        )
        restore_accepted = restarted_host.submit(restore_command)
        self.assertEqual(restore_accepted.status, "ACCEPTED")
        restore_event = next(
            item
            for item in restarted_host.events_after(0)
            if item.payload.get("operation_id") == restore_accepted.operation_id
            and item.kind == "COMMAND_ACCEPTED"
        )
        self.assertTrue(
            restore_event.payload["action_payload"]["restore_new_exposure"]
        )
        self.assertEqual(
            restore_event.payload["action_payload"]["reason_code"],
            "POLICY_REVIEW",
        )
        self.assertEqual(
            restore_event.payload["action_payload"]["expected_block"],
            {
                "command_id": "11111111-1111-1111-1111-111111111111",
                "reason": (
                    "host_operator_command:"
                    "BLOCK_NEW_EXPOSURE:EMERGENCY_STOP"
                ),
                "blocked_at": "2030-01-01T00:00:00Z",
            },
        )

        restore_completed = restarted_host.execute_authority_operation(
            restore_accepted.operation_id
        )
        self.assertEqual(restore_completed.phase, "SUCCEEDED")
        self.assertEqual(
            restore_completed.affected_refs,
            (
                "authority-policy:fresh-exposure-policy",
                "authority-new-exposure-block:"
                "paper-account-1:SIMULATION",
            ),
        )
        self.assertEqual(
            [item["event_type"] for item in restore_completed.evidence],
            ["AuthorityPolicyRegistered", "AuthorityNewExposureRestored"],
        )

        after_restore = AuthorityService(JournalStore(self.path))
        self.assertFalse(
            after_restore.is_new_exposure_blocked(
                "paper-account-1",
                "SIMULATION",
            )
        )
        admitted = after_restore._admit_unverified(
            admission_id="restored-new-risk",
            policy_id="fresh-exposure-policy",
            intent_hash="restored-risk-intent",
            account_id="paper-account-1",
            environment="SIMULATION",
            instrument_id="11111111-1111-4111-8111-111111111111",
            instrument_version=1,
            action="ORDER.SUBMIT",
            notional="10",
            state_version=3,
            risk_admitted=True,
            risk_reducing=False,
            now="2030-01-01T00:00:05Z",
        )
        self.assertEqual(admitted.outcome, "ADMITTED")
        self.assertEqual(
            after_restore.dispatch_allowed(
                admitted.admission_id,
                intent_hash="restored-risk-intent",
                account_id="paper-account-1",
                environment="SIMULATION",
                instrument_id="11111111-1111-4111-8111-111111111111",
                instrument_version=1,
                action="ORDER.SUBMIT",
                now="2030-01-01T00:00:06Z",
            ),
            (True, "allowed"),
        )

        restarted_after_restore = self.store(
            environment="SIMULATION",
            now="2030-01-01T00:00:07Z",
        )
        self.assertFalse(
            AuthorityService(JournalStore(self.path)).is_new_exposure_blocked(
                "paper-account-1",
                "SIMULATION",
            )
        )
        replayed_restore = restarted_after_restore.execute_authority_operation(
            restore_accepted.operation_id
        )
        self.assertEqual(replayed_restore.phase, "SUCCEEDED")
        self.assertEqual(
            len(
                JournalStore(self.path).load_events(
                    "authority_state",
                    "canonical",
                )
            ),
            authority_event_count_before_restore + 2,
        )


    def test_accepted_set_restore_cannot_clear_newer_block_generation(self):
        host = self.store(
            environment="SIMULATION",
            now="2030-01-01T00:00:00Z",
        )
        blocked = host.submit(
            self.command(
                environment="SIMULATION",
                payload={"reason_code": "EMERGENCY_STOP"},
            )
        )
        self.assertEqual(
            host.execute_authority_operation(blocked.operation_id).phase,
            "SUCCEEDED",
        )

        policy_payload = {
            "policy_id": "stale-restore-policy",
            "environments": ["SIMULATION"],
            "instruments": [
                {
                    "instrument_id": "11111111-1111-4111-8111-111111111111",
                    "version": 1,
                }
            ],
            "actions": ["ORDER.SUBMIT"],
            "max_notional": "1000",
            "valid_from": "2029-01-01T00:00:00Z",
            "expires_at": "2035-01-01T00:00:00Z",
            "autonomous": True,
            "protection_only": False,
            "version": 1,
        }
        set_host = self.store(
            environment="SIMULATION",
            now="2030-01-01T00:00:01Z",
        )
        registered = set_host.submit(
            self.command(
                command_id="22222222-2222-2222-2222-222222222222",
                key="register-before-stale-restore",
                version=set_host.snapshot()["state_version"],
                environment="SIMULATION",
                action="SET_AUTHORITY",
                payload=policy_payload,
            )
        )
        self.assertEqual(
            set_host.execute_authority_operation(registered.operation_id).phase,
            "SUCCEEDED",
        )

        restore_host = self.store(
            environment="SIMULATION",
            now="2030-01-01T00:00:02Z",
        )
        restore_payload = {
            **policy_payload,
            "restore_new_exposure": True,
            "reason_code": "POLICY_REVIEW",
        }
        accepted = restore_host.submit(
            self.command(
                command_id="33333333-3333-3333-3333-333333333333",
                key="stale-explicit-restore",
                version=restore_host.snapshot()["state_version"],
                environment="SIMULATION",
                action="SET_AUTHORITY",
                payload=restore_payload,
            )
        )
        accepted_event = next(
            item
            for item in restore_host.events_after(0)
            if item.payload.get("operation_id") == accepted.operation_id
            and item.kind == "COMMAND_ACCEPTED"
        )
        captured = accepted_event.payload["action_payload"]["expected_block"]

        authority = AuthorityService(JournalStore(self.path))
        authority.restore_new_exposure(
            account_id="paper-account-1",
            environment="SIMULATION",
            reason="test-reconciliation-completed",
            restored_at="2030-01-01T00:00:03Z",
            command_id="44444444-4444-4444-4444-444444444444",
            expected_block_command_id=captured["command_id"],
            expected_block_reason=captured["reason"],
            expected_blocked_at=captured["blocked_at"],
        )
        authority.block_new_exposure(
            account_id="paper-account-1",
            environment="SIMULATION",
            reason="newer-emergency-generation",
            blocked_at="2030-01-01T00:00:04Z",
            command_id="55555555-5555-4555-8555-555555555555",
        )

        failed = restore_host.execute_authority_operation(accepted.operation_id)
        self.assertEqual(failed.phase, "FAILED")
        self.assertEqual(failed.affected_refs, ())
        replayed = AuthorityService(JournalStore(self.path))
        self.assertTrue(
            replayed.is_new_exposure_blocked(
                "paper-account-1",
                "SIMULATION",
            )
        )
        self.assertEqual(
            replayed.export_state()["new_exposure_blocks"][0]["command_id"],
            "55555555-5555-4555-8555-555555555555",
        )

    def test_stale_restore_cannot_clear_newer_new_exposure_block(self):
        journal = JournalStore(self.path)
        service = AuthorityService(journal)
        service.block_new_exposure(
            account_id="paper-account-1",
            environment="PAPER",
            reason="host_operator_command:BLOCK_NEW_EXPOSURE:EMERGENCY_STOP",
            blocked_at="2030-01-01T00:00:00Z",
            command_id="11111111-1111-1111-1111-111111111111",
        )
        stale_block = dict(service.export_state()["new_exposure_blocks"][0])
        service.restore_new_exposure(
            account_id="paper-account-1",
            environment="PAPER",
            reason="host_operator_command:RESTORE_NEW_EXPOSURE:POLICY_REVIEW",
            restored_at="2030-01-01T00:00:01Z",
            command_id="22222222-2222-2222-2222-222222222222",
            expected_block_command_id=stale_block["command_id"],
            expected_block_reason=stale_block["reason"],
            expected_blocked_at=stale_block["blocked_at"],
        )
        service.block_new_exposure(
            account_id="paper-account-1",
            environment="PAPER",
            reason="host_operator_command:BLOCK_NEW_EXPOSURE:EMERGENCY_STOP",
            blocked_at="2030-01-01T00:00:02Z",
            command_id="33333333-3333-3333-3333-333333333333",
        )

        with self.assertRaisesRegex(
            AuthorityConflict,
            "does not match active block",
        ):
            service.restore_new_exposure(
                account_id="paper-account-1",
                environment="PAPER",
                reason=(
                    "host_operator_command:"
                    "RESTORE_NEW_EXPOSURE:POLICY_REVIEW"
                ),
                restored_at="2030-01-01T00:00:03Z",
                command_id="44444444-4444-4444-4444-444444444444",
                expected_block_command_id=stale_block["command_id"],
                expected_block_reason=stale_block["reason"],
                expected_blocked_at=stale_block["blocked_at"],
            )

        restarted = AuthorityService(JournalStore(self.path))
        self.assertTrue(
            restarted.is_new_exposure_blocked(
                "paper-account-1",
                "PAPER",
            )
        )
        self.assertEqual(
            restarted.export_state()["new_exposure_blocks"][0]["command_id"],
            "33333333-3333-3333-3333-333333333333",
        )


    def test_block_event_identity_is_scoped_for_same_external_command_id(self):
        journal = JournalStore(self.path)
        authority = AuthorityService(journal)
        shared_command = "11111111-1111-1111-1111-111111111111"
        authority.block_new_exposure(
            account_id="paper-account-1",
            environment="PAPER",
            reason="host_operator_command:BLOCK_NEW_EXPOSURE:EMERGENCY_STOP",
            blocked_at="2030-01-01T00:00:00Z",
            command_id=shared_command,
        )
        authority.block_new_exposure(
            account_id="paper-account-2",
            environment="PAPER",
            reason="host_operator_command:BLOCK_NEW_EXPOSURE:EMERGENCY_STOP",
            blocked_at="2030-01-01T00:00:01Z",
            command_id=shared_command,
        )

        events = journal.load_events("authority_state", "canonical")
        self.assertEqual(
            [item["event_type"] for item in events],
            ["AuthorityNewExposureBlocked", "AuthorityNewExposureBlocked"],
        )
        self.assertNotEqual(events[0]["event_id"], events[1]["event_id"])

        restored = AuthorityService(JournalStore(self.path))
        self.assertTrue(
            restored.is_new_exposure_blocked("paper-account-1", "PAPER")
        )
        self.assertTrue(
            restored.is_new_exposure_blocked("paper-account-2", "PAPER")
        )


    def test_repeated_block_reuses_existing_scope_fact_without_corrupting_replay(self):
        first = self.store(now="2030-01-01T00:00:00Z")
        first_accepted = first.submit(
            self.command(payload={"reason_code": "EMERGENCY_STOP"})
        )
        first_done = first.execute_authority_operation(first_accepted.operation_id)
        self.assertEqual(first_done.phase, "SUCCEEDED")
        authority_events_before = JournalStore(self.path).load_events(
            "authority_state",
            "canonical",
        )
        self.assertEqual(
            [item["event_type"] for item in authority_events_before],
            ["AuthorityNewExposureBlocked"],
        )

        second = self.store(now="2030-01-01T00:00:01Z")
        second_command = self.command(
            command_id="33333333-3333-3333-3333-333333333333",
            key="repeat-block",
            version=second.snapshot()["state_version"],
            payload={"reason_code": "EMERGENCY_STOP"},
        )
        second_accepted = second.submit(second_command)
        self.assertEqual(second_accepted.status, "ACCEPTED")
        second_done = second.execute_authority_operation(
            second_accepted.operation_id
        )
        self.assertEqual(second_done.phase, "SUCCEEDED")
        self.assertEqual(
            second_done.affected_refs,
            ("authority-new-exposure-block:paper-account-1:PAPER",),
        )

        authority_events_after = JournalStore(self.path).load_events(
            "authority_state",
            "canonical",
        )
        self.assertEqual(authority_events_after, authority_events_before)
        restarted_authority = AuthorityService(JournalStore(self.path))
        self.assertTrue(
            restarted_authority.is_new_exposure_blocked(
                "paper-account-1",
                "PAPER",
            )
        )


    def test_repeated_block_reuses_scope_fact_without_revoking_new_policy(self):
        first = self.store(now="2030-01-01T00:00:00Z")
        first_accepted = first.submit(
            self.command(payload={"reason_code": "EMERGENCY_STOP"})
        )
        first_done = first.execute_authority_operation(first_accepted.operation_id)
        self.assertEqual(first_done.phase, "SUCCEEDED")

        set_host = self.store(now="2030-01-01T00:00:01Z")
        policy_payload = {
            "policy_id": "post-block-policy",
            "environments": ["PAPER"],
            "instruments": [
                {
                    "instrument_id": "11111111-1111-4111-8111-111111111111",
                    "version": 1,
                }
            ],
            "actions": ["ORDER.SUBMIT"],
            "max_notional": "1000",
            "valid_from": "2029-01-01T00:00:00Z",
            "expires_at": "2035-01-01T00:00:00Z",
            "autonomous": True,
            "protection_only": False,
            "version": 1,
        }
        set_accepted = set_host.submit(
            self.command(
                command_id="44444444-4444-4444-8444-444444444444",
                key="post-block-set",
                version=set_host.snapshot()["state_version"],
                action="SET_AUTHORITY",
                payload=policy_payload,
            )
        )
        self.assertEqual(
            set_host.execute_authority_operation(set_accepted.operation_id).phase,
            "SUCCEEDED",
        )

        second = self.store(now="2030-01-01T00:00:02Z")
        second_accepted = second.submit(
            self.command(
                command_id="55555555-5555-4555-8555-555555555555",
                key="repeat-block-with-new-policy",
                version=second.snapshot()["state_version"],
                payload={"reason_code": "EMERGENCY_STOP"},
            )
        )
        second_event = second.events_after(0)[-1]
        self.assertEqual(
            second_event.payload["action_payload"]["target_policies"],
            [],
        )
        second_done = second.execute_authority_operation(
            second_accepted.operation_id
        )
        self.assertEqual(second_done.phase, "SUCCEEDED")
        self.assertEqual(
            second_done.affected_refs,
            ("authority-new-exposure-block:paper-account-1:PAPER",),
        )

        authority_events = JournalStore(self.path).load_events(
            "authority_state",
            "canonical",
        )
        self.assertEqual(
            sum(
                item["event_type"] == "AuthorityNewExposureBlocked"
                for item in authority_events
            ),
            1,
        )
        restored = AuthorityService(JournalStore(self.path)).export_state()
        self.assertEqual(restored["revocations"], [])
        self.assertEqual(len(restored["new_exposure_blocks"]), 1)


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

    def test_set_authority_restore_mode_is_explicit_and_preexisting_only(self):
        store = self.store(now="2030-01-01T00:00:00Z")
        policy_payload = {
            "policy_id": "restore-mode-policy",
            "environments": ["PAPER"],
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
        cases = (
            (
                {"reason_code": "POLICY_REVIEW"},
                "requires restore_new_exposure=true",
            ),
            (
                {
                    "restore_new_exposure": False,
                    "reason_code": "POLICY_REVIEW",
                },
                "restore_new_exposure must be true",
            ),
            (
                {
                    "restore_new_exposure": True,
                    "reason_code": "POLICY_REVIEW",
                },
                "exact policy to be already registered",
            ),
        )
        for extension, expected in cases:
            with self.subTest(extension=extension), self.assertRaisesRegex(
                ValueError,
                expected,
            ):
                store.submit(
                    self.command(
                        action="SET_AUTHORITY",
                        payload={**policy_payload, **extension},
                    )
                )
            self.assertEqual(store.state_version, 0)
            self.assertEqual(store.events_after(0), ())

    def test_set_authority_cannot_reactivate_revoked_policy_identity(self):
        journal = JournalStore(self.path)
        authority = AuthorityService(journal)
        policy = self.authority_policy("revoked-policy")
        authority.register_policy(policy)
        authority.revoke_policy(
            policy.policy_id,
            reason="test-revocation",
            revoked_at="2029-12-31T00:00:00Z",
        )
        store = self.store(now="2030-01-01T00:00:00Z")
        exported = authority.export_state()["policies"][0]
        payload = {
            key: value
            for key, value in exported.items()
            if key != "account_id"
        }
        with self.assertRaisesRegex(
            ValueError,
            "cannot be reactivated",
        ):
            store.submit(self.command(action="SET_AUTHORITY", payload=payload))
        self.assertEqual(store.state_version, 0)

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

    def test_free_text_reason_is_rejected_before_journal_write(self):
        store = self.store(now="2030-01-01T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            store.submit(self.command(payload={"reason": "token-or-free-text"}))
        self.assertEqual(store.events_after(0), ())

    def test_reason_code_must_be_canonical(self):
        store = self.store(now="2030-01-01T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "canonical operator reason"):
            store.submit(self.command(payload={"reason_code": "emergency_stop"}))
        self.assertEqual(store.events_after(0), ())

    def test_unknown_payload_fields_never_enter_durable_operator_event(self):
        store = self.store(now="2030-01-01T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "unsupported fields"):
            store.submit(
                self.command(
                    payload={
                        "reason_code": "EMERGENCY_STOP",
                        "session_secret": "must-not-be-persisted",
                    }
                )
            )
        self.assertEqual(store.state_version, 0)
        self.assertEqual(store.events_after(0), ())

    def test_empty_block_target_cannot_ignore_later_authority_grant(self):
        store = self.store(now="2030-01-01T00:00:00Z")
        accepted = store.submit(self.command())
        accepted_event = store.events_after(0)[0]
        self.assertEqual(
            accepted_event.payload["action_payload"]["target_policies"],
            [],
        )

        AuthorityService(JournalStore(self.path)).register_policy(
            self.authority_policy("later-policy")
        )
        failed = store.execute_authority_operation(accepted.operation_id)
        self.assertEqual(failed.phase, "FAILED")
        restored = AuthorityService(JournalStore(self.path)).export_state()
        self.assertEqual(restored["revocations"], [])

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

    def test_restart_resumes_after_scope_block_commit_without_duplicate(self):
        journal = JournalStore(self.path)
        AuthorityService(journal).register_policy(
            self.authority_policy("exposure-policy")
        )
        store = self.store(now="2030-01-01T00:00:00Z")
        accepted = store.submit(
            self.command(payload={"reason_code": "EMERGENCY_STOP"})
        )
        event = store.events_after(0)[0]
        action_payload = event.payload["action_payload"]
        reason = "host_operator_command:BLOCK_NEW_EXPOSURE:EMERGENCY_STOP"

        AuthorityService(JournalStore(self.path)).block_new_exposure(
            account_id=event.payload["account_id"],
            environment=event.payload["environment"],
            reason=reason,
            blocked_at=event.payload["started_at"],
            command_id=event.payload["command_id"],
        )
        before = JournalStore(self.path).load_events(
            "authority_state",
            "canonical",
        )
        self.assertEqual(
            [item["event_type"] for item in before],
            ["AuthorityPolicyRegistered", "AuthorityNewExposureBlocked"],
        )

        restarted = self.store(now="2030-01-01T00:00:01Z")
        completed = restarted.execute_authority_operation(accepted.operation_id)
        self.assertEqual(completed.phase, "SUCCEEDED")
        self.assertEqual(
            completed.affected_refs,
            ("authority-new-exposure-block:paper-account-1:PAPER",),
        )
        after = JournalStore(self.path).load_events(
            "authority_state",
            "canonical",
        )
        self.assertEqual(
            [item["event_type"] for item in after],
            [
                "AuthorityPolicyRegistered",
                "AuthorityNewExposureBlocked",
            ],
        )
        block_events = [
            item
            for item in after
            if item["event_type"] == "AuthorityNewExposureBlocked"
        ]
        self.assertEqual(len(block_events), 1)
        self.assertEqual(
            block_events[0]["payload"]["command_id"],
            action_payload["command_id"],
        )


    def test_success_cannot_forge_affected_refs_with_real_authority_evidence(self):
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

        result = execute_operator_authority_action(
            journal,
            event.payload["action"],
            event.payload["action_payload"],
            event.payload["action_payload_hash"],
            event.payload["account_id"],
            event.payload["environment"],
            event.payload["started_at"],
        )
        with self.assertRaisesRegex(ValueError, "affected_refs"):
            store.update_operation(
                accepted.operation_id,
                "SUCCEEDED",
                affected_refs=("authority-policy:forged",),
                evidence=result.evidence,
            )
        self.assertEqual(store.get_operation(accepted.operation_id).phase, "QUEUED")
        self.assertEqual(
            store.execute_authority_operation(accepted.operation_id).phase,
            "SUCCEEDED",
        )

    def test_success_cannot_be_fabricated_without_authority_evidence(self):
        store = self.store(now="2030-01-01T00:00:00Z")
        accepted = store.submit(self.command())
        with self.assertRaisesRegex(
            ValueError,
            "terminal authority operation has no canonical outcome",
        ):
            store.update_operation(accepted.operation_id, "SUCCEEDED")
        self.assertEqual(
            store.get_operation(accepted.operation_id).phase,
            "QUEUED",
        )

    def test_partial_authority_commit_is_reported_when_later_state_change_conflicts(self):
        journal = JournalStore(self.path)
        authority = AuthorityService(journal)
        authority.register_policy(self.authority_policy("a-policy"))
        authority.register_policy(self.authority_policy("b-policy"))
        store = self.store(now="2030-01-01T00:00:00Z")
        accepted = store.submit(self.command(action="REVOKE_AUTHORITY"))
        accepted_event = store.events_after(0)[0]
        accepted_at = accepted_event.payload["started_at"]

        AuthorityService(JournalStore(self.path)).revoke_policy(
            "a-policy",
            reason="host_operator_command:REVOKE_AUTHORITY:OPERATOR_REQUEST",
            revoked_at=accepted_at,
        )
        AuthorityService(JournalStore(self.path)).register_policy(
            self.authority_policy("outside-policy")
        )

        failed = store.execute_authority_operation(accepted.operation_id)
        self.assertEqual(failed.phase, "FAILED")
        self.assertEqual(
            failed.affected_refs,
            ("authority-policy:a-policy",),
        )
        self.assertEqual(
            failed.evidence[0]["event_type"],
            "AuthorityPolicyRevoked",
        )
        self.assertEqual(
            failed.evidence[-1]["reason_code"],
            "authority_state_changed",
        )

        restored = AuthorityService(JournalStore(self.path)).export_state()
        self.assertEqual(
            {item["policy_id"] for item in restored["revocations"]},
            {"a-policy"},
        )
        self.assertEqual(
            self.store(now="2030-01-01T00:00:01Z")
            .get_operation(accepted.operation_id)
            .affected_refs,
            ("authority-policy:a-policy",),
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
