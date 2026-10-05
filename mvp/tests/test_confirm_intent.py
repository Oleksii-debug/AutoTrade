from __future__ import annotations

from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.authority import (
    AuthorityPolicy,
    AuthorityService,
    InstrumentVersionIdentity,
)
from mvp.autotrade_mvp.confirm_intent import ConfirmIntentError, confirm_pending_intent
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


NOW = datetime(2026, 10, 5, 0, 30, tzinfo=timezone.utc)
INSTRUMENT_ID = "11111111-1111-1111-1111-111111111111"


class ConfirmPendingIntentTests(unittest.TestCase):
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
        pending_registry.register(
            pending_intent_id="pending-confirm-1",
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
            expires_at=NOW + timedelta(minutes=10),
        )

        resolved = risk_registry.resolve_current(scope)
        DurablePendingIntentFinancialBindingRegistry(store).bind(
            pending_registry,
            pending_intent_id="pending-confirm-1",
            account_id="acct-1",
            environment="PAPER",
            authority_policy_id="authority-policy-1",
            authority_policy_version=3,
            resolved_risk_policy=resolved,
            reservation_requirements={"CASH:USD": "202.50"},
            at=NOW + timedelta(seconds=1),
        )
        return pending_registry, risk_registry, scope

    def test_confirm_uses_only_durable_server_owned_financial_envelope(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            self._setup(store)

            result = confirm_pending_intent(
                store,
                pending_intent_id="pending-confirm-1",
                confirmation_id="host-command-1",
                actor_id="owner-1",
                account_id="acct-1",
                environment="PAPER",
                accepted_at=NOW + timedelta(seconds=2),
            )

            self.assertEqual(result.confirmation_id, "host-command-1")
            self.assertEqual(result.actor_id, "owner-1")
            self.assertEqual(
                result.pending_intent.intent_hash,
                result.financial_binding.intent_hash,
            )

            restarted = AuthorityService(JournalStore(store.path))
            confirmation = restarted._confirmations["host-command-1"]
            self.assertEqual(confirmation.intent_hash, result.pending_intent.intent_hash)
            self.assertEqual(confirmation.account_id, "acct-1")
            self.assertEqual(confirmation.environment, "PAPER")
            self.assertEqual(confirmation.notional, result.pending_intent.notional)
            self.assertIsNotNone(confirmation.financial_binding_hash)

    def test_claim_then_crash_recovers_after_pending_expiry(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            pending_registry, _risk_registry, _scope = self._setup(store)
            claim_time = NOW + timedelta(seconds=2)

            pending_registry.claim_confirmation(
                "pending-confirm-1",
                confirmation_id="host-command-crash",
                actor_id="owner-1",
                account_id="acct-1",
                environment="PAPER",
                policy_id="authority-policy-1",
                authority_policy_version=3,
                at=claim_time,
            )
            self.assertNotIn(
                "host-command-crash",
                AuthorityService(store)._confirmations,
            )

            # The pending record expired at NOW + 10 minutes. The exact durable
            # claim must still permit recovery of the same host operation.
            recovery_time = NOW + timedelta(minutes=11)
            recovered = confirm_pending_intent(
                store,
                pending_intent_id="pending-confirm-1",
                confirmation_id="host-command-crash",
                actor_id="owner-1",
                account_id="acct-1",
                environment="PAPER",
                accepted_at=recovery_time,
            )
            self.assertEqual(recovered.confirmation_id, "host-command-crash")
            self.assertIn(
                "host-command-crash",
                AuthorityService(store)._confirmations,
            )

            retry = confirm_pending_intent(
                store,
                pending_intent_id="pending-confirm-1",
                confirmation_id="host-command-crash",
                actor_id="owner-1",
                account_id="acct-1",
                environment="PAPER",
                accepted_at=recovery_time + timedelta(seconds=20),
            )
            self.assertEqual(retry, recovered)

    def test_different_actor_cannot_take_over_durable_claim(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            pending_registry, _risk_registry, _scope = self._setup(store)
            point = NOW + timedelta(seconds=2)
            pending_registry.claim_confirmation(
                "pending-confirm-1",
                confirmation_id="host-command-owned",
                actor_id="owner-1",
                account_id="acct-1",
                environment="PAPER",
                policy_id="authority-policy-1",
                authority_policy_version=3,
                at=point,
            )

            with self.assertRaisesRegex(ConfirmIntentError, "claim failed"):
                confirm_pending_intent(
                    store,
                    pending_intent_id="pending-confirm-1",
                    confirmation_id="host-command-owned",
                    actor_id="owner-2",
                    account_id="acct-1",
                    environment="PAPER",
                    accepted_at=point + timedelta(seconds=1),
                )
            self.assertNotIn(
                "host-command-owned",
                AuthorityService(store)._confirmations,
            )

    def test_quantitative_policy_drift_fails_before_pending_claim(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            _pending_registry, risk_registry, scope = self._setup(store)
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

            before = store.load_events(
                "pending_financial_intent",
                "pending-confirm-1",
            )
            self.assertEqual(len(before), 1)
            with self.assertRaisesRegex(
                ConfirmIntentError,
                "risk authority no longer matches",
            ):
                confirm_pending_intent(
                    store,
                    pending_intent_id="pending-confirm-1",
                    confirmation_id="host-command-stale-policy",
                    actor_id="owner-1",
                    account_id="acct-1",
                    environment="PAPER",
                    accepted_at=NOW + timedelta(seconds=4),
                )
            after = store.load_events(
                "pending_financial_intent",
                "pending-confirm-1",
            )
            self.assertEqual(after, before)
            self.assertNotIn(
                "host-command-stale-policy",
                AuthorityService(store)._confirmations,
            )

    def test_financial_override_arguments_do_not_exist(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            self._setup(store)
            with self.assertRaises(TypeError):
                confirm_pending_intent(
                    store,
                    pending_intent_id="pending-confirm-1",
                    confirmation_id="host-command-no-override",
                    actor_id="owner-1",
                    account_id="acct-1",
                    environment="PAPER",
                    accepted_at=NOW + timedelta(seconds=2),
                    notional="999999",
                )


if __name__ == "__main__":
    unittest.main()
