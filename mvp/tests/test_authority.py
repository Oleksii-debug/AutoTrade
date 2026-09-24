import unittest

from mvp.autotrade_mvp.authority import AuthorityPolicy, AuthorityService


def policy(**overrides):
    values = dict(
        policy_id="p1",
        account_id="paper-1",
        environments={"PAPER"},
        instruments={"ABC"},
        actions={"ORDER.SUBMIT", "ORDER.CANCEL"},
        max_notional="1000",
        expires_at="2026-09-25T00:00:00Z",
        autonomous=False,
        protection_only=False,
    )
    values.update(overrides)
    return AuthorityPolicy.create(**values)


class AuthorityTests(unittest.TestCase):
    def test_confirmation_is_bound_to_exact_intent_and_single_use(self):
        service = AuthorityService()
        service.register_policy(policy())
        service.add_confirmation(
            confirmation_id="c1",
            policy_id="p1",
            intent_hash="hash-a",
            account_id="paper-1",
            environment="PAPER",
            instrument="ABC",
            action="ORDER.SUBMIT",
            notional="100",
            expires_at="2026-09-24T23:00:00Z",
        )
        rejected = service.admit(
            admission_id="a-bad", policy_id="p1", intent_hash="hash-b",
            account_id="paper-1", environment="PAPER", instrument="ABC",
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
            confirmation_id="c1",
        )
        self.assertEqual(rejected.outcome, "REJECTED")
        self.assertEqual(rejected.reason, "confirmation_intent_mismatch")

        admitted = service.admit(
            admission_id="a-good", policy_id="p1", intent_hash="hash-a",
            account_id="paper-1", environment="PAPER", instrument="ABC",
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
            confirmation_id="c1",
        )
        self.assertEqual(admitted.outcome, "ADMITTED")

        reused = service.admit(
            admission_id="a-reuse", policy_id="p1", intent_hash="hash-a",
            account_id="paper-1", environment="PAPER", instrument="ABC",
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:01:00Z",
            confirmation_id="c1",
        )
        self.assertEqual(reused.reason, "confirmation_already_used")

    def test_confirmation_scope_is_bound_to_exact_financial_action(self):
        service = AuthorityService()
        service.register_policy(policy())
        service.add_confirmation(
            confirmation_id="c-scope",
            policy_id="p1",
            intent_hash="same-hash",
            account_id="paper-1",
            environment="PAPER",
            instrument="ABC",
            action="ORDER.SUBMIT",
            notional="100",
            expires_at="2026-09-24T23:00:00Z",
        )
        variants = (
            dict(account_id="other", environment="PAPER", instrument="ABC", action="ORDER.SUBMIT", notional="100"),
            dict(account_id="paper-1", environment="SIMULATION", instrument="ABC", action="ORDER.SUBMIT", notional="100"),
            dict(account_id="paper-1", environment="PAPER", instrument="XYZ", action="ORDER.SUBMIT", notional="100"),
            dict(account_id="paper-1", environment="PAPER", instrument="ABC", action="ORDER.CANCEL", notional="100"),
            dict(account_id="paper-1", environment="PAPER", instrument="ABC", action="ORDER.SUBMIT", notional="101"),
        )
        for index, scope in enumerate(variants):
            with self.subTest(scope=scope):
                result = service.admit(
                    admission_id=f"a-scope-{index}",
                    policy_id="p1",
                    intent_hash="same-hash",
                    state_version=1,
                    risk_admitted=True,
                    now="2026-09-24T18:00:00Z",
                    confirmation_id="c-scope",
                    **scope,
                )
                self.assertEqual(result.outcome, "REJECTED")
                self.assertEqual(result.reason, "confirmation_scope_mismatch")

        admitted = service.admit(
            admission_id="a-scope-good",
            policy_id="p1",
            intent_hash="same-hash",
            account_id="paper-1",
            environment="PAPER",
            instrument="ABC",
            action="ORDER.SUBMIT",
            notional="100",
            state_version=1,
            risk_admitted=True,
            now="2026-09-24T18:01:00Z",
            confirmation_id="c-scope",
        )
        self.assertEqual(admitted.outcome, "ADMITTED")

    def test_policy_expiry_and_revocation_fail_dispatch_barrier(self):
        service = AuthorityService()
        service.register_policy(policy(autonomous=True))
        admitted = service.admit(
            admission_id="a1", policy_id="p1", intent_hash="h1",
            account_id="paper-1", environment="PAPER", instrument="ABC",
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
        )
        self.assertEqual(admitted.outcome, "ADMITTED")
        self.assertEqual(
            service.dispatch_allowed(
                "a1", intent_hash="h1", account_id="paper-1", environment="PAPER",
                instrument="ABC", action="ORDER.SUBMIT", now="2026-09-24T18:01:00Z"
            ),
            (True, "allowed"),
        )
        service.revoke_policy(
            "p1", reason="operator revoke", revoked_at="2026-09-24T18:02:00Z"
        )
        self.assertEqual(
            service.dispatch_allowed(
                "a1", intent_hash="h1", account_id="paper-1", environment="PAPER",
                instrument="ABC", action="ORDER.SUBMIT", now="2026-09-24T18:03:00Z"
            ),
            (False, "policy_revoked"),
        )

        second = AuthorityService()
        second.register_policy(policy(autonomous=True))
        expired = second.admit(
            admission_id="a2", policy_id="p1", intent_hash="h2",
            account_id="paper-1", environment="PAPER", instrument="ABC",
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-25T00:00:00Z",
        )
        self.assertEqual(expired.reason, "policy_expired")

    def test_changed_intent_hash_is_blocked_at_dispatch(self):
        service = AuthorityService()
        service.register_policy(policy(autonomous=True))
        service.admit(
            admission_id="a1", policy_id="p1", intent_hash="original",
            account_id="paper-1", environment="PAPER", instrument="ABC",
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
        )
        self.assertEqual(
            service.dispatch_allowed(
                "a1", intent_hash="changed", account_id="paper-1", environment="PAPER",
                instrument="ABC", action="ORDER.SUBMIT", now="2026-09-24T18:01:00Z"
            ),
            (False, "intent_hash_changed"),
        )

    def test_dispatch_scope_cannot_change_after_admission(self):
        service = AuthorityService()
        service.register_policy(policy(autonomous=True))
        service.admit(
            admission_id="a1", policy_id="p1", intent_hash="h1",
            account_id="paper-1", environment="PAPER", instrument="ABC",
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
        )
        variants = (
            dict(account_id="other", environment="PAPER", instrument="ABC", action="ORDER.SUBMIT"),
            dict(account_id="paper-1", environment="SIMULATION", instrument="ABC", action="ORDER.SUBMIT"),
            dict(account_id="paper-1", environment="PAPER", instrument="XYZ", action="ORDER.SUBMIT"),
            dict(account_id="paper-1", environment="PAPER", instrument="ABC", action="ORDER.CANCEL"),
        )
        for scope in variants:
            with self.subTest(scope=scope):
                allowed, reason = service.dispatch_allowed(
                    "a1", intent_hash="h1", now="2026-09-24T18:01:00Z", **scope
                )
                self.assertFalse(allowed)
                self.assertEqual(reason, "admission_scope_changed")

    def test_future_revocation_applies_only_at_its_effective_time(self):
        service = AuthorityService()
        service.register_policy(policy(autonomous=True))
        service.admit(
            admission_id="a1", policy_id="p1", intent_hash="h1",
            account_id="paper-1", environment="PAPER", instrument="ABC",
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
        )
        service.revoke_policy(
            "p1", reason="scheduled revoke", revoked_at="2026-09-24T18:05:00Z"
        )
        before = service.dispatch_allowed(
            "a1", intent_hash="h1", account_id="paper-1", environment="PAPER",
            instrument="ABC", action="ORDER.SUBMIT", now="2026-09-24T18:04:59Z"
        )
        self.assertEqual(before, (True, "allowed"))
        at = service.dispatch_allowed(
            "a1", intent_hash="h1", account_id="paper-1", environment="PAPER",
            instrument="ABC", action="ORDER.SUBMIT", now="2026-09-24T18:05:00Z"
        )
        self.assertEqual(at, (False, "policy_revoked"))

    def test_unrelated_policy_registration_does_not_cancel_admission(self):
        service = AuthorityService()
        service.register_policy(policy(autonomous=True))
        service.admit(
            admission_id="a1", policy_id="p1", intent_hash="h1",
            account_id="paper-1", environment="PAPER", instrument="ABC",
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
        )
        service.register_policy(policy(
            policy_id="p2", account_id="paper-2", autonomous=True
        ))
        self.assertEqual(
            service.dispatch_allowed(
                "a1", intent_hash="h1", account_id="paper-1", environment="PAPER",
                instrument="ABC", action="ORDER.SUBMIT", now="2026-09-24T18:01:00Z"
            ),
            (True, "allowed"),
        )

    def test_policy_boolean_inputs_fail_closed(self):
        with self.assertRaises(TypeError):
            policy(autonomous="false")
        with self.assertRaises(TypeError):
            policy(protection_only="true")

    def test_protection_only_policy_cannot_open_nonreducing_risk(self):
        service = AuthorityService()
        service.register_policy(policy(
            autonomous=True,
            policy_id="protect",
            protection_only=True,
            actions={"ORDER.SUBMIT", "ORDER.CANCEL"},
        ))
        rejected = service.admit(
            admission_id="a1", policy_id="protect", intent_hash="h1",
            account_id="paper-1", environment="PAPER", instrument="ABC",
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, risk_reducing=False,
            now="2026-09-24T18:00:00Z",
        )
        self.assertEqual(rejected.reason, "protection_policy_requires_risk_reduction")
        allowed = service.admit(
            admission_id="a2", policy_id="protect", intent_hash="h2",
            account_id="paper-1", environment="PAPER", instrument="ABC",
            action="ORDER.CANCEL", notional="0", state_version=1,
            risk_admitted=True, risk_reducing=True,
            now="2026-09-24T18:01:00Z",
        )
        self.assertEqual(allowed.outcome, "ADMITTED")

    def test_scope_and_risk_fail_closed(self):
        service = AuthorityService()
        service.register_policy(policy(autonomous=True))
        cases = [
            dict(account_id="other", environment="PAPER", instrument="ABC", action="ORDER.SUBMIT", notional="10", risk_admitted=True, reason="account_out_of_scope"),
            dict(account_id="paper-1", environment="LIVE", instrument="ABC", action="ORDER.SUBMIT", notional="10", risk_admitted=True, reason="environment_out_of_scope"),
            dict(account_id="paper-1", environment="PAPER", instrument="XYZ", action="ORDER.SUBMIT", notional="10", risk_admitted=True, reason="instrument_out_of_scope"),
            dict(account_id="paper-1", environment="PAPER", instrument="ABC", action="WITHDRAW", notional="10", risk_admitted=True, reason="action_out_of_scope"),
            dict(account_id="paper-1", environment="PAPER", instrument="ABC", action="ORDER.SUBMIT", notional="1001", risk_admitted=True, reason="notional_out_of_scope"),
            dict(account_id="paper-1", environment="PAPER", instrument="ABC", action="ORDER.SUBMIT", notional="10", risk_admitted=False, reason="risk_rejected"),
        ]
        for index, case in enumerate(cases):
            with self.subTest(reason=case["reason"]):
                reason = case.pop("reason")
                record = service.admit(
                    admission_id=f"a{index}", policy_id="p1", intent_hash=f"h{index}",
                    state_version=1, now="2026-09-24T18:00:00Z", **case
                )
                self.assertEqual(record.outcome, "REJECTED")
                self.assertEqual(record.reason, reason)


    def test_retry_of_same_admission_is_idempotent_after_confirmation_consumed(self):
        service = AuthorityService()
        service.register_policy(policy())
        service.add_confirmation(
            confirmation_id="c1",
            policy_id="p1",
            intent_hash="h1",
            account_id="paper-1",
            environment="PAPER",
            instrument="ABC",
            action="ORDER.SUBMIT",
            notional="100",
            expires_at="2026-09-24T23:00:00Z",
        )
        kwargs = dict(
            admission_id="a1", policy_id="p1", intent_hash="h1",
            account_id="paper-1", environment="PAPER", instrument="ABC",
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
            confirmation_id="c1",
        )
        first = service.admit(**kwargs)
        second = service.admit(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual(second.outcome, "ADMITTED")

    def test_same_admission_id_with_changed_scope_conflicts(self):
        service = AuthorityService()
        service.register_policy(policy(autonomous=True))
        base = dict(
            admission_id="a1", policy_id="p1", intent_hash="h1",
            account_id="paper-1", environment="PAPER", instrument="ABC",
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
        )
        service.admit(**base)
        with self.assertRaisesRegex(Exception, "admission_id"):
            service.admit(**{**base, "notional": "101"})



if __name__ == "__main__":
    unittest.main()
