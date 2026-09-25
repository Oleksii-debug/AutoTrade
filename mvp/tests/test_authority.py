import unittest
from decimal import Decimal
from hashlib import sha256
import json
from tempfile import TemporaryDirectory

from mvp.autotrade_mvp.authority import (
    AuthorityPolicy,
    AuthorityService,
    InstrumentVersionIdentity,
)
from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"
OTHER_INSTRUMENT_ID = "22222222-2222-4222-8222-222222222222"


def instrument_ref(version=1, instrument_id=INSTRUMENT_ID):
    return (instrument_id, version)


def policy(**overrides):
    values = dict(
        policy_id="p1",
        account_id="paper-1",
        environments={"PAPER"},
        instruments={instrument_ref()},
        actions={"ORDER.SUBMIT", "ORDER.CANCEL"},
        max_notional="1000",
        valid_from="2026-09-24T00:00:00Z",
        expires_at="2026-09-25T00:00:00Z",
        autonomous=False,
        protection_only=False,
    )
    values.update(overrides)
    return AuthorityPolicy.create(**values)


class AuthorityTests(unittest.TestCase):
    def test_registration_rejects_unvalidated_policy_objects(self):
        class MutablePolicy:
            policy_id = "unsafe"

        service = AuthorityService()
        with self.assertRaisesRegex(TypeError, "AuthorityPolicy"):
            service.register_policy(MutablePolicy())

    def test_confirmation_is_bound_to_exact_intent_and_single_use(self):
        service = AuthorityService()
        service.register_policy(policy())
        service.add_confirmation(
            confirmation_id="c1",
            policy_id="p1",
            intent_hash="hash-a",
            account_id="paper-1",
            environment="PAPER",
            instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT",
            notional="100",
            expires_at="2026-09-24T23:00:00Z",
        )
        rejected = service.admit(
            admission_id="a-bad", policy_id="p1", intent_hash="hash-b",
            account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
            confirmation_id="c1",
        )
        self.assertEqual(rejected.outcome, "REJECTED")
        self.assertEqual(rejected.reason, "confirmation_intent_mismatch")

        admitted = service.admit(
            admission_id="a-good", policy_id="p1", intent_hash="hash-a",
            account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
            confirmation_id="c1",
        )
        self.assertEqual(admitted.outcome, "ADMITTED")

        reused = service.admit(
            admission_id="a-reuse", policy_id="p1", intent_hash="hash-a",
            account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:01:00Z",
            confirmation_id="c1",
        )
        self.assertEqual(reused.reason, "confirmation_already_used")

    def test_confirmation_is_bound_to_exact_financial_scope(self):
        variants = (
            dict(account_id="other", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", notional="100"),
            dict(account_id="paper-1", environment="SIMULATION", instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", notional="100"),
            dict(account_id="paper-1", environment="PAPER", instrument_id=OTHER_INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", notional="100"),
            dict(account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.CANCEL", notional="100"),
            dict(account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", notional="101"),
        )
        for index, confirmation_scope in enumerate(variants):
            with self.subTest(confirmation_scope=confirmation_scope):
                service = AuthorityService()
                service.register_policy(policy())
                service.add_confirmation(
                    confirmation_id=f"scope-{index}",
                    policy_id="p1",
                    intent_hash="same-hash",
                    expires_at="2026-09-24T23:00:00Z",
                    **confirmation_scope,
                )
                result = service.admit(
                    admission_id=f"admission-{index}",
                    policy_id="p1",
                    intent_hash="same-hash",
                    account_id="paper-1",
                    environment="PAPER",
                    instrument_id=INSTRUMENT_ID, instrument_version=1,
                    action="ORDER.SUBMIT",
                    notional="100",
                    state_version=1,
                    risk_admitted=True,
                    now="2026-09-24T18:00:00Z",
                    confirmation_id=f"scope-{index}",
                )
                self.assertEqual(result.outcome, "REJECTED")
                self.assertEqual(result.reason, "confirmation_scope_mismatch")

    def test_symbol_alias_is_not_an_authority_identity(self):
        with self.assertRaisesRegex(TypeError, "instrument"):
            policy(instruments={"ABC"})

    def test_policy_is_bound_to_exact_instrument_version(self):
        service = AuthorityService()
        service.register_policy(policy(autonomous=True))
        rejected = service.admit(
            admission_id="wrong-version",
            policy_id="p1",
            intent_hash="h-version",
            account_id="paper-1",
            environment="PAPER",
            instrument_id=INSTRUMENT_ID,
            instrument_version=2,
            action="ORDER.SUBMIT",
            notional="100",
            state_version=1,
            risk_admitted=True,
            now="2026-09-24T18:00:00Z",
        )
        self.assertEqual(rejected.outcome, "REJECTED")
        self.assertEqual(rejected.reason, "instrument_version_out_of_scope")

    def test_confirmation_rejects_same_instrument_different_version(self):
        service = AuthorityService()
        service.register_policy(
            policy(instruments={instrument_ref(1), instrument_ref(2)})
        )
        service.add_confirmation(
            confirmation_id="version-confirmation",
            policy_id="p1",
            intent_hash="same-hash",
            account_id="paper-1",
            environment="PAPER",
            instrument_id=INSTRUMENT_ID,
            instrument_version=1,
            action="ORDER.SUBMIT",
            notional="100",
            expires_at="2026-09-24T23:00:00Z",
        )
        rejected = service.admit(
            admission_id="version-mismatch",
            policy_id="p1",
            intent_hash="same-hash",
            account_id="paper-1",
            environment="PAPER",
            instrument_id=INSTRUMENT_ID,
            instrument_version=2,
            action="ORDER.SUBMIT",
            notional="100",
            state_version=1,
            risk_admitted=True,
            now="2026-09-24T18:00:00Z",
            confirmation_id="version-confirmation",
        )
        self.assertEqual(rejected.outcome, "REJECTED")
        self.assertEqual(rejected.reason, "confirmation_scope_mismatch")

    def test_dispatch_blocks_instrument_version_change_after_admission(self):
        service = AuthorityService()
        service.register_policy(policy(autonomous=True))
        service.admit(
            admission_id="versioned-admission",
            policy_id="p1",
            intent_hash="h1",
            account_id="paper-1",
            environment="PAPER",
            instrument_id=INSTRUMENT_ID,
            instrument_version=1,
            action="ORDER.SUBMIT",
            notional="100",
            state_version=1,
            risk_admitted=True,
            now="2026-09-24T18:00:00Z",
        )
        allowed, reason = service.dispatch_allowed(
            "versioned-admission",
            intent_hash="h1",
            account_id="paper-1",
            environment="PAPER",
            instrument_id=INSTRUMENT_ID,
            instrument_version=2,
            action="ORDER.SUBMIT",
            now="2026-09-24T18:01:00Z",
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "admission_scope_changed")

    def test_instrument_identity_normalizes_uuid_and_rejects_invalid_version(self):
        identity = InstrumentVersionIdentity(INSTRUMENT_ID.upper(), 1)
        self.assertEqual(identity.instrument_id, INSTRUMENT_ID)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            InstrumentVersionIdentity(INSTRUMENT_ID, 0)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            InstrumentVersionIdentity(INSTRUMENT_ID, True)
        with self.assertRaisesRegex(ValueError, "UUID"):
            InstrumentVersionIdentity("ABC", 1)

    def test_confirmation_rejects_negative_notional(self):
        service = AuthorityService()
        service.register_policy(policy())
        with self.assertRaisesRegex(ValueError, "non-negative"):
            service.add_confirmation(
                confirmation_id="negative",
                policy_id="p1",
                intent_hash="h",
                account_id="paper-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID, instrument_version=1,
                action="ORDER.SUBMIT",
                notional="-1",
                expires_at="2026-09-24T23:00:00Z",
            )

    def test_future_policy_is_rejected_until_valid_from(self):
        service = AuthorityService()
        service.register_policy(policy(
            autonomous=True,
            valid_from="2026-09-24T19:00:00Z",
            expires_at="2026-09-24T20:00:00Z",
        ))
        early = service.admit(
            admission_id="future-early", policy_id="p1", intent_hash="h1",
            account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:59:59Z",
        )
        self.assertEqual(early.outcome, "REJECTED")
        self.assertEqual(early.reason, "policy_not_yet_active")

        active = service.admit(
            admission_id="future-active", policy_id="p1", intent_hash="h2",
            account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T19:00:00Z",
        )
        self.assertEqual(active.outcome, "ADMITTED")

    def test_direct_policy_construction_cannot_bypass_confirmation_with_truthy_string(self):
        with self.assertRaisesRegex(TypeError, "must be booleans"):
            AuthorityPolicy(
                policy_id="unsafe-direct",
                account_id="paper-1",
                environments=frozenset({"PAPER"}),
                instruments=frozenset({instrument_ref()}),
                actions=frozenset({"ORDER.SUBMIT"}),
                max_notional=Decimal("1000"),
                valid_from="2026-09-24T00:00:00Z",
                expires_at="2026-09-25T00:00:00Z",
                autonomous="false",
                protection_only=False,
            )

    def test_direct_policy_construction_normalizes_same_authority_invariants(self):
        direct = AuthorityPolicy(
            policy_id=" direct ",
            account_id=" paper-1 ",
            environments=frozenset({"paper"}),
            instruments=frozenset({instrument_ref()}),
            actions=frozenset({"order.submit"}),
            max_notional="1000",
            valid_from="2026-09-24T00:00:00Z",
            expires_at="2026-09-25T00:00:00Z",
            autonomous=False,
            protection_only=False,
        )
        self.assertEqual(direct.policy_id, "direct")
        self.assertEqual(direct.account_id, "paper-1")
        self.assertEqual(direct.environments, frozenset({"PAPER"}))
        self.assertEqual(
            direct.instruments,
            frozenset({InstrumentVersionIdentity(INSTRUMENT_ID, 1)}),
        )
        self.assertEqual(direct.actions, frozenset({"ORDER.SUBMIT"}))
        self.assertEqual(direct.max_notional, Decimal("1000"))

    def test_policy_requires_nonempty_validity_window(self):
        with self.assertRaisesRegex(ValueError, "valid_from must precede expires_at"):
            policy(
                valid_from="2026-09-25T00:00:00Z",
                expires_at="2026-09-25T00:00:00Z",
            )
        with self.assertRaisesRegex(ValueError, "valid_from must precede expires_at"):
            policy(
                valid_from="2026-09-25T00:00:01Z",
                expires_at="2026-09-25T00:00:00Z",
            )

    def test_policy_expiry_and_revocation_fail_dispatch_barrier(self):
        service = AuthorityService()
        service.register_policy(policy(autonomous=True))
        admitted = service.admit(
            admission_id="a1", policy_id="p1", intent_hash="h1",
            account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
        )
        self.assertEqual(admitted.outcome, "ADMITTED")
        self.assertEqual(
            service.dispatch_allowed(
                "a1", intent_hash="h1", account_id="paper-1", environment="PAPER",
                instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", now="2026-09-24T18:01:00Z"
            ),
            (True, "allowed"),
        )
        service.revoke_policy(
            "p1", reason="operator revoke", revoked_at="2026-09-24T18:02:00Z"
        )
        self.assertEqual(
            service.dispatch_allowed(
                "a1", intent_hash="h1", account_id="paper-1", environment="PAPER",
                instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", now="2026-09-24T18:03:00Z"
            ),
            (False, "policy_revoked"),
        )

        second = AuthorityService()
        second.register_policy(policy(autonomous=True))
        expired = second.admit(
            admission_id="a2", policy_id="p1", intent_hash="h2",
            account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-25T00:00:00Z",
        )
        self.assertEqual(expired.reason, "policy_expired")

    def test_changed_intent_hash_is_blocked_at_dispatch(self):
        service = AuthorityService()
        service.register_policy(policy(autonomous=True))
        service.admit(
            admission_id="a1", policy_id="p1", intent_hash="original",
            account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
        )
        self.assertEqual(
            service.dispatch_allowed(
                "a1", intent_hash="changed", account_id="paper-1", environment="PAPER",
                instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", now="2026-09-24T18:01:00Z"
            ),
            (False, "intent_hash_changed"),
        )

    def test_dispatch_scope_cannot_change_after_admission(self):
        service = AuthorityService()
        service.register_policy(policy(autonomous=True))
        service.admit(
            admission_id="a1", policy_id="p1", intent_hash="h1",
            account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
        )
        variants = (
            dict(account_id="other", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT"),
            dict(account_id="paper-1", environment="SIMULATION", instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT"),
            dict(account_id="paper-1", environment="PAPER", instrument_id=OTHER_INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT"),
            dict(account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.CANCEL"),
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
            account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
        )
        service.revoke_policy(
            "p1", reason="scheduled revoke", revoked_at="2026-09-24T18:05:00Z"
        )
        before = service.dispatch_allowed(
            "a1", intent_hash="h1", account_id="paper-1", environment="PAPER",
            instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", now="2026-09-24T18:04:59Z"
        )
        self.assertEqual(before, (True, "allowed"))
        at = service.dispatch_allowed(
            "a1", intent_hash="h1", account_id="paper-1", environment="PAPER",
            instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", now="2026-09-24T18:05:00Z"
        )
        self.assertEqual(at, (False, "policy_revoked"))

    def test_unrelated_policy_registration_does_not_cancel_admission(self):
        service = AuthorityService()
        service.register_policy(policy(autonomous=True))
        service.admit(
            admission_id="a1", policy_id="p1", intent_hash="h1",
            account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
        )
        service.register_policy(policy(
            policy_id="p2", account_id="paper-2", autonomous=True
        ))
        self.assertEqual(
            service.dispatch_allowed(
                "a1", intent_hash="h1", account_id="paper-1", environment="PAPER",
                instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", now="2026-09-24T18:01:00Z"
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
            account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, risk_reducing=False,
            now="2026-09-24T18:00:00Z",
        )
        self.assertEqual(rejected.reason, "protection_policy_requires_risk_reduction")
        allowed = service.admit(
            admission_id="a2", policy_id="protect", intent_hash="h2",
            account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.CANCEL", notional="0", state_version=1,
            risk_admitted=True, risk_reducing=True,
            now="2026-09-24T18:01:00Z",
        )
        self.assertEqual(allowed.outcome, "ADMITTED")

    def test_scope_and_risk_fail_closed(self):
        service = AuthorityService()
        service.register_policy(policy(autonomous=True))
        cases = [
            dict(account_id="other", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", notional="10", risk_admitted=True, reason="account_out_of_scope"),
            dict(account_id="paper-1", environment="LIVE", instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", notional="10", risk_admitted=True, reason="environment_out_of_scope"),
            dict(account_id="paper-1", environment="PAPER", instrument_id=OTHER_INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", notional="10", risk_admitted=True, reason="instrument_out_of_scope"),
            dict(account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1, action="WITHDRAW", notional="10", risk_admitted=True, reason="action_out_of_scope"),
            dict(account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", notional="1001", risk_admitted=True, reason="notional_out_of_scope"),
            dict(account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", notional="10", risk_admitted=False, reason="risk_rejected"),
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
            instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT",
            notional="100",
            expires_at="2026-09-24T23:00:00Z",
        )
        kwargs = dict(
            admission_id="a1", policy_id="p1", intent_hash="h1",
            account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1,
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
            account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
        )
        service.admit(**base)
        with self.assertRaisesRegex(Exception, "admission_id"):
            service.admit(**{**base, "notional": "101"})
        with self.assertRaisesRegex(Exception, "admission_id"):
            service.admit(**{**base, "instrument_version": 2})



    def test_durable_versioned_admission_and_confirmation_survive_restart(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            service = AuthorityService(store)
            service.register_policy(policy())
            service.add_confirmation(
                confirmation_id="c-durable",
                policy_id="p1",
                intent_hash="h-durable",
                account_id="paper-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                expires_at="2026-09-24T23:00:00Z",
            )
            admitted = service.admit(
                admission_id="a-durable", policy_id="p1", intent_hash="h-durable",
                account_id="paper-1", environment="PAPER",
                instrument_id=INSTRUMENT_ID, instrument_version=1,
                action="ORDER.SUBMIT", notional="100", state_version=1,
                risk_admitted=True, now="2026-09-24T18:00:00Z",
                confirmation_id="c-durable",
            )
            self.assertEqual(admitted.outcome, "ADMITTED")

            restarted = AuthorityService(store)
            self.assertEqual(
                restarted.dispatch_allowed(
                    "a-durable", intent_hash="h-durable",
                    account_id="paper-1", environment="PAPER",
                    instrument_id=INSTRUMENT_ID, instrument_version=1,
                    action="ORDER.SUBMIT", now="2026-09-24T18:01:00Z",
                ),
                (True, "allowed"),
            )
            self.assertEqual(
                restarted.dispatch_allowed(
                    "a-durable", intent_hash="h-durable",
                    account_id="paper-1", environment="PAPER",
                    instrument_id=INSTRUMENT_ID, instrument_version=2,
                    action="ORDER.SUBMIT", now="2026-09-24T18:01:00Z",
                ),
                (False, "admission_scope_changed"),
            )
            reused = restarted.admit(
                admission_id="a-durable-2", policy_id="p1", intent_hash="h-durable",
                account_id="paper-1", environment="PAPER",
                instrument_id=INSTRUMENT_ID, instrument_version=1,
                action="ORDER.SUBMIT", notional="100", state_version=2,
                risk_admitted=True, now="2026-09-24T18:02:00Z",
                confirmation_id="c-durable",
            )
            self.assertEqual(reused.reason, "confirmation_already_used")

    def test_durable_versioned_revocation_survives_restart(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            service = AuthorityService(store)
            service.register_policy(policy(autonomous=True))
            service.admit(
                admission_id="a-revoke", policy_id="p1", intent_hash="h-revoke",
                account_id="paper-1", environment="PAPER",
                instrument_id=INSTRUMENT_ID, instrument_version=1,
                action="ORDER.SUBMIT", notional="100", state_version=1,
                risk_admitted=True, now="2026-09-24T18:00:00Z",
            )
            service.revoke_policy(
                "p1", reason="operator revoke", revoked_at="2026-09-24T18:02:00Z"
            )
            restarted = AuthorityService(store)
            self.assertEqual(
                restarted.dispatch_allowed(
                    "a-revoke", intent_hash="h-revoke",
                    account_id="paper-1", environment="PAPER",
                    instrument_id=INSTRUMENT_ID, instrument_version=1,
                    action="ORDER.SUBMIT", now="2026-09-24T18:03:00Z",
                ),
                (False, "policy_revoked"),
            )

    def test_durable_retry_does_not_duplicate_versioned_authority_events(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            first = AuthorityService(store)
            item = policy(autonomous=True)
            self.assertTrue(first.register_policy(item))
            self.assertFalse(first.register_policy(item))
            restarted = AuthorityService(store)
            self.assertFalse(restarted.register_policy(item))
            events = store.load_events("authority_state", "canonical")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["AuthorityPolicyRegistered"],
            )

    def test_dispatch_guard_checks_versioned_scope_after_revoke(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            authority.register_policy(policy(autonomous=True))
            admitted = authority.admit(
                admission_id="a-guard", policy_id="p1", intent_hash="h-guard",
                account_id="paper-1", environment="PAPER",
                instrument_id=INSTRUMENT_ID, instrument_version=1,
                action="ORDER.SUBMIT", notional="100", state_version=1,
                risk_admitted=True, now="2026-09-24T18:00:00Z",
            )
            self.assertEqual(admitted.outcome, "ADMITTED")
            guard = authority.dispatch_guard(
                "a-guard",
                account_id="paper-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="paper-1",
                owner_token="owner",
            )
            outbound = 0

            def transport(client_id, request, final_guard):
                nonlocal outbound
                authority.revoke_policy(
                    "p1",
                    reason="operator revoke",
                    revoked_at="2026-09-24T18:00:01Z",
                )
                final_guard()
                outbound += 1
                return {"ok": True}

            result = dispatcher.dispatch(
                attempt_id="attempt-versioned",
                intent_id="intent-versioned",
                intent_hash="h-guard",
                provider="sim",
                request={},
                now="2026-09-24T18:00:00Z",
                authority_check=guard,
                transport_send=transport,
                final_barrier_clock=lambda: "2026-09-24T18:00:02Z",
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "policy_revoked")
            self.assertEqual(outbound, 0)


    def test_durable_replay_rejects_admitted_scope_outside_policy(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            service = AuthorityService(store)
            service.register_policy(policy(autonomous=True))

            request = {
                "policy_id": "p1",
                "intent_hash": "forged-intent",
                "account_id": "other-account",
                "environment": "PAPER",
                "instrument_id": INSTRUMENT_ID,
                "instrument_version": 1,
                "action": "ORDER.SUBMIT",
                "notional": "100",
                "state_version": 1,
                "risk_admitted": True,
                "confirmation_id": None,
                "risk_reducing": False,
            }
            fingerprint = sha256(
                json.dumps(
                    request, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            payload = {
                "admission_id": "forged-admission",
                "policy_id": "p1",
                "intent_hash": "forged-intent",
                "account_id": "other-account",
                "environment": "PAPER",
                "instrument": {
                    "instrument_id": INSTRUMENT_ID,
                    "version": 1,
                },
                "action": "ORDER.SUBMIT",
                "notional": "100",
                "risk_reducing": False,
                "state_version": 1,
                "authority_epoch": 1,
                "outcome": "ADMITTED",
                "admitted_at": "2026-09-24T18:00:00Z",
                "confirmation_id": None,
                "reason": "admitted",
                "request_fingerprint": fingerprint,
            }
            store.append_event(
                {
                    "event_id": "forged-admission-event",
                    "event_type": "AuthorityAdmissionRecorded",
                    "aggregate_type": "authority_state",
                    "aggregate_id": "canonical",
                    "aggregate_version": 2,
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": "2026-09-24T18:00:00Z",
                }
            )
            with self.assertRaisesRegex(
                Exception, "violates policy scope"
            ):
                AuthorityService(store)

    def test_durable_replay_rejects_malformed_admission_types(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            service = AuthorityService(store)
            service.register_policy(policy(autonomous=True))
            payload = {
                "admission_id": "malformed-admission",
                "policy_id": "p1",
                "intent_hash": "h",
                "account_id": "paper-1",
                "environment": "PAPER",
                "instrument": {
                    "instrument_id": INSTRUMENT_ID,
                    "version": 1,
                },
                "action": "ORDER.SUBMIT",
                "notional": "100",
                "risk_reducing": "false",
                "state_version": 1,
                "authority_epoch": 1,
                "outcome": "ADMITTED",
                "admitted_at": "2026-09-24T18:00:00Z",
                "confirmation_id": None,
                "reason": "admitted",
                "request_fingerprint": "a" * 64,
            }
            store.append_event(
                {
                    "event_id": "malformed-admission-event",
                    "event_type": "AuthorityAdmissionRecorded",
                    "aggregate_type": "authority_state",
                    "aggregate_id": "canonical",
                    "aggregate_version": 2,
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": "2026-09-24T18:00:00Z",
                }
            )
            with self.assertRaisesRegex(TypeError, "risk_reducing"):
                AuthorityService(store)

    def test_durable_replay_rejects_double_confirmation_consumption(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            service = AuthorityService(store)
            service.register_policy(policy())
            service.add_confirmation(
                confirmation_id="single-use",
                policy_id="p1",
                intent_hash="same-intent",
                account_id="paper-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                expires_at="2026-09-24T23:00:00Z",
            )
            first = service.admit(
                admission_id="first-use",
                policy_id="p1",
                intent_hash="same-intent",
                account_id="paper-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                state_version=1,
                risk_admitted=True,
                now="2026-09-24T18:00:00Z",
                confirmation_id="single-use",
            )
            self.assertEqual(first.outcome, "ADMITTED")
            duplicate = {
                "admission_id": "second-use",
                "policy_id": first.policy_id,
                "intent_hash": first.intent_hash,
                "account_id": first.account_id,
                "environment": first.environment,
                "instrument": {
                    "instrument_id": first.instrument_version.instrument_id,
                    "version": first.instrument_version.version,
                },
                "action": first.action,
                "notional": str(first.notional),
                "risk_reducing": first.risk_reducing,
                "state_version": first.state_version,
                "authority_epoch": first.authority_epoch,
                "outcome": first.outcome,
                "admitted_at": "2026-09-24T18:00:01Z",
                "confirmation_id": first.confirmation_id,
                "reason": first.reason,
                "request_fingerprint": first.request_fingerprint,
            }
            store.append_event(
                {
                    "event_id": "second-confirmation-use-event",
                    "event_type": "AuthorityAdmissionRecorded",
                    "aggregate_type": "authority_state",
                    "aggregate_id": "canonical",
                    "aggregate_version": 4,
                    "payload": duplicate,
                    "payload_hash": payload_digest(duplicate),
                    "committed_at": "2026-09-24T18:00:01Z",
                }
            )
            with self.assertRaisesRegex(
                Exception, "consumed by multiple admissions"
            ):
                AuthorityService(store)

    def test_stale_authority_process_cannot_double_consume_confirmation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            seed = AuthorityService(store)
            seed.register_policy(policy())
            seed.add_confirmation(
                confirmation_id="concurrent-single-use",
                policy_id="p1",
                intent_hash="h-concurrent",
                account_id="paper-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                expires_at="2026-09-24T23:00:00Z",
            )

            first = AuthorityService(store)
            stale = AuthorityService(store)

            admitted = first.admit(
                admission_id="winner",
                policy_id="p1",
                intent_hash="h-concurrent",
                account_id="paper-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                state_version=1,
                risk_admitted=True,
                now="2026-09-24T18:00:00Z",
                confirmation_id="concurrent-single-use",
            )
            self.assertEqual(admitted.outcome, "ADMITTED")

            with self.assertRaisesRegex(
                AuthorityConflict,
                "journal advanced|changed concurrently",
            ):
                stale.admit(
                    admission_id="stale-loser",
                    policy_id="p1",
                    intent_hash="h-concurrent",
                    account_id="paper-1",
                    environment="PAPER",
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=1,
                    action="ORDER.SUBMIT",
                    notional="100",
                    state_version=1,
                    risk_admitted=True,
                    now="2026-09-24T18:00:01Z",
                    confirmation_id="concurrent-single-use",
                )

            restarted = AuthorityService(store)
            self.assertEqual(
                restarted.dispatch_allowed(
                    "winner",
                    intent_hash="h-concurrent",
                    account_id="paper-1",
                    environment="PAPER",
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=1,
                    action="ORDER.SUBMIT",
                    now="2026-09-24T18:00:02Z",
                ),
                (True, "allowed"),
            )
            events = store.load_events("authority_state", "canonical")
            admissions = [
                event for event in events
                if event["event_type"] == "AuthorityAdmissionRecorded"
            ]
            self.assertEqual(len(admissions), 1)

    def test_stale_authority_process_cannot_append_after_revocation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            seed = AuthorityService(store)
            seed.register_policy(policy(autonomous=True))

            stale = AuthorityService(store)
            revoker = AuthorityService(store)
            revoker.revoke_policy(
                "p1",
                reason="operator revoke",
                revoked_at="2026-09-24T18:00:01Z",
            )

            with self.assertRaisesRegex(
                AuthorityConflict,
                "journal advanced|changed concurrently",
            ):
                stale.admit(
                    admission_id="stale-after-revoke",
                    policy_id="p1",
                    intent_hash="h-stale",
                    account_id="paper-1",
                    environment="PAPER",
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=1,
                    action="ORDER.SUBMIT",
                    notional="100",
                    state_version=1,
                    risk_admitted=True,
                    now="2026-09-24T18:00:02Z",
                )

            restarted = AuthorityService(store)
            self.assertEqual(
                restarted.epoch,
                2,
            )


if __name__ == "__main__":
    unittest.main()
