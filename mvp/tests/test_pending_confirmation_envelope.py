from __future__ import annotations

from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.pending_confirmation_envelope import (
    DurablePendingConfirmationEnvelopeRegistry,
    PendingConfirmationEnvelope,
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


def risk_scope(*, account_id: str = "acct-1", environment: str = "PAPER") -> RiskPolicyScope:
    return RiskPolicyScope(
        provider_id="BYBIT",
        account_id=account_id,
        environment=environment,
        provider_environment="TESTNET" if environment == "PAPER" else "MAINNET",
        entity_policy_id="bybit-global-v1",
        instrument_family="PERPETUAL",
    )


def risk_policy(*, max_gross_leverage: str = "2") -> RiskPolicy:
    return RiskPolicy.create(
        max_abs_position="100",
        max_single_notional="1000",
        max_gross_leverage=max_gross_leverage,
        max_net_leverage="1.5",
        max_daily_loss="100",
        max_drawdown_fraction="0.20",
        max_data_age_seconds="5",
        max_fx_age_seconds="5",
        min_margin_headroom="0.10",
        max_stress_loss="200",
        max_asset_concentration_fraction="0.75",
        max_venue_concentration_fraction="0.80",
        max_order_participation_fraction="0.10",
        max_spread_fraction="0.01",
        max_slippage_fraction="0.02",
        max_clock_age_seconds="2",
        allowed_actions=("TRADE", "REDUCE", "HEDGE", "FLATTEN"),
        require_settlement_evidence=True,
    )


class PendingConfirmationEnvelopeTests(unittest.TestCase):
    @staticmethod
    def _pending_registry(store: JournalStore) -> DurablePendingIntentRegistry:
        return DurablePendingIntentRegistry(store)

    @staticmethod
    def _register_pending(registry: DurablePendingIntentRegistry, *, pending_id="pending-1"):
        return registry.register(
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
                action="TRADE",
                instrument_type="PERPETUAL",
            ),
            registered_at=NOW + timedelta(seconds=2),
            expires_at=NOW + timedelta(minutes=10),
        )

    @staticmethod
    def _resolved_policy(
        store: JournalStore,
        *,
        scope: RiskPolicyScope | None = None,
        policy_id: str = "risk-core",
        version: int = 1,
        policy: RiskPolicy | None = None,
    ):
        registry = DurableRiskPolicyRegistry(store)
        exact_scope = risk_scope() if scope is None else scope
        exact_policy = risk_policy() if policy is None else policy
        registry.register(
            scope=exact_scope,
            policy_id=policy_id,
            version=version,
            policy=exact_policy,
            committed_at=NOW,
        )
        registry.activate(
            scope=exact_scope,
            policy_id=policy_id,
            version=version,
            committed_at=NOW + timedelta(seconds=1),
        )
        return registry, exact_scope, registry.resolve_current(exact_scope)

    def _prepared(self, directory: str):
        store = JournalStore(f"{directory}/journal.sqlite3")
        risk_registry, scope, resolved = self._resolved_policy(store)
        pending_registry = self._pending_registry(store)
        pending = self._register_pending(pending_registry)
        envelope_registry = DurablePendingConfirmationEnvelopeRegistry(pending_registry)
        envelope = envelope_registry.prepare(
            pending.pending_intent_id,
            account_id="acct-1",
            environment="PAPER",
            resolved_risk_policy=resolved,
            reservation_requirements={"CASH:USD": "202.50"},
            prepared_at=NOW + timedelta(seconds=3),
        )
        return (
            store,
            risk_registry,
            scope,
            pending_registry,
            pending,
            envelope_registry,
            envelope,
        )

    def test_restart_reconstructs_exact_historical_policy_and_requirements(self) -> None:
        with TemporaryDirectory() as directory:
            (
                store,
                _risk_registry,
                _scope,
                _pending_registry,
                pending,
                _envelope_registry,
                envelope,
            ) = self._prepared(directory)
            restarted_pending = DurablePendingIntentRegistry(JournalStore(store.path))
            restarted = DurablePendingConfirmationEnvelopeRegistry(restarted_pending)
            recovered = restarted.load(pending.pending_intent_id)

            self.assertEqual(recovered.pending_intent_hash, pending.intent_hash)
            self.assertEqual(
                recovered.resolved_risk_policy.identity,
                envelope.resolved_risk_policy.identity,
            )
            self.assertEqual(
                recovered.resolved_risk_policy.activation_event_id,
                envelope.resolved_risk_policy.activation_event_id,
            )
            self.assertEqual(
                dict(recovered.reservation_requirements),
                {"CASH:USD": envelope.reservation_requirements[0][1]},
            )
            self.assertEqual(str(recovered.reservation_requirements[0][1]), "202.50")

            current_envelope, current_policy = restarted.require_current(
                pending.pending_intent_id
            )
            self.assertEqual(current_envelope, recovered)
            self.assertEqual(current_policy.identity, recovered.resolved_risk_policy.identity)

    def test_risk_policy_from_another_journal_cannot_bind_pending_intent(self) -> None:
        with TemporaryDirectory() as first, TemporaryDirectory() as second:
            store = JournalStore(f"{first}/journal.sqlite3")
            pending_registry = DurablePendingIntentRegistry(store)
            pending = self._register_pending(pending_registry)
            envelope_registry = DurablePendingConfirmationEnvelopeRegistry(pending_registry)

            other_store = JournalStore(f"{second}/journal.sqlite3")
            _registry, _scope, other_resolved = self._resolved_policy(other_store)
            with self.assertRaisesRegex(
                PendingConfirmationEnvelopeError,
                "another JournalStore generation",
            ):
                envelope_registry.prepare(
                    pending.pending_intent_id,
                    account_id="acct-1",
                    environment="PAPER",
                    resolved_risk_policy=other_resolved,
                    reservation_requirements={"CASH:USD": "202.50"},
                    prepared_at=NOW + timedelta(seconds=3),
                )

    def test_risk_policy_account_or_environment_scope_mismatch_fails_closed(self) -> None:
        for exact_scope in (
            risk_scope(account_id="acct-2"),
            risk_scope(environment="LIVE"),
        ):
            with self.subTest(scope=exact_scope):
                with TemporaryDirectory() as directory:
                    store = JournalStore(f"{directory}/journal.sqlite3")
                    _registry, _scope, resolved = self._resolved_policy(
                        store,
                        scope=exact_scope,
                    )
                    pending_registry = DurablePendingIntentRegistry(store)
                    pending = self._register_pending(pending_registry)
                    envelope_registry = DurablePendingConfirmationEnvelopeRegistry(
                        pending_registry
                    )
                    with self.assertRaisesRegex(
                        PendingConfirmationEnvelopeError,
                        "scope differs",
                    ):
                        envelope_registry.prepare(
                            pending.pending_intent_id,
                            account_id="acct-1",
                            environment="PAPER",
                            resolved_risk_policy=resolved,
                            reservation_requirements={"CASH:USD": "202.50"},
                            prepared_at=NOW + timedelta(seconds=3),
                        )

    def test_same_pending_intent_cannot_rebind_reservation_requirements(self) -> None:
        with TemporaryDirectory() as directory:
            (
                _store,
                _risk_registry,
                _scope,
                _pending_registry,
                pending,
                envelope_registry,
                envelope,
            ) = self._prepared(directory)
            self.assertEqual(
                envelope_registry.prepare(
                    pending.pending_intent_id,
                    account_id="acct-1",
                    environment="PAPER",
                    resolved_risk_policy=envelope.resolved_risk_policy,
                    reservation_requirements={"CASH:USD": "202.50"},
                    prepared_at=NOW + timedelta(seconds=3),
                ),
                envelope,
            )
            with self.assertRaisesRegex(
                PendingConfirmationEnvelopeError,
                "conflicts with durable state",
            ):
                envelope_registry.prepare(
                    pending.pending_intent_id,
                    account_id="acct-1",
                    environment="PAPER",
                    resolved_risk_policy=envelope.resolved_risk_policy,
                    reservation_requirements={"CASH:USD": "202.51"},
                    prepared_at=NOW + timedelta(seconds=3),
                )

    def test_policy_supersession_keeps_historical_envelope_but_blocks_current_use(self) -> None:
        with TemporaryDirectory() as directory:
            (
                _store,
                risk_registry,
                scope,
                _pending_registry,
                pending,
                envelope_registry,
                envelope,
            ) = self._prepared(directory)

            risk_registry.register(
                scope=scope,
                policy_id="risk-core",
                version=2,
                policy=risk_policy(max_gross_leverage="1.5"),
                committed_at=NOW + timedelta(seconds=4),
            )
            risk_registry.activate(
                scope=scope,
                policy_id="risk-core",
                version=2,
                committed_at=NOW + timedelta(seconds=5),
            )

            historical = envelope_registry.load(pending.pending_intent_id)
            self.assertEqual(
                historical.resolved_risk_policy.identity,
                envelope.resolved_risk_policy.identity,
            )
            with self.assertRaisesRegex(
                PendingConfirmationEnvelopeError,
                "stale against current RiskPolicy authority",
            ):
                envelope_registry.require_current(pending.pending_intent_id)

    def test_direct_envelope_construction_is_not_authority(self) -> None:
        with TemporaryDirectory() as directory:
            (
                _store,
                _risk_registry,
                _scope,
                _pending_registry,
                _pending,
                _envelope_registry,
                envelope,
            ) = self._prepared(directory)
            with self.assertRaisesRegex(
                PendingConfirmationEnvelopeError,
                "must come from durable registry",
            ):
                PendingConfirmationEnvelope(
                    pending_intent_id=envelope.pending_intent_id,
                    pending_intent_hash=envelope.pending_intent_hash,
                    resolved_risk_policy=envelope.resolved_risk_policy,
                    reservation_requirements=envelope.reservation_requirements,
                    prepared_at=envelope.prepared_at,
                    event_id=envelope.event_id,
                    journal_sequence=envelope.journal_sequence,
                )


if __name__ == "__main__":
    unittest.main()
