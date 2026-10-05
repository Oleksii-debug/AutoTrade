from __future__ import annotations

from datetime import datetime, timedelta, timezone
import inspect
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.authority import (
    AuthorityPolicy,
    AuthorityService,
    InstrumentVersionIdentity,
)
from mvp.autotrade_mvp.confirm_intent import confirm_pending_intent
from mvp.autotrade_mvp.confirmed_pending_intent import (
    ConfirmedPendingIntentResolutionError,
    resolve_confirmed_pending_intent,
)
from mvp.autotrade_mvp.pending_intent_financial_binding import (
    DurablePendingIntentFinancialBindingRegistry,
)
from mvp.autotrade_mvp.pending_intents import DurablePendingIntentRegistry
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.risk import RiskIntent, RiskPolicy
from mvp.autotrade_mvp.risk_policy_authority import (
    DurableRiskPolicyRegistry,
    RiskPolicyScope,
)


NOW = datetime(2026, 10, 5, 1, 30, tzinfo=timezone.utc)
INSTRUMENT_ID = "11111111-1111-1111-1111-111111111111"
PENDING_ID = "pending-resolve-confirmed-1"
CONFIRMATION_ID = "confirm-resolve-confirmed-1"


class ConfirmedPendingIntentResolutionTests(unittest.TestCase):
    @staticmethod
    def _risk_policy(*, max_single_notional: str = "1000") -> RiskPolicy:
        return RiskPolicy.create(
            max_abs_position="100",
            max_single_notional=max_single_notional,
            max_gross_leverage="5",
            max_net_leverage="5",
            max_daily_loss="500",
            max_drawdown_fraction="0.5",
            max_data_age_seconds="30",
            max_fx_age_seconds="30",
            min_margin_headroom="0.1",
            max_stress_loss="500",
            allowed_actions=["TRADE"],
        )

    @staticmethod
    def _scope() -> RiskPolicyScope:
        return RiskPolicyScope(
            provider_id="TEST",
            account_id="acct-1",
            environment="PAPER",
            provider_environment="PAPER",
            entity_policy_id="entity-policy-1",
            instrument_family="SPOT",
        )

    def _setup(self, store: JournalStore):
        authority = AuthorityService(store)
        authority.register_policy(
            AuthorityPolicy.create(
                policy_id="authority-policy-1",
                account_id="acct-1",
                environments={"PAPER"},
                instruments={InstrumentVersionIdentity(INSTRUMENT_ID, 9)},
                actions={"ORDER.SUBMIT"},
                max_notional="1000",
                valid_from=(NOW - timedelta(minutes=5)).isoformat().replace(
                    "+00:00", "Z"
                ),
                expires_at=(NOW + timedelta(hours=1)).isoformat().replace(
                    "+00:00", "Z"
                ),
                autonomous=False,
                protection_only=False,
                version=3,
            )
        )
        risk_registry = DurableRiskPolicyRegistry(store)
        scope = self._scope()
        risk_registry.register(
            scope=scope,
            policy_id="risk-policy-1",
            version=1,
            policy=self._risk_policy(),
            committed_at=NOW - timedelta(seconds=4),
        )
        risk_registry.activate(
            scope=scope,
            policy_id="risk-policy-1",
            version=1,
            committed_at=NOW - timedelta(seconds=3),
        )
        pending_registry = DurablePendingIntentRegistry(store)
        pending = pending_registry.register(
            pending_intent_id=PENDING_ID,
            account_id="acct-1",
            environment="PAPER",
            policy_id="authority-policy-1",
            authority_policy_version=3,
            instrument_id=INSTRUMENT_ID,
            instrument_version=9,
            authority_action="ORDER.SUBMIT",
            notional="202.50",
            risk_intent=RiskIntent.create(
                symbol="BTCUSD",
                side="BUY",
                quantity="2",
                price="101.25",
                expected_state_version=7,
                reduce_only=False,
                action="TRADE",
                instrument_type="SPOT",
            ),
            registered_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=10),
        )
        binding = DurablePendingIntentFinancialBindingRegistry(store).bind(
            pending_registry,
            pending_intent_id=PENDING_ID,
            account_id="acct-1",
            environment="PAPER",
            authority_policy_id="authority-policy-1",
            authority_policy_version=3,
            resolved_risk_policy=risk_registry.resolve_current(scope),
            reservation_requirements={"CASH:USD": "202.50"},
            at=NOW,
        )
        return pending_registry, risk_registry, scope, pending, binding

    def test_restart_recovers_exact_confirmed_server_owned_intent(self) -> None:
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            _pending_registry, _risk_registry, _scope, pending, binding = self._setup(store)
            confirmed = confirm_pending_intent(
                store,
                pending_intent_id=PENDING_ID,
                confirmation_id=CONFIRMATION_ID,
                actor_id="owner-1",
                account_id="acct-1",
                environment="PAPER",
                accepted_at=NOW,
            )

            resolved = resolve_confirmed_pending_intent(
                JournalStore(path),
                pending_intent_id=PENDING_ID,
                at=NOW + timedelta(seconds=1),
            )
            self.assertEqual(resolved.confirmation_id, CONFIRMATION_ID)
            self.assertEqual(resolved.actor_id, "owner-1")
            self.assertEqual(resolved.pending_intent, pending)
            self.assertEqual(resolved.financial_binding, binding)
            self.assertEqual(resolved, confirmed)

    def test_claim_without_authority_confirmation_is_not_resolved(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            pending_registry, _risk_registry, _scope, _pending, _binding = self._setup(store)
            pending_registry.claim_confirmation(
                PENDING_ID,
                confirmation_id=CONFIRMATION_ID,
                actor_id="owner-1",
                account_id="acct-1",
                environment="PAPER",
                policy_id="authority-policy-1",
                authority_policy_version=3,
                at=NOW,
            )

            with self.assertRaisesRegex(
                ConfirmedPendingIntentResolutionError,
                "no AuthorityConfirmationAdded proof",
            ):
                resolve_confirmed_pending_intent(
                    store,
                    pending_intent_id=PENDING_ID,
                    at=NOW + timedelta(seconds=1),
                )

    def test_risk_policy_activation_drift_invalidates_recovery(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            _pending_registry, risk_registry, scope, _pending, _binding = self._setup(store)
            confirm_pending_intent(
                store,
                pending_intent_id=PENDING_ID,
                confirmation_id=CONFIRMATION_ID,
                actor_id="owner-1",
                account_id="acct-1",
                environment="PAPER",
                accepted_at=NOW,
            )
            risk_registry.register(
                scope=scope,
                policy_id="risk-policy-2",
                version=2,
                policy=self._risk_policy(max_single_notional="900"),
                committed_at=NOW + timedelta(seconds=2),
            )
            risk_registry.activate(
                scope=scope,
                policy_id="risk-policy-2",
                version=2,
                committed_at=NOW + timedelta(seconds=3),
            )

            with self.assertRaisesRegex(
                ConfirmedPendingIntentResolutionError,
                "current quantitative risk authority",
            ):
                resolve_confirmed_pending_intent(
                    store,
                    pending_intent_id=PENDING_ID,
                    at=NOW + timedelta(seconds=4),
                )

    def test_resolution_api_has_no_financial_override_parameters(self) -> None:
        signature = inspect.signature(resolve_confirmed_pending_intent)
        self.assertEqual(
            tuple(signature.parameters),
            ("store", "pending_intent_id", "at"),
        )
        for forbidden in (
            "risk_intent",
            "quantity",
            "price",
            "notional",
            "risk_policy",
            "reservation_requirements",
            "account_id",
            "environment",
            "policy_id",
            "confirmation_id",
            "actor_id",
        ):
            self.assertNotIn(forbidden, signature.parameters)


if __name__ == "__main__":
    unittest.main()
