import tempfile
import unittest
from pathlib import Path

from mvp.autotrade_mvp.authority import AuthorityPolicy, AuthorityService
from mvp.autotrade_mvp.guarded_dispatch import (
    DispatchConflict,
    DispatchJournal,
    GuardedDispatcher,
    stable_client_order_id,
)


def make_authority():
    service = AuthorityService()
    service.register_policy(
        AuthorityPolicy.create(
            policy_id="p1",
            account_id="paper-1",
            environments={"PAPER"},
            instruments={"ABC"},
            actions={"ORDER.SUBMIT"},
            max_notional="1000",
            expires_at="2026-09-25T00:00:00Z",
            autonomous=True,
        )
    )
    service.admit(
        admission_id="a1",
        policy_id="p1",
        intent_hash="intent-1",
        account_id="paper-1",
        environment="PAPER",
        instrument="ABC",
        action="ORDER.SUBMIT",
        notional="100",
        state_version=1,
        risk_admitted=True,
        now="2026-09-24T18:00:00Z",
    )
    return service


class GuardedDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = DispatchJournal(Path(self.temp.name) / "dispatch.sqlite3")
        self.authority = make_authority()
        self.dispatcher = GuardedDispatcher(self.authority, self.journal)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def test_stable_client_id_is_deterministic_and_provider_scoped(self):
        first = stable_client_order_id("h", provider="x", account_id="a")
        self.assertEqual(first, stable_client_order_id("h", provider="x", account_id="a"))
        self.assertNotEqual(first, stable_client_order_id("h", provider="y", account_id="a"))
        self.assertLessEqual(len(first), 36)

    def test_revocation_during_wait_blocks_before_provider_call(self):
        calls = []
        def wait():
            self.authority.revoke_policy(
                "p1", reason="operator revoke", revoked_at="2026-09-24T18:00:30Z"
            )
        attempt = self.dispatcher.dispatch(
            attempt_id="d1",
            admission_id="a1",
            intent_hash="intent-1",
            provider="paper-provider",
            account_id="paper-1",
            now="2026-09-24T18:01:00Z",
            wait_before_send=wait,
            send=lambda client_id: calls.append(client_id),
        )
        self.assertEqual(attempt.state, "REJECTED")
        self.assertEqual(attempt.detail, "policy_revoked")
        self.assertEqual(calls, [])

    def test_duplicate_delivery_does_not_send_twice(self):
        calls = []
        kwargs = dict(
            attempt_id="d1",
            admission_id="a1",
            intent_hash="intent-1",
            provider="paper-provider",
            account_id="paper-1",
            now="2026-09-24T18:01:00Z",
            send=lambda client_id: calls.append(client_id),
        )
        first = self.dispatcher.dispatch(**kwargs)
        second = self.dispatcher.dispatch(**kwargs)
        self.assertEqual(first.state, "SENT")
        self.assertEqual(second.state, "SENT")
        self.assertEqual(len(calls), 1)
        self.assertEqual(first.client_order_id, second.client_order_id)

    def test_provider_exception_becomes_unknown_and_is_not_blindly_retried(self):
        calls = []
        def fail(client_id):
            calls.append(client_id)
            raise TimeoutError("ambiguous timeout")
        first = self.dispatcher.dispatch(
            attempt_id="d1",
            admission_id="a1",
            intent_hash="intent-1",
            provider="paper-provider",
            account_id="paper-1",
            now="2026-09-24T18:01:00Z",
            send=fail,
        )
        self.assertEqual(first.state, "UNKNOWN")
        second = self.dispatcher.dispatch(
            attempt_id="d1",
            admission_id="a1",
            intent_hash="intent-1",
            provider="paper-provider",
            account_id="paper-1",
            now="2026-09-24T18:02:00Z",
            send=lambda client_id: calls.append(client_id),
        )
        self.assertEqual(second.state, "UNKNOWN")
        self.assertEqual(len(calls), 1)

    def test_restart_converts_sending_to_unknown(self):
        self.journal.create(
            attempt_id="d1",
            admission_id="a1",
            intent_hash="intent-1",
            provider="paper-provider",
            account_id="paper-1",
            client_order_id="at-test",
        )
        self.journal.transition(
            "d1", expected={"PENDING"}, state="SENDING", detail="send_barrier_crossed"
        )
        self.assertEqual(self.journal.recover_interrupted(), 1)
        self.assertEqual(self.journal.get("d1").state, "UNKNOWN")

    def test_attempt_identity_conflict_fails_closed(self):
        self.journal.create(
            attempt_id="d1",
            admission_id="a1",
            intent_hash="intent-1",
            provider="paper-provider",
            account_id="paper-1",
            client_order_id="at-one",
        )
        with self.assertRaises(DispatchConflict):
            self.journal.create(
                attempt_id="d1",
                admission_id="a1",
                intent_hash="intent-2",
                provider="paper-provider",
                account_id="paper-1",
                client_order_id="at-two",
            )


if __name__ == "__main__":
    unittest.main()
