from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory

from mvp.autotrade_mvp.authority import AuthorityPolicy, AuthorityService
from mvp.autotrade_mvp.authority_command_executor import AuthorityCommandExecutor
from mvp.autotrade_mvp.durable_host_api import JournalBackedHostCommandStore
from mvp.autotrade_mvp.persistence import JournalStore

INSTRUMENT_ID = "11111111-2222-4333-8444-555555555555"


class AuthorityCommandExecutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = f"{self.directory.name}/journal.sqlite3"
        self.journal = JournalStore(self.path)
        self.host = JournalBackedHostCommandStore(
            self.journal,
            account_id="paper-account-1",
            environment="PAPER",
            session_validator=lambda session, actor, origin, action: (
                session == "session-owner"
                and actor == "owner"
                and origin == "https://local.autotrade.invalid"
            ),
            request_origin_provider=lambda: "https://local.autotrade.invalid",
            now=lambda: "2026-09-26T00:30:00Z",
        )
        self.authority = AuthorityService(self.journal)
        self.executor = AuthorityCommandExecutor(self.host, self.authority)

    def command(self, *, action, payload, version=None, suffix="1"):
        return {
            "command_id": f"00000000-0000-4000-8000-{suffix.zfill(12)}",
            "expected_state_version": (
                str(self.host.state_version) if version is None else version
            ),
            "idempotency_key": f"key-{suffix}",
            "actor": "owner",
            "session": "session-owner",
            "account_id": "paper-account-1",
            "environment": "PAPER",
            "action": action,
            "payload": payload,
        }

    def policy_payload(self, policy_id="policy-1"):
        return {
            "policy_id": policy_id,
            "account_id": "paper-account-1",
            "environments": ["PAPER"],
            "instruments": [
                {"instrument_id": INSTRUMENT_ID, "version": 1},
            ],
            "actions": ["ORDER.SUBMIT", "ORDER.CANCEL"],
            "max_notional": "1000",
            "expires_at": "2026-09-27T00:00:00Z",
            "autonomous": True,
            "valid_from": "2026-09-25T00:00:00Z",
            "protection_only": False,
            "version": 1,
        }

    def test_block_payload_is_normalized_and_survives_restart(self):
        result = self.host.submit(
            self.command(
                action="BLOCK_NEW_EXPOSURE",
                payload={},
            )
        )
        accepted = self.host.get_accepted_command(result.operation_id)
        self.assertEqual(
            accepted["action_payload"],
            {"reason": "operator_requested_block_new_exposure"},
        )
        restarted = JournalBackedHostCommandStore(
            JournalStore(self.path),
            account_id="paper-account-1",
            environment="PAPER",
            session_validator=lambda *_args: True,
            request_origin_provider=lambda: "https://local.autotrade.invalid",
            now=lambda: "2026-09-26T00:31:00Z",
        )
        self.assertEqual(
            restarted.get_accepted_command(result.operation_id)["action_payload"],
            accepted["action_payload"],
        )

    def test_revoke_requires_policy_id_before_durable_mutation(self):
        with self.assertRaisesRegex(ValueError, "policy_id"):
            self.host.submit(
                self.command(
                    action="REVOKE_AUTHORITY",
                    payload={},
                )
            )
        self.assertEqual(self.host.state_version, 0)

    def test_set_authority_executes_and_terminal_evidence_binds_event(self):
        accepted = self.host.submit(
            self.command(
                action="SET_AUTHORITY",
                payload={"policy": self.policy_payload()},
            )
        )
        completed = self.executor.execute(accepted.operation_id)
        self.assertEqual(completed.phase, "SUCCEEDED")
        self.assertEqual(
            completed.affected_refs,
            ("authority-policy:policy-1",),
        )
        self.assertEqual(len(completed.evidence), 1)
        policy = self.authority.policy("policy-1")
        self.assertEqual(policy.account_id, "paper-account-1")
        event = self.journal.load_events("authority_state", "canonical")[0]
        self.assertEqual(
            completed.evidence[0]["artifact_id"],
            event["event_id"],
        )
        self.assertEqual(
            completed.evidence[0]["sha256"],
            event["payload_hash"],
        )

    def test_block_new_exposure_blocks_new_risk_but_not_risk_reducing(self):
        journal = JournalStore(f"{self.directory.name}/simulation.sqlite3")
        host = JournalBackedHostCommandStore(
            journal,
            account_id="paper-account-1",
            environment="SIMULATION",
            session_validator=lambda *_args: True,
            request_origin_provider=lambda: "https://local.autotrade.invalid",
            now=lambda: "2026-09-26T00:30:00Z",
        )
        authority = AuthorityService(journal)
        executor = AuthorityCommandExecutor(host, authority)
        authority.register_policy(
            AuthorityPolicy.create(
                policy_id="policy-1",
                account_id="paper-account-1",
                environments={"SIMULATION"},
                instruments=[(INSTRUMENT_ID, 1)],
                actions={"ORDER.SUBMIT", "ORDER.CANCEL"},
                max_notional="1000",
                expires_at="2026-09-27T00:00:00Z",
                autonomous=True,
                valid_from="2026-09-25T00:00:00Z",
                protection_only=False,
            )
        )
        command = self.command(
            action="BLOCK_NEW_EXPOSURE",
            payload={"reason": "operator emergency stop"},
            suffix="2",
        )
        command["environment"] = "SIMULATION"
        accepted = host.submit(command)
        completed = executor.execute(accepted.operation_id)
        self.assertEqual(completed.phase, "SUCCEEDED")
        self.assertTrue(
            authority.is_new_exposure_blocked(
                "paper-account-1",
                "SIMULATION",
            )
        )
        new_risk = authority._admit_unverified(
            admission_id="admission-new-risk",
            policy_id="policy-1",
            intent_hash="intent-hash-1",
            account_id="paper-account-1",
            environment="SIMULATION",
            instrument_id=INSTRUMENT_ID,
            instrument_version=1,
            action="ORDER.SUBMIT",
            notional="10",
            state_version=1,
            risk_admitted=True,
            now="2026-09-26T00:31:00Z",
            risk_reducing=False,
        )
        self.assertEqual(new_risk.outcome, "REJECTED")
        self.assertEqual(new_risk.reason, "new_exposure_blocked")

        reducing = authority._admit_unverified(
            admission_id="admission-reducing",
            policy_id="policy-1",
            intent_hash="intent-hash-2",
            account_id="paper-account-1",
            environment="SIMULATION",
            instrument_id=INSTRUMENT_ID,
            instrument_version=1,
            action="ORDER.CANCEL",
            notional="0",
            state_version=1,
            risk_admitted=True,
            now="2026-09-26T00:31:00Z",
            risk_reducing=True,
        )
        self.assertEqual(reducing.outcome, "ADMITTED")

    def test_block_survives_restart_and_unrelated_new_policy_registration(self):
        self.authority.register_policy(
            AuthorityPolicy.create(
                policy_id="policy-1",
                account_id="paper-account-1",
                environments={"PAPER"},
                instruments=[(INSTRUMENT_ID, 1)],
                actions={"ORDER.SUBMIT"},
                max_notional="1000",
                expires_at="2026-09-27T00:00:00Z",
                autonomous=True,
                valid_from="2026-09-25T00:00:00Z",
                protection_only=False,
            )
        )
        accepted = self.host.submit(
            self.command(
                action="BLOCK_NEW_EXPOSURE",
                payload={},
                suffix="3",
            )
        )
        self.executor.execute(accepted.operation_id)

        restarted = AuthorityService(JournalStore(self.path))
        self.assertTrue(
            restarted.is_new_exposure_blocked("paper-account-1", "PAPER")
        )
        restarted.register_policy(
            AuthorityPolicy.create(
                policy_id="policy-2",
                account_id="paper-account-1",
                environments={"PAPER"},
                instruments=[(INSTRUMENT_ID, 1)],
                actions={"ORDER.SUBMIT"},
                max_notional="1000",
                expires_at="2026-09-27T00:00:00Z",
                autonomous=True,
                valid_from="2026-09-26T00:32:00Z",
                protection_only=False,
            )
        )
        self.assertTrue(
            restarted.is_new_exposure_blocked("paper-account-1", "PAPER")
        )
        again = AuthorityService(JournalStore(self.path))
        self.assertTrue(
            again.is_new_exposure_blocked("paper-account-1", "PAPER")
        )

    def test_executor_retry_after_authority_commit_is_idempotent(self):
        accepted = self.host.submit(
            self.command(
                action="SET_AUTHORITY",
                payload={"policy": self.policy_payload("policy-retry")},
                suffix="4",
            )
        )
        first = self.executor.execute(accepted.operation_id)
        self.assertEqual(first.phase, "SUCCEEDED")
        restarted_host = JournalBackedHostCommandStore(
            JournalStore(self.path),
            account_id="paper-account-1",
            environment="PAPER",
            session_validator=lambda *_args: True,
            request_origin_provider=lambda: "https://local.autotrade.invalid",
            now=lambda: "2026-09-26T00:35:00Z",
        )
        restarted_authority = AuthorityService(JournalStore(self.path))
        restarted_executor = AuthorityCommandExecutor(
            restarted_host,
            restarted_authority,
        )
        same = restarted_executor.execute(accepted.operation_id)
        self.assertEqual(same, first)
        registered = [
            event
            for event in self.journal.load_events("authority_state", "canonical")
            if event["event_type"] == "AuthorityPolicyRegistered"
            and event["payload"]["policy_id"] == "policy-retry"
        ]
        self.assertEqual(len(registered), 1)

    def test_revoke_scope_mismatch_fails_without_authority_mutation(self):
        other = AuthorityPolicy.create(
            policy_id="other",
            account_id="other-account",
            environments={"PAPER"},
            instruments=[(INSTRUMENT_ID, 1)],
            actions={"ORDER.SUBMIT"},
            max_notional="10",
            expires_at="2026-09-27T00:00:00Z",
            autonomous=True,
        )
        self.authority.register_policy(other)
        accepted = self.host.submit(
            self.command(
                action="REVOKE_AUTHORITY",
                payload={"policy_id": "other"},
                suffix="5",
            )
        )
        failed = self.executor.execute(accepted.operation_id)
        self.assertEqual(failed.phase, "FAILED")
        self.assertNotIn("other", self.authority._revocations)


if __name__ == "__main__":
    unittest.main()
