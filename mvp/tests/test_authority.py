import unittest
from decimal import Decimal
from hashlib import sha256
import json
from tempfile import TemporaryDirectory

from mvp.autotrade_mvp.authority import (
    AuthorityConflict,
    AuthorityPolicy,
    AuthorityService,
    InstrumentVersionIdentity,
)
from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.reconciliation import (
    ResourceAvailabilityEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.autotrade_mvp.risk import (
    RiskContext,
    RiskDecision,
    RiskIntent,
    RiskPolicy,
    RiskRuleResult,
    bind_risk_decision,
)


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



def bound_risk_decision(
    *,
    admitted=True,
    intent_hash="sha256:" + "a" * 64,
    state_version=7,
    policy_version=1,
    reservation_version=0,
    reservation_requirements=None,
    capability_snapshot_id="cap-snapshot-1",
    evaluated_at="2026-09-24T18:00:00Z",
    valid_until="2026-09-24T18:30:00Z",
):
    raw = RiskDecision(
        admitted=admitted,
        resulting_position=Decimal("1"),
        gross_leverage=Decimal("0.10"),
        net_leverage=Decimal("0.10"),
        worst_stress_loss=Decimal("10"),
        input_fingerprint=sha256(b"authority-bound-risk-fixture").hexdigest(),
        rules=(
            RiskRuleResult(
                rule="test-boundary",
                passed=admitted,
                observed="bounded",
                limit="bounded",
                reason="fixture",
            ),
        ),
    )
    return bind_risk_decision(
        raw,
        intent_hash=intent_hash,
        state_version=state_version,
        policy_version=policy_version,
        reservation_version=reservation_version,
        reservation_requirements=(
            {"CASH:USD": "100"}
            if reservation_requirements is None
            else reservation_requirements
        ),
        capability_snapshot_id=capability_snapshot_id,
        evaluated_at=evaluated_at,
        valid_until=valid_until,
    )


PUBLIC_INTENT_HASH = "sha256:" + "a" * 64
PUBLIC_CAPABILITY_SNAPSHOT_ID = "cap-snapshot-1"
PUBLIC_RISK_VALID_UNTIL = "2026-09-24T18:30:00Z"


def public_risk_intent(*, expected_state_version=7):
    return RiskIntent.create(
        symbol="ABC",
        side="BUY",
        quantity="1",
        price="100",
        expected_state_version=expected_state_version,
    )


def public_risk_context(*, state_version=7):
    return RiskContext.create(
        state_version=state_version,
        equity="1000",
        positions={},
        marks={"ABC": "100"},
        reserved_position_delta={},
        daily_pnl="0",
        drawdown_fraction="0",
        market_data_age_seconds="1",
        fx_age_seconds={"USD": "1"},
        margin_headroom="1",
        capability_allowed=True,
        borrow_available=True,
        stress_scenarios=({"ABC": "-0.10"},),
    )


def public_risk_policy(**overrides):
    values = dict(
        max_abs_position="10",
        max_single_notional="1000",
        max_gross_leverage="2",
        max_net_leverage="2",
        max_daily_loss="500",
        max_drawdown_fraction="0.20",
        max_data_age_seconds="5",
        max_fx_age_seconds="60",
        min_margin_headroom="0.20",
        max_stress_loss="500",
    )
    values.update(overrides)
    return RiskPolicy.create(**values)


def public_financial_kwargs(store, **overrides):
    provider_id = "TEST_PROVIDER"
    result = reconcile_account(
        provider_id=provider_id,
        account_id="paper-1",
        environment="PAPER",
        local_cash={"USD": "1000"},
        provider_cash={"USD": "1000"},
        local_positions={},
        provider_positions={},
        local_execution_ids=(),
        provider_fills=(),
        snapshot_consistency=SnapshotConsistencyEvidence(
            provider_id=provider_id,
            account_id="paper-1",
            environment="PAPER",
            mode="ATOMIC",
            query_started_at="2026-09-24T18:00:00Z",
            query_completed_at="2026-09-24T18:00:30Z",
        ),
        coverage_start="2026-09-24T18:00:00Z",
        coverage_end="2026-09-24T18:01:00Z",
        pagination_complete=True,
        provider_activity_provider_id=provider_id,
        provider_activity_account_id="paper-1",
        resource_availability=ResourceAvailabilityEvidence(
            provider_id=provider_id,
            account_id="paper-1",
            environment="PAPER",
            snapshot_id="authority-account-snapshot",
            query_started_at="2026-09-24T18:00:00Z",
            query_completed_at="2026-09-24T18:00:30Z",
            valid_until="2026-09-24T18:02:00Z",
            available_resources={"CASH:USD": "1000"},
            provider_as_of="2026-09-24T18:00:30Z",
            evidence_refs=("provider:authority-account-snapshot",),
        ),
    )
    checkpoint = record_reconciliation_checkpoint(
        store,
        reconciliation_id="paper-1:admission-availability",
        result=result,
        observed_at="2026-09-24T18:00:30Z",
        host_id="authority-test-host",
        owner_epoch="1",
    )
    for pending in store.pending_outbox():
        if pending["event_id"] == checkpoint["event_id"]:
            store.mark_outbox_delivered(
                pending["outbox_id"],
                expected_envelope_hash=pending["envelope_hash"],
            )
    values = dict(
        intent_hash=PUBLIC_INTENT_HASH,
        capability_snapshot_id=PUBLIC_CAPABILITY_SNAPSHOT_ID,
        risk_intent=public_risk_intent(),
        risk_context=public_risk_context(),
        risk_policy=public_risk_policy(),
        risk_valid_until=PUBLIC_RISK_VALID_UNTIL,
        reservation_requirements={"CASH:USD": "100"},
        reservation_available={"CASH:USD": "1000"},
        reservation_checkpoint_event_id=checkpoint["event_id"],
        reservation_provider_id=provider_id,
        reservation_max_age_seconds="60",
        now="2026-09-24T18:01:00Z",
    )
    values.update(overrides)
    return values

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
        rejected = service._admit_unverified(
            admission_id="a-bad", policy_id="p1", intent_hash="hash-b",
            account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
            confirmation_id="c1",
        )
        self.assertEqual(rejected.outcome, "REJECTED")
        self.assertEqual(rejected.reason, "confirmation_intent_mismatch")

        admitted = service._admit_unverified(
            admission_id="a-good", policy_id="p1", intent_hash="hash-a",
            account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
            confirmation_id="c1",
        )
        self.assertEqual(admitted.outcome, "ADMITTED")

        reused = service._admit_unverified(
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
                result = service._admit_unverified(
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
        rejected = service._admit_unverified(
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
        rejected = service._admit_unverified(
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
        service._admit_unverified(
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
        early = service._admit_unverified(
            admission_id="future-early", policy_id="p1", intent_hash="h1",
            account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:59:59Z",
        )
        self.assertEqual(early.outcome, "REJECTED")
        self.assertEqual(early.reason, "policy_not_yet_active")

        active = service._admit_unverified(
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
        service.register_policy(policy(autonomous=True, environments={"SIMULATION"}))
        admitted = service._admit_unverified(
            admission_id="a1", policy_id="p1", intent_hash="h1",
            account_id="paper-1", environment="SIMULATION", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
        )
        self.assertEqual(admitted.outcome, "ADMITTED")
        self.assertEqual(
            service.dispatch_allowed(
                "a1", intent_hash="h1", account_id="paper-1", environment="SIMULATION",
                instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", now="2026-09-24T18:01:00Z"
            ),
            (True, "allowed"),
        )
        service.revoke_policy(
            "p1", reason="operator revoke", revoked_at="2026-09-24T18:02:00Z"
        )
        self.assertEqual(
            service.dispatch_allowed(
                "a1", intent_hash="h1", account_id="paper-1", environment="SIMULATION",
                instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", now="2026-09-24T18:03:00Z"
            ),
            (False, "policy_revoked"),
        )

        second = AuthorityService()
        second.register_policy(policy(autonomous=True, environments={"SIMULATION"}))
        expired = second._admit_unverified(
            admission_id="a2", policy_id="p1", intent_hash="h2",
            account_id="paper-1", environment="SIMULATION", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-25T00:00:00Z",
        )
        self.assertEqual(expired.reason, "policy_expired")

    def test_changed_intent_hash_is_blocked_at_dispatch(self):
        service = AuthorityService()
        service.register_policy(policy(autonomous=True))
        service._admit_unverified(
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
        service._admit_unverified(
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
        service.register_policy(policy(autonomous=True, environments={"SIMULATION"}))
        service._admit_unverified(
            admission_id="a1", policy_id="p1", intent_hash="h1",
            account_id="paper-1", environment="SIMULATION", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
        )
        service.revoke_policy(
            "p1", reason="scheduled revoke", revoked_at="2026-09-24T18:05:00Z"
        )
        before = service.dispatch_allowed(
            "a1", intent_hash="h1", account_id="paper-1", environment="SIMULATION",
            instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", now="2026-09-24T18:04:59Z"
        )
        self.assertEqual(before, (True, "allowed"))
        at = service.dispatch_allowed(
            "a1", intent_hash="h1", account_id="paper-1", environment="SIMULATION",
            instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", now="2026-09-24T18:05:00Z"
        )
        self.assertEqual(at, (False, "policy_revoked"))

    def test_unrelated_policy_registration_does_not_cancel_admission(self):
        service = AuthorityService()
        service.register_policy(policy(autonomous=True, environments={"SIMULATION"}))
        service._admit_unverified(
            admission_id="a1", policy_id="p1", intent_hash="h1",
            account_id="paper-1", environment="SIMULATION", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, now="2026-09-24T18:00:00Z",
        )
        service.register_policy(policy(
            policy_id="p2", account_id="paper-2", autonomous=True
        ))
        self.assertEqual(
            service.dispatch_allowed(
                "a1", intent_hash="h1", account_id="paper-1", environment="SIMULATION",
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
        rejected = service._admit_unverified(
            admission_id="a1", policy_id="protect", intent_hash="h1",
            account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1,
            action="ORDER.SUBMIT", notional="100", state_version=1,
            risk_admitted=True, risk_reducing=False,
            now="2026-09-24T18:00:00Z",
        )
        self.assertEqual(rejected.reason, "protection_policy_requires_risk_reduction")
        allowed = service._admit_unverified(
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
            dict(account_id="paper-1", environment="PAPER", instrument_id=OTHER_INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", notional="10", risk_admitted=True, reason="instrument_version_out_of_scope"),
            dict(account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1, action="WITHDRAW", notional="10", risk_admitted=True, reason="action_out_of_scope"),
            dict(account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", notional="1001", risk_admitted=True, reason="notional_out_of_scope"),
            dict(account_id="paper-1", environment="PAPER", instrument_id=INSTRUMENT_ID, instrument_version=1, action="ORDER.SUBMIT", notional="10", risk_admitted=False, reason="risk_rejected"),
        ]
        for index, case in enumerate(cases):
            with self.subTest(reason=case["reason"]):
                reason = case.pop("reason")
                record = service._admit_unverified(
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
        first = service._admit_unverified(**kwargs)
        second = service._admit_unverified(**kwargs)
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
        service._admit_unverified(**base)
        with self.assertRaisesRegex(Exception, "admission_id"):
            service._admit_unverified(**{**base, "notional": "101"})
        with self.assertRaisesRegex(Exception, "admission_id"):
            service._admit_unverified(**{**base, "instrument_version": 2})



    def test_durable_versioned_admission_and_confirmation_survive_restart(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            service = AuthorityService(store)
            service.register_policy(policy(environments={"SIMULATION"}))
            service.add_confirmation(
                confirmation_id="c-durable",
                policy_id="p1",
                intent_hash="h-durable",
                account_id="paper-1",
                environment="SIMULATION",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                expires_at="2026-09-24T23:00:00Z",
            )
            admitted = service._admit_unverified(
                admission_id="a-durable", policy_id="p1", intent_hash="h-durable",
                account_id="paper-1", environment="SIMULATION",
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
                    account_id="paper-1", environment="SIMULATION",
                    instrument_id=INSTRUMENT_ID, instrument_version=1,
                    action="ORDER.SUBMIT", now="2026-09-24T18:01:00Z",
                ),
                (True, "allowed"),
            )
            self.assertEqual(
                restarted.dispatch_allowed(
                    "a-durable", intent_hash="h-durable",
                    account_id="paper-1", environment="SIMULATION",
                    instrument_id=INSTRUMENT_ID, instrument_version=2,
                    action="ORDER.SUBMIT", now="2026-09-24T18:01:00Z",
                ),
                (False, "admission_scope_changed"),
            )
            reused = restarted._admit_unverified(
                admission_id="a-durable-2", policy_id="p1", intent_hash="h-durable",
                account_id="paper-1", environment="SIMULATION",
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
            service.register_policy(policy(autonomous=True, environments={"SIMULATION"}))
            service._admit_unverified(
                admission_id="a-revoke", policy_id="p1", intent_hash="h-revoke",
                account_id="paper-1", environment="SIMULATION",
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
                    account_id="paper-1", environment="SIMULATION",
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
            authority.register_policy(policy(autonomous=True, environments={"SIMULATION"}))
            admitted = authority._admit_unverified(
                admission_id="a-guard", policy_id="p1", intent_hash="h-guard",
                account_id="paper-1", environment="SIMULATION",
                instrument_id=INSTRUMENT_ID, instrument_version=1,
                action="ORDER.SUBMIT", notional="100", state_version=1,
                risk_admitted=True, now="2026-09-24T18:00:00Z",
            )
            self.assertEqual(admitted.outcome, "ADMITTED")
            guard = authority.dispatch_guard(
                "a-guard",
                account_id="paper-1",
                environment="SIMULATION",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
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
                sender_check=lambda _owner_token, _owner_epoch: None,
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
                    "aggregate_version": "2",
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
                    "aggregate_version": "2",
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
            service.register_policy(policy(environments={"SIMULATION"}))
            service.add_confirmation(
                confirmation_id="single-use",
                policy_id="p1",
                intent_hash="same-intent",
                account_id="paper-1",
                environment="SIMULATION",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                expires_at="2026-09-24T23:00:00Z",
            )
            first = service._admit_unverified(
                admission_id="first-use",
                policy_id="p1",
                intent_hash="same-intent",
                account_id="paper-1",
                environment="SIMULATION",
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
                    "aggregate_version": "4",
                    "payload": duplicate,
                    "payload_hash": payload_digest(duplicate),
                    "committed_at": "2026-09-24T18:00:01Z",
                }
            )
            with self.assertRaisesRegex(
                Exception, "consumed by multiple admissions"
            ):
                AuthorityService(store)


    def test_stale_process_cannot_authorize_after_other_process_revokes(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            process_a = AuthorityService(store)
            process_a.register_policy(policy(autonomous=True, environments={"SIMULATION"}))
            process_a._admit_unverified(
                admission_id="a-stale-read",
                policy_id="p1",
                intent_hash="h-stale-read",
                account_id="paper-1",
                environment="SIMULATION",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                state_version=1,
                risk_admitted=True,
                now="2026-09-24T18:00:00Z",
            )
            process_b = AuthorityService(store)
            process_b.revoke_policy(
                "p1",
                reason="operator revoke",
                revoked_at="2026-09-24T18:01:00Z",
            )

            self.assertEqual(
                process_a.dispatch_allowed(
                    "a-stale-read",
                    intent_hash="h-stale-read",
                    account_id="paper-1",
                    environment="SIMULATION",
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=1,
                    action="ORDER.SUBMIT",
                    now="2026-09-24T18:02:00Z",
                ),
                (False, "authority_state_stale"),
            )


    def test_final_dispatch_barrier_blocks_stale_process_after_external_revoke(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            process_a = AuthorityService(store)
            process_a.register_policy(policy(autonomous=True, environments={"SIMULATION"}))
            admitted = process_a._admit_unverified(
                admission_id="a-stale-guard",
                policy_id="p1",
                intent_hash="h-stale-guard",
                account_id="paper-1",
                environment="SIMULATION",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                state_version=1,
                risk_admitted=True,
                now="2026-09-24T18:00:00Z",
            )
            self.assertEqual(admitted.outcome, "ADMITTED")
            process_b = AuthorityService(store)
            guard = process_a.dispatch_guard(
                "a-stale-guard",
                account_id="paper-1",
                environment="SIMULATION",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="paper-1",
                owner_token="owner-stale-guard",
            )
            outbound = 0

            def transport(client_id, request, final_guard):
                nonlocal outbound
                process_b.revoke_policy(
                    "p1",
                    reason="operator revoke",
                    revoked_at="2026-09-24T18:00:01Z",
                )
                final_guard()
                outbound += 1
                return {"provider_order_id": "must-not-happen"}

            result = dispatcher.dispatch(
                attempt_id="attempt-stale-guard",
                intent_id="intent-stale-guard",
                intent_hash="h-stale-guard",
                provider="sim",
                request={},
                now="2026-09-24T18:00:00Z",
                authority_check=guard,
                transport_send=transport,
                final_barrier_clock=lambda: "2026-09-24T18:00:02Z",
                sender_check=lambda _owner_token, _owner_epoch: None,
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "authority_state_stale")
            self.assertEqual(outbound, 0)


    def test_stale_authority_process_cannot_double_consume_confirmation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            seed = AuthorityService(store)
            seed.register_policy(policy(environments={"SIMULATION"}))
            seed.add_confirmation(
                confirmation_id="concurrent-single-use",
                policy_id="p1",
                intent_hash="h-concurrent",
                account_id="paper-1",
                environment="SIMULATION",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                expires_at="2026-09-24T23:00:00Z",
            )

            first = AuthorityService(store)
            stale = AuthorityService(store)

            admitted = first._admit_unverified(
                admission_id="winner",
                policy_id="p1",
                intent_hash="h-concurrent",
                account_id="paper-1",
                environment="SIMULATION",
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
                stale._admit_unverified(
                    admission_id="stale-loser",
                    policy_id="p1",
                    intent_hash="h-concurrent",
                    account_id="paper-1",
                    environment="SIMULATION",
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
                    environment="SIMULATION",
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
            seed.register_policy(
                policy(autonomous=True, environments={"SIMULATION"})
            )

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
                stale._admit_unverified(
                    admission_id="stale-after-revoke",
                    policy_id="p1",
                    intent_hash="h-stale",
                    account_id="paper-1",
                    environment="SIMULATION",
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


    def test_store_backed_unverified_paper_admission_cannot_persist(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            service = AuthorityService(store)
            service.register_policy(policy(autonomous=True))

            with self.assertRaisesRegex(
                AuthorityConflict,
                "unverified PAPER/LIVE admission",
            ):
                service._admit_unverified(
                    admission_id="legacy-direct",
                    policy_id="p1",
                    intent_hash="legacy-direct-intent",
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

            admission_events = [
                event
                for event in store.load_events("authority_state", "canonical")
                if event["event_type"] == "AuthorityAdmissionRecorded"
            ]
            self.assertEqual(admission_events, [])
            restarted = AuthorityService(JournalStore(path))
            self.assertEqual(
                restarted.dispatch_allowed(
                    "legacy-direct",
                    intent_hash="legacy-direct-intent",
                    account_id="paper-1",
                    environment="PAPER",
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=1,
                    action="ORDER.SUBMIT",
                    now="2026-09-24T18:01:00Z",
                ),
                (False, "admission_missing"),
            )

    def test_legacy_paper_admission_is_readable_but_never_dispatchable(self):
        service = AuthorityService()
        service.register_policy(policy(autonomous=True))
        legacy = service._admit_unverified(
            admission_id="legacy-readable",
            policy_id="p1",
            intent_hash="legacy-readable-intent",
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
        self.assertEqual(legacy.outcome, "ADMITTED")
        self.assertIsNone(legacy.risk_decision_id)
        self.assertEqual(
            service.dispatch_allowed(
                "legacy-readable",
                intent_hash="legacy-readable-intent",
                account_id="paper-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                now="2026-09-24T18:01:00Z",
            ),
            (False, "financial_evidence_missing"),
        )

    def test_public_financial_admission_commit_failure_leaves_transaction_a_clean(self):
        class FailingFinancialJournalStore(JournalStore):
            def __init__(self, path):
                super().__init__(path)
                self.prepared_event_types = ()

            def commit_command(self, **kwargs):
                self.prepared_event_types = tuple(
                    envelope["event_type"]
                    for envelope, _topic in kwargs["events"]
                )
                raise RuntimeError("injected financial commit failure")

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = FailingFinancialJournalStore(path)
            authority = AuthorityService(store)
            item = policy()
            authority.register_policy(item)
            confirmation_id = "confirm-transaction-a-fault"
            authority.add_confirmation(
                confirmation_id=confirmation_id,
                policy_id=item.policy_id,
                intent_hash=PUBLIC_INTENT_HASH,
                account_id="paper-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                expires_at="2026-09-24T23:00:00Z",
            )
            reservations = DurableReservationBook(
                store,
                environment="PAPER",
                account_id="paper-1",
            )
            kwargs = dict(
                command_id="cmd-public-fault",
                idempotency_key="idem-public-fault",
                admission_id="admission-public-fault",
                policy_id=item.policy_id,
                intent_id="intent-public-fault",
                account_id="paper-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                reservation_id="reservation-public-fault",
                confirmation_id=confirmation_id,
                **public_financial_kwargs(store),
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "injected financial commit failure",
            ):
                authority.admit(
                    reservation_book=reservations,
                    **kwargs,
                )

            self.assertEqual(
                store.prepared_event_types,
                (
                    "RiskDecisionRecorded",
                    "ReservationMutationCommitted",
                    "AuthorityAdmissionRecorded",
                ),
            )
            self.assertEqual(reservations.version, 0)
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("0"),
            )
            self.assertEqual(store.pending_outbox(), [])
            self.assertNotIn(confirmation_id, authority._used_confirmations)
            self.assertEqual(
                authority.dispatch_allowed(
                    "admission-public-fault",
                    intent_hash=PUBLIC_INTENT_HASH,
                    account_id="paper-1",
                    environment="PAPER",
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=1,
                    action="ORDER.SUBMIT",
                    now="2026-09-24T18:02:00Z",
                ),
                (False, "admission_missing"),
            )
            with store._connect() as connection:
                financial_events = connection.execute(
                    """
                    SELECT event_type
                    FROM events
                    WHERE event_type IN (
                        'RiskDecisionRecorded',
                        'ReservationMutationCommitted',
                        'AuthorityAdmissionRecorded'
                    )
                    ORDER BY event_type
                    """
                ).fetchall()
                command_count = connection.execute(
                    "SELECT COUNT(*) FROM command_dedupe"
                ).fetchone()[0]
            self.assertEqual(financial_events, [])
            self.assertEqual(command_count, 0)

            # Restart from durable truth: the failed process must leave no
            # half-admission, and the same persisted confirmation remains usable.
            restarted_store = JournalStore(path)
            restarted_authority = AuthorityService(restarted_store)
            restarted_reservations = DurableReservationBook(
                restarted_store,
                environment="PAPER",
                account_id="paper-1",
            )
            self.assertEqual(restarted_reservations.version, 0)
            self.assertEqual(
                restarted_reservations.total_reserved("CASH:USD"),
                Decimal("0"),
            )
            self.assertNotIn(
                confirmation_id,
                restarted_authority._used_confirmations,
            )

            admitted = restarted_authority.admit(
                reservation_book=restarted_reservations,
                **kwargs,
            )
            self.assertEqual(admitted.outcome, "ADMITTED")
            self.assertEqual(
                restarted_reservations.total_reserved("CASH:USD"),
                Decimal("100"),
            )
            self.assertEqual(len(restarted_store.pending_outbox()), 1)
            with restarted_store._connect() as connection:
                counts = {
                    event_type: connection.execute(
                        "SELECT COUNT(*) FROM events WHERE event_type = ?",
                        (event_type,),
                    ).fetchone()[0]
                    for event_type in (
                        "RiskDecisionRecorded",
                        "ReservationMutationCommitted",
                        "AuthorityAdmissionRecorded",
                    )
                }
                command_count = connection.execute(
                    "SELECT COUNT(*) FROM command_dedupe"
                ).fetchone()[0]
            self.assertEqual(
                counts,
                {
                    "RiskDecisionRecorded": 1,
                    "ReservationMutationCommitted": 1,
                    "AuthorityAdmissionRecorded": 1,
                },
            )
            self.assertEqual(command_count, 1)


    def test_public_financial_admission_is_atomic_and_restart_idempotent(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            item = policy(autonomous=True)
            authority.register_policy(item)
            reservations = DurableReservationBook(
                store,
                environment="PAPER",
                account_id="paper-1",
            )
            kwargs = dict(
                command_id="cmd-public-admit",
                idempotency_key="idem-public-admit",
                admission_id="admission-public",
                policy_id=item.policy_id,
                intent_id="intent-public",
                account_id="paper-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                reservation_id="reservation-public",
                **public_financial_kwargs(store),
            )
            first = authority.admit(
                reservation_book=reservations,
                **kwargs,
            )
            self.assertEqual(first.outcome, "ADMITTED")
            self.assertTrue(first.risk_decision_id.startswith("risk:sha256:"))
            self.assertEqual(first.reservation_id, "reservation-public")
            self.assertEqual(
                reservations.total_reserved("CASH:USD"),
                Decimal("100"),
            )
            self.assertEqual(len(store.pending_outbox()), 1)

            # Lost-response replay re-evaluates the typed risk inputs against
            # immutable original evidence; it never reserves or publishes twice.
            restarted_authority = AuthorityService(store)
            restarted_reservations = DurableReservationBook(
                store,
                environment="PAPER",
                account_id="paper-1",
            )
            replayed = restarted_authority.admit(
                reservation_book=restarted_reservations,
                **kwargs,
            )
            self.assertEqual(replayed, first)
            self.assertEqual(restarted_reservations.version, 1)
            self.assertEqual(
                restarted_reservations.total_reserved("CASH:USD"),
                Decimal("100"),
            )
            self.assertEqual(len(store.pending_outbox()), 1)
            self.assertEqual(
                len(store.load_events("risk_decision", first.risk_decision_id)),
                1,
            )
            self.assertEqual(
                len(store.load_events("authority_state", "canonical")),
                2,
            )


    def test_public_admit_has_no_preapproved_risk_decision_escape_hatch(self):
        import inspect

        parameters = inspect.signature(AuthorityService.admit).parameters
        self.assertIn("risk_intent", parameters)
        self.assertIn("risk_context", parameters)
        self.assertIn("risk_policy", parameters)
        self.assertNotIn("risk_admitted", parameters)
        self.assertNotIn("risk_decision", parameters)


    def test_public_financial_admission_risk_rejection_does_not_reserve_or_publish(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            item = policy(autonomous=True)
            authority.register_policy(item)
            reservations = DurableReservationBook(
                store,
                environment="PAPER",
                account_id="paper-1",
            )
            rejected = authority.admit(
                command_id="cmd-risk-reject",
                idempotency_key="idem-risk-reject",
                admission_id="admission-risk-reject",
                policy_id=item.policy_id,
                intent_id="intent-risk-reject",
                account_id="paper-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                reservation_book=reservations,
                reservation_id="reservation-risk-reject",
                **public_financial_kwargs(
                    store,
                    risk_policy=public_risk_policy(max_single_notional="50")
                ),
            )
            self.assertEqual(rejected.outcome, "REJECTED")
            self.assertEqual(rejected.reason, "risk_rejected")
            self.assertIsNone(rejected.reservation_id)
            self.assertEqual(reservations.version, 0)
            self.assertEqual(reservations.total_reserved("CASH:USD"), Decimal("0"))
            self.assertEqual(store.pending_outbox(), [])
            self.assertEqual(
                len(store.load_events("risk_decision", rejected.risk_decision_id)),
                1,
            )


    def test_public_financial_admission_stale_state_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            item = policy(autonomous=True)
            authority.register_policy(item)
            reservations = DurableReservationBook(
                store, environment="PAPER", account_id="paper-1"
            )
            rejected = authority.admit(
                command_id="cmd-stale-state",
                idempotency_key="idem-stale-state",
                admission_id="admission-stale-state",
                policy_id=item.policy_id,
                intent_id="intent-stale-state",
                account_id="paper-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                reservation_book=reservations,
                reservation_id="reservation-stale-state",
                **public_financial_kwargs(
                    store,
                    risk_intent=public_risk_intent(expected_state_version=6),
                    risk_context=public_risk_context(state_version=7),
                ),
            )
            self.assertEqual(rejected.outcome, "REJECTED")
            self.assertEqual(rejected.reason, "risk_rejected")
            self.assertEqual(reservations.version, 0)
            self.assertEqual(store.pending_outbox(), [])


    def test_public_financial_admission_replay_rejects_changed_reservation_delta(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            item = policy(autonomous=True)
            authority.register_policy(item)
            reservations = DurableReservationBook(
                store,
                environment="PAPER",
                account_id="paper-1",
            )
            kwargs = dict(
                command_id="cmd-retry-delta",
                idempotency_key="idem-retry-delta",
                admission_id="admission-retry-delta",
                policy_id=item.policy_id,
                intent_id="intent-retry-delta",
                account_id="paper-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                reservation_id="reservation-retry-delta",
                **public_financial_kwargs(store),
            )
            first = authority.admit(
                reservation_book=reservations,
                **kwargs,
            )
            restarted = AuthorityService(store)
            restarted_book = DurableReservationBook(
                store,
                environment="PAPER",
                account_id="paper-1",
            )
            with self.assertRaisesRegex(
                AuthorityConflict,
                "another financial command",
            ):
                restarted.admit(
                    reservation_book=restarted_book,
                    **{
                        **kwargs,
                        "reservation_requirements": {"CASH:USD": "99"},
                    },
                )
            self.assertEqual(
                restarted_book.total_reserved("CASH:USD"),
                Decimal("100"),
            )
            self.assertEqual(restarted_book.version, 1)
            self.assertEqual(len(store.pending_outbox()), 1)
            self.assertEqual(
                len(store.load_events("risk_decision", first.risk_decision_id)),
                1,
            )


    def test_public_financial_admission_replay_rejects_changed_scope(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            item = policy(autonomous=True)
            authority.register_policy(item)
            reservations = DurableReservationBook(
                store,
                environment="PAPER",
                account_id="paper-1",
            )
            kwargs = dict(
                command_id="cmd-replay-scope",
                idempotency_key="idem-replay-scope",
                admission_id="admission-replay-scope",
                policy_id=item.policy_id,
                intent_id="intent-replay-scope",
                account_id="paper-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                reservation_id="reservation-replay-scope",
                **public_financial_kwargs(store),
            )
            authority.admit(reservation_book=reservations, **kwargs)
            restarted = AuthorityService(store)
            restarted_book = DurableReservationBook(
                store,
                environment="PAPER",
                account_id="paper-1",
            )
            with self.assertRaisesRegex(
                AuthorityConflict,
                "another financial command",
            ):
                restarted.admit(
                    reservation_book=restarted_book,
                    **{**kwargs, "notional": "101"},
                )
            self.assertEqual(
                restarted_book.total_reserved("CASH:USD"),
                Decimal("100"),
            )
            self.assertEqual(len(store.pending_outbox()), 1)


    def test_internal_bound_risk_commit_rejects_stale_reservation_cut(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            item = policy(autonomous=True)
            authority.register_policy(item)
            reservations = DurableReservationBook(
                store, environment="PAPER", account_id="paper-1"
            )
            stale = bound_risk_decision(
                policy_version=item.version,
                reservation_version=reservations.version,
            )
            reservations.reserve(
                command_id="other-command",
                idempotency_key="other-command",
                reservation_id="other-reservation",
                intent_id="other-intent",
                requirements={"CASH:USD": "1"},
                available={"CASH:USD": "1000"},
            )
            with self.assertRaisesRegex(
                AuthorityConflict, "reservation_version is stale"
            ):
                authority._admit_bound_risk(
                    command_id="stale-command",
                    idempotency_key="stale-command",
                    admission_id="stale-admission",
                    policy_id=item.policy_id,
                    intent_id="intent-stale",
                    intent_hash=stale.intent_hash,
                    account_id="paper-1",
                    environment="PAPER",
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=1,
                    action="ORDER.SUBMIT",
                    notional="100",
                    current_state_version=7,
                    capability_snapshot_id=stale.capability_snapshot_id,
                    risk_decision=stale,
                    reservation_book=reservations,
                    reservation_id="stale-reservation",
                    reservation_requirements={"CASH:USD": "100"},
                    reservation_available={"CASH:USD": "1000"},
                    journal_sequence_cut=store.current_journal_sequence(),
                    now="2026-09-24T18:01:00Z",
                )


    def test_public_dispatch_rechecks_capability_and_reservation_state(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            item = policy(autonomous=True)
            authority.register_policy(item)
            reservations = DurableReservationBook(
                store, environment="PAPER", account_id="paper-1"
            )
            admitted = authority.admit(
                command_id="dispatch-command",
                idempotency_key="dispatch-command",
                admission_id="dispatch-admission",
                policy_id=item.policy_id,
                intent_id="dispatch-intent",
                account_id="paper-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                reservation_book=reservations,
                reservation_id="dispatch-reservation",
                **public_financial_kwargs(store),
            )
            common = dict(
                intent_hash=PUBLIC_INTENT_HASH,
                account_id="paper-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                now="2026-09-24T18:01:15Z",
            )
            self.assertEqual(
                authority.dispatch_allowed(admitted.admission_id, **common),
                (False, "capability_snapshot_required"),
            )
            self.assertEqual(
                authority.dispatch_allowed(
                    admitted.admission_id,
                    capability_snapshot_id="different-capability",
                    **common,
                ),
                (False, "capability_snapshot_changed"),
            )
            self.assertEqual(
                authority.dispatch_allowed(
                    admitted.admission_id,
                    capability_snapshot_id=PUBLIC_CAPABILITY_SNAPSHOT_ID,
                    **common,
                ),
                (True, "allowed"),
            )
            reservations.mark_unknown(
                command_id="dispatch-unknown",
                idempotency_key="dispatch-unknown",
                reservation_id="dispatch-reservation",
            )
            self.assertEqual(
                authority.dispatch_allowed(
                    admitted.admission_id,
                    capability_snapshot_id=PUBLIC_CAPABILITY_SNAPSHOT_ID,
                    **common,
                ),
                (False, "reservation_not_dispatchable"),
            )


    def test_public_dispatch_blocks_after_other_reservation_advances_book(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            item = policy(autonomous=True)
            authority.register_policy(item)
            reservations = DurableReservationBook(
                store, environment="PAPER", account_id="paper-1"
            )
            admitted = authority.admit(
                command_id="first-command",
                idempotency_key="first-command",
                admission_id="first-admission",
                policy_id=item.policy_id,
                intent_id="first-intent",
                account_id="paper-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                reservation_book=reservations,
                reservation_id="first-reservation",
                **public_financial_kwargs(store),
            )
            reservations.reserve(
                command_id="second-command",
                idempotency_key="second-command",
                reservation_id="second-reservation",
                intent_id="second-intent",
                requirements={"CASH:USD": "1"},
                available={"CASH:USD": "1000"},
            )
            self.assertEqual(
                authority.dispatch_allowed(
                    admitted.admission_id,
                    intent_hash=PUBLIC_INTENT_HASH,
                    account_id="paper-1",
                    environment="PAPER",
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=1,
                    action="ORDER.SUBMIT",
                    now="2026-09-24T18:01:15Z",
                    capability_snapshot_id=PUBLIC_CAPABILITY_SNAPSHOT_ID,
                ),
                (False, "reservation_state_changed"),
            )


if __name__ == "__main__":
    unittest.main()
