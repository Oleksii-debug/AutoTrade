from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from mvp.autotrade_mvp.authority import (
    build_policy,
    dispatch_barrier,
    evaluate_authority,
    issue_confirmation,
    require_admission,
)


NOW = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)


def policy(*, autonomous=False):
    return build_policy(
        policy_id="policy-1",
        version=1,
        actor_id="user-1",
        environment="SIM",
        allowed_actions=("ORDER.SUBMIT", "ORDER.CANCEL"),
        provider_ids=("provider-a",),
        account_ids=("account-a",),
        instrument_ids=("instrument-a",),
        valid_from=NOW - timedelta(minutes=5),
        valid_until=NOW + timedelta(minutes=30),
        autonomous=autonomous,
    )


def intent(quantity="1"):
    return {
        "intent_id": "intent-1",
        "action": "ORDER.SUBMIT",
        "instrument_id": "instrument-a",
        "quantity": quantity,
        "price": "100",
    }


def confirmation(p=None, body=None):
    p = p or policy()
    body = body or intent()
    return issue_confirmation(
        confirmation_id="confirmation-1",
        actor_id="user-1",
        policy=p,
        intent=body,
        issued_at=NOW - timedelta(seconds=10),
        expires_at=NOW + timedelta(minutes=5),
    )


def evaluate(p=None, body=None, confirm=None, **overrides):
    p = p or policy()
    body = body or intent()
    values = dict(
        policy=p,
        intent=body,
        action="ORDER.SUBMIT",
        environment="SIM",
        provider_id="provider-a",
        account_id="account-a",
        instrument_id="instrument-a",
        at=NOW,
        confirmation=confirm if confirm is not None else confirmation(p, body),
    )
    values.update(overrides)
    return evaluate_authority(**values)


class AuthorityFoundationTests(unittest.TestCase):
    def test_confirmation_binds_exact_intent_hash(self):
        p = policy()
        confirmed = confirmation(p, intent("1"))
        changed = evaluate(p, intent("2"), confirmed)
        self.assertFalse(changed.allowed)
        self.assertIn("AUTH.CONFIRMATION_INTENT_MISMATCH", changed.reason_codes)

    def test_expired_confirmation_fails_closed(self):
        p = policy()
        old = issue_confirmation(
            confirmation_id="old",
            actor_id="user-1",
            policy=p,
            intent=intent(),
            issued_at=NOW - timedelta(minutes=2),
            expires_at=NOW - timedelta(seconds=1),
        )
        decision = evaluate(p, intent(), old)
        self.assertFalse(decision.allowed)
        self.assertIn("AUTH.CONFIRMATION_EXPIRED", decision.reason_codes)

    def test_revocation_after_admission_blocks_dispatch(self):
        p = policy()
        c = confirmation(p)
        admitted = evaluate(p, intent(), c)
        self.assertTrue(admitted.allowed)
        revoked = replace(p, revoked_at=NOW + timedelta(seconds=1))
        barrier = dispatch_barrier(
            admission=admitted,
            current_policy=revoked,
            intent=intent(),
            action="ORDER.SUBMIT",
            environment="SIM",
            provider_id="provider-a",
            account_id="account-a",
            instrument_id="instrument-a",
            at=NOW + timedelta(seconds=2),
            confirmation=c,
        )
        self.assertFalse(barrier.allowed)
        self.assertIn("AUTH.POLICY_REVOKED", barrier.reason_codes)

    def test_changed_policy_version_after_admission_blocks_dispatch(self):
        p = policy(autonomous=True)
        admitted = evaluate(
            p, intent(), None, confirmation=None
        )
        self.assertTrue(admitted.allowed)
        updated = replace(p, version=2)
        barrier = dispatch_barrier(
            admission=admitted,
            current_policy=updated,
            intent=intent(),
            action="ORDER.SUBMIT",
            environment="SIM",
            provider_id="provider-a",
            account_id="account-a",
            instrument_id="instrument-a",
            at=NOW,
            confirmation=None,
        )
        self.assertFalse(barrier.allowed)
        self.assertIn("AUTH.POLICY_CHANGED_AFTER_ADMISSION", barrier.reason_codes)

    def test_scope_is_exact_not_provider_or_account_coerced(self):
        decision = evaluate(provider_id="provider-b")
        self.assertFalse(decision.allowed)
        self.assertIn("AUTH.PROVIDER_OUT_OF_SCOPE", decision.reason_codes)

    def test_autonomous_policy_needs_no_confirmation_but_keeps_exact_scope(self):
        p = policy(autonomous=True)
        allowed = evaluate_authority(
            policy=p,
            intent=intent(),
            action="ORDER.SUBMIT",
            environment="SIM",
            provider_id="provider-a",
            account_id="account-a",
            instrument_id="instrument-a",
            at=NOW,
            confirmation=None,
        )
        self.assertTrue(allowed.allowed)
        denied = evaluate_authority(
            policy=p,
            intent=intent(),
            action="ORDER.SUBMIT",
            environment="LIVE",
            provider_id="provider-a",
            account_id="account-a",
            instrument_id="instrument-a",
            at=NOW,
            confirmation=None,
        )
        self.assertFalse(denied.allowed)
        self.assertIn("AUTH.ENVIRONMENT_MISMATCH", denied.reason_codes)

    def test_risk_rejection_cannot_be_overridden_by_authority(self):
        decision = evaluate()
        self.assertTrue(decision.allowed)
        with self.assertRaises(PermissionError):
            require_admission(decision, risk_verdict="REJECT")

    def test_binary_float_in_intent_is_rejected_before_hashing(self):
        p = policy(autonomous=True)
        bad = intent()
        bad["price"] = 100.1
        with self.assertRaises(TypeError):
            evaluate_authority(
                policy=p,
                intent=bad,
                action="ORDER.SUBMIT",
                environment="SIM",
                provider_id="provider-a",
                account_id="account-a",
                instrument_id="instrument-a",
                at=NOW,
                confirmation=None,
            )


if __name__ == "__main__":
    unittest.main()
