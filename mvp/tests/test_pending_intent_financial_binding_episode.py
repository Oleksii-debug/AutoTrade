from __future__ import annotations

from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.pending_intent_financial_binding import (
    DurablePendingIntentFinancialBindingRegistry,
    PendingIntentFinancialBindingError,
)
from mvp.autotrade_mvp.pending_intents import DurablePendingIntentRegistry
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.risk import RiskIntent, RiskPolicy
from mvp.autotrade_mvp.risk_policy_authority import (
    DurableRiskPolicyRegistry,
    RiskPolicyScope,
)


NOW = datetime(2026, 10, 5, 0, 35, tzinfo=timezone.utc)
INSTRUMENT_ID = "11111111-1111-1111-1111-111111111111"


class PendingIntentRiskPolicyEpisodeTests(unittest.TestCase):
    @staticmethod
    def _policy(max_notional: str) -> RiskPolicy:
        return RiskPolicy.create(
            max_abs_position="100",
            max_single_notional=max_notional,
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

    def test_same_policy_content_reactivated_in_new_episode_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            scope = self._scope()
            risk_registry = DurableRiskPolicyRegistry(store)
            risk_registry.register(
                scope=scope,
                policy_id="risk-policy-1",
                version=1,
                policy=self._policy("1000"),
                committed_at=NOW - timedelta(seconds=6),
            )
            risk_registry.activate(
                scope=scope,
                policy_id="risk-policy-1",
                version=1,
                committed_at=NOW - timedelta(seconds=5),
            )
            first_episode = risk_registry.resolve_current(scope)

            pending_registry = DurablePendingIntentRegistry(store)
            pending_registry.register(
                pending_intent_id="pending-episode-1",
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
                registered_at=NOW - timedelta(seconds=4),
                expires_at=NOW + timedelta(minutes=5),
            )
            bindings = DurablePendingIntentFinancialBindingRegistry(store)
            bound = bindings.bind(
                pending_registry,
                pending_intent_id="pending-episode-1",
                account_id="acct-1",
                environment="PAPER",
                authority_policy_id="authority-policy-1",
                authority_policy_version=3,
                resolved_risk_policy=first_episode,
                reservation_requirements={"CASH:USD": "202.50"},
                at=NOW - timedelta(seconds=3),
            )

            risk_registry.register(
                scope=scope,
                policy_id="risk-policy-2",
                version=1,
                policy=self._policy("900"),
                committed_at=NOW - timedelta(seconds=2),
            )
            risk_registry.activate(
                scope=scope,
                policy_id="risk-policy-2",
                version=1,
                committed_at=NOW - timedelta(seconds=1),
                activation_request_id="switch-to-risk-policy-2",
                expected_previous_activation_event_id=(
                    first_episode.activation_event_id
                ),
            )
            second_episode = risk_registry.resolve_current(scope)
            self.assertEqual(second_episode.identity.policy_id, "risk-policy-2")

            risk_registry.activate(
                scope=scope,
                policy_id="risk-policy-1",
                version=1,
                committed_at=NOW,
                activation_request_id="reactivate-risk-policy-1",
                expected_previous_activation_event_id=(
                    second_episode.activation_event_id
                ),
            )
            reactivated = risk_registry.resolve_current(scope)

            self.assertEqual(reactivated.identity, first_episode.identity)
            self.assertEqual(
                reactivated.registration_event_id,
                first_episode.registration_event_id,
            )
            self.assertNotEqual(
                reactivated.activation_event_id,
                first_episode.activation_event_id,
            )
            self.assertEqual(
                bound.risk_policy_activation_event_id,
                first_episode.activation_event_id,
            )

            with self.assertRaisesRegex(
                PendingIntentFinancialBindingError,
                "activation episode no longer matches",
            ):
                bindings.resolve_current(
                    "pending-episode-1",
                    resolved_risk_policy=reactivated,
                    reservation_requirements={"CASH:USD": "202.50"},
                )


if __name__ == "__main__":
    unittest.main()
