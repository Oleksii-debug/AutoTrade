from __future__ import annotations

from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.pending_confirmation_envelope import (
    DurablePendingConfirmationEnvelopeRegistry,
    PendingConfirmationEnvelopeError,
)
from mvp.autotrade_mvp.pending_intents import DurablePendingIntentRegistry
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.risk import RiskIntent, RiskPolicy
from mvp.autotrade_mvp.risk_policy_authority import (
    DurableRiskPolicyRegistry,
    RiskPolicyScope,
)


NOW = datetime(2026, 10, 4, 20, 0, tzinfo=timezone.utc)
INSTRUMENT_ID = "11111111-1111-1111-1111-111111111111"


def _scope() -> RiskPolicyScope:
    return RiskPolicyScope(
        provider_id="BYBIT",
        account_id="acct-1",
        environment="PAPER",
        provider_environment="TESTNET",
        entity_policy_id="bybit-global-v1",
        instrument_family="PERPETUAL",
    )


def _policy(leverage: str) -> RiskPolicy:
    return RiskPolicy.create(
        max_abs_position="100",
        max_single_notional="1000",
        max_gross_leverage=leverage,
        max_net_leverage="1.5",
        max_daily_loss="100",
        max_drawdown_fraction="0.20",
        max_data_age_seconds="5",
        max_fx_age_seconds="5",
        min_margin_headroom="0.10",
        max_stress_loss="200",
        allowed_actions=("TRADE", "REDUCE", "HEDGE", "FLATTEN"),
    )


class PendingConfirmationEnvelopeCurrentnessTests(unittest.TestCase):
    def test_stale_registry_issued_policy_cannot_prepare_operator_envelope(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            scope = _scope()
            risk_registry = DurableRiskPolicyRegistry(store)
            risk_registry.register(
                scope=scope,
                policy_id="risk-core",
                version=1,
                policy=_policy("2"),
                committed_at=NOW,
            )
            risk_registry.activate(
                scope=scope,
                policy_id="risk-core",
                version=1,
                committed_at=NOW + timedelta(seconds=1),
            )
            stale = risk_registry.resolve_current(scope)

            pending_registry = DurablePendingIntentRegistry(store)
            pending = pending_registry.register(
                pending_intent_id="pending-stale-risk",
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
                    action="TRADE",
                    instrument_type="PERPETUAL",
                ),
                registered_at=NOW + timedelta(seconds=2),
                expires_at=NOW + timedelta(minutes=10),
            )

            risk_registry.register(
                scope=scope,
                policy_id="risk-core",
                version=2,
                policy=_policy("1.5"),
                committed_at=NOW + timedelta(seconds=3),
            )
            risk_registry.activate(
                scope=scope,
                policy_id="risk-core",
                version=2,
                committed_at=NOW + timedelta(seconds=4),
            )

            envelope_registry = DurablePendingConfirmationEnvelopeRegistry(
                pending_registry
            )
            with self.assertRaisesRegex(
                PendingConfirmationEnvelopeError,
                "not current",
            ):
                envelope_registry.prepare(
                    pending.pending_intent_id,
                    account_id="acct-1",
                    environment="PAPER",
                    resolved_risk_policy=stale,
                    reservation_requirements={"CASH:USD": "202.50"},
                    prepared_at=NOW + timedelta(seconds=5),
                )

            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "pending_financial_confirmation_envelope",
                    pending.pending_intent_id,
                ),
                [],
            )


if __name__ == "__main__":
    unittest.main()
