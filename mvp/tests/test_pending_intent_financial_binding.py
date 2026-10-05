from __future__ import annotations

from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.pending_intent_financial_binding import (
    DurablePendingIntentFinancialBindingRegistry,
    PendingIntentFinancialBinding,
    PendingIntentFinancialBindingError,
)
from mvp.autotrade_mvp.pending_intents import DurablePendingIntentRegistry
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.risk import RiskIntent, RiskPolicy
from mvp.autotrade_mvp.risk_policy_authority import (
    DurableRiskPolicyRegistry,
    RiskPolicyScope,
)


NOW = datetime(2026, 10, 5, 0, 20, tzinfo=timezone.utc)
INSTRUMENT_ID = "11111111-1111-1111-1111-111111111111"


class PendingIntentFinancialBindingTests(unittest.TestCase):
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

    @staticmethod
    def _register_pending(
        store: JournalStore,
        *,
        pending_id: str = "pending-bind-1",
    ) -> DurablePendingIntentRegistry:
        registry = DurablePendingIntentRegistry(store)
        registry.register(
            pending_intent_id=pending_id,
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
            registered_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        return registry

    def _register_risk_policy(
        self,
        store: JournalStore,
        *,
        version: int = 1,
        max_single_notional: str = "1000",
        activate_at: datetime | None = None,
    ):
        registry = DurableRiskPolicyRegistry(store)
        scope = self._scope()
        registry.register(
            scope=scope,
            policy_id="risk-policy-1",
            version=version,
            policy=self._risk_policy(max_single_notional=max_single_notional),
            committed_at=(activate_at or NOW) - timedelta(seconds=2),
        )
        registry.activate(
            scope=scope,
            policy_id="risk-policy-1",
            version=version,
            committed_at=(activate_at or NOW) - timedelta(seconds=1),
        )
        return registry, scope

    def test_binding_survives_restart_and_preserves_exact_financial_envelope(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            risk_registry, scope = self._register_risk_policy(store)
            pending_registry = self._register_pending(store)
            resolved = risk_registry.resolve_current(scope)
            bindings = DurablePendingIntentFinancialBindingRegistry(store)

            created = bindings.bind(
                pending_registry,
                pending_intent_id="pending-bind-1",
                account_id="acct-1",
                environment="PAPER",
                authority_policy_id="authority-policy-1",
                authority_policy_version=3,
                resolved_risk_policy=resolved,
                reservation_requirements={"CASH:USD": "202.50"},
                at=NOW + timedelta(seconds=1),
            )

            restarted_store = JournalStore(store.path)
            restarted_risk = DurableRiskPolicyRegistry(restarted_store)
            restarted_resolved = restarted_risk.resolve_current(scope)
            restarted = DurablePendingIntentFinancialBindingRegistry(restarted_store)
            reloaded = restarted.resolve_current(
                created.pending_intent_id,
                resolved_risk_policy=restarted_resolved,
                reservation_requirements={"CASH:USD": "202.50"},
            )

            self.assertEqual(reloaded, created)
            self.assertEqual(reloaded.risk_policy_id, "risk-policy-1")
            self.assertEqual(reloaded.risk_policy_version, 1)
            self.assertEqual(
                dict(reloaded.reservation_requirements),
                {"CASH:USD": "202.5"},
            )
            self.assertTrue(reloaded.risk_policy_content_digest.startswith("sha256:"))
            self.assertTrue(reloaded.binding_hash.startswith("sha256:"))

    def test_exact_retry_is_idempotent_but_rebinding_requirements_conflicts(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            risk_registry, scope = self._register_risk_policy(store)
            pending_registry = self._register_pending(store)
            resolved = risk_registry.resolve_current(scope)
            bindings = DurablePendingIntentFinancialBindingRegistry(store)
            kwargs = dict(
                pending_intent_id="pending-bind-1",
                account_id="acct-1",
                environment="PAPER",
                authority_policy_id="authority-policy-1",
                authority_policy_version=3,
                resolved_risk_policy=resolved,
                reservation_requirements={"CASH:USD": "202.50"},
                at=NOW + timedelta(seconds=1),
            )
            first = bindings.bind(pending_registry, **kwargs)
            retry = bindings.bind(pending_registry, **kwargs)
            self.assertEqual(retry, first)

            changed = dict(kwargs)
            changed["reservation_requirements"] = {"CASH:USD": "203"}
            with self.assertRaisesRegex(
                PendingIntentFinancialBindingError,
                "conflicts with durable state",
            ):
                bindings.bind(pending_registry, **changed)

    def test_current_risk_policy_drift_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            risk_registry, scope = self._register_risk_policy(store)
            pending_registry = self._register_pending(store)
            resolved_v1 = risk_registry.resolve_current(scope)
            bindings = DurablePendingIntentFinancialBindingRegistry(store)
            created = bindings.bind(
                pending_registry,
                pending_intent_id="pending-bind-1",
                account_id="acct-1",
                environment="PAPER",
                authority_policy_id="authority-policy-1",
                authority_policy_version=3,
                resolved_risk_policy=resolved_v1,
                reservation_requirements={"CASH:USD": "202.50"},
                at=NOW + timedelta(seconds=1),
            )

            risk_registry.register(
                scope=scope,
                policy_id="risk-policy-1",
                version=2,
                policy=self._risk_policy(max_single_notional="900"),
                committed_at=NOW + timedelta(seconds=2),
            )
            risk_registry.activate(
                scope=scope,
                policy_id="risk-policy-1",
                version=2,
                committed_at=NOW + timedelta(seconds=3),
            )
            resolved_v2 = risk_registry.resolve_current(scope)

            with self.assertRaisesRegex(
                PendingIntentFinancialBindingError,
                "risk policy no longer matches",
            ):
                bindings.resolve_current(
                    created.pending_intent_id,
                    resolved_risk_policy=resolved_v2,
                    reservation_requirements={"CASH:USD": "202.50"},
                )

    def test_current_reservation_drift_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            risk_registry, scope = self._register_risk_policy(store)
            pending_registry = self._register_pending(store)
            resolved = risk_registry.resolve_current(scope)
            bindings = DurablePendingIntentFinancialBindingRegistry(store)
            bindings.bind(
                pending_registry,
                pending_intent_id="pending-bind-1",
                account_id="acct-1",
                environment="PAPER",
                authority_policy_id="authority-policy-1",
                authority_policy_version=3,
                resolved_risk_policy=resolved,
                reservation_requirements={"CASH:USD": "202.50"},
                at=NOW + timedelta(seconds=1),
            )

            with self.assertRaisesRegex(
                PendingIntentFinancialBindingError,
                "reservation requirements no longer match",
            ):
                bindings.resolve_current(
                    "pending-bind-1",
                    resolved_risk_policy=resolved,
                    reservation_requirements={"CASH:USD": "203"},
                )

    def test_cross_store_risk_policy_injection_is_rejected(self) -> None:
        with TemporaryDirectory() as first, TemporaryDirectory() as second:
            store = JournalStore(f"{first}/journal.sqlite3")
            pending_registry = self._register_pending(store)
            other_store = JournalStore(f"{second}/journal.sqlite3")
            other_risk_registry, scope = self._register_risk_policy(other_store)
            other_resolved = other_risk_registry.resolve_current(scope)
            bindings = DurablePendingIntentFinancialBindingRegistry(store)

            with self.assertRaisesRegex(
                PendingIntentFinancialBindingError,
                "same JournalStore",
            ):
                bindings.bind(
                    pending_registry,
                    pending_intent_id="pending-bind-1",
                    account_id="acct-1",
                    environment="PAPER",
                    authority_policy_id="authority-policy-1",
                    authority_policy_version=3,
                    resolved_risk_policy=other_resolved,
                    reservation_requirements={"CASH:USD": "202.50"},
                    at=NOW + timedelta(seconds=1),
                )

    def test_direct_binding_construction_is_not_authority(self) -> None:
        with self.assertRaisesRegex(
            PendingIntentFinancialBindingError,
            "durable registry",
        ):
            PendingIntentFinancialBinding(
                pending_intent_id="forged",
                intent_hash="sha256:" + "0" * 64,
                account_id="acct-1",
                environment="PAPER",
                authority_policy_id="authority-policy-1",
                authority_policy_version=3,
                risk_policy_id="risk-policy-1",
                risk_policy_version=1,
                risk_policy_content_digest="sha256:" + "1" * 64,
                risk_policy_scope={},
                risk_policy_registration_event_id="register",
                risk_policy_activation_event_id="activate",
                risk_policy_resolved_journal_sequence_cut=1,
                journal_store_identity_digest="sha256:" + "2" * 64,
                reservation_requirements={"CASH:USD": "202.5"},
                bound_at="2026-10-05T00:20:01Z",
                binding_hash="sha256:" + "3" * 64,
            )


if __name__ == "__main__":
    unittest.main()
