from __future__ import annotations

from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.authority import (
    AuthorityPolicy,
    AuthorityService,
    InstrumentVersionIdentity,
)
from mvp.autotrade_mvp.durable_host_api import JournalBackedHostCommandStore
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


NOW = datetime(2026, 10, 5, 0, 55, tzinfo=timezone.utc)
NOW_TEXT = NOW.isoformat().replace("+00:00", "Z")
INSTRUMENT_ID = "11111111-1111-1111-1111-111111111111"
COMMAND_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class ConfirmIntentPendingClaimProofTests(unittest.TestCase):
    @staticmethod
    def _risk_policy() -> RiskPolicy:
        return RiskPolicy.create(
            max_abs_position="100",
            max_single_notional="1000",
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
            account_id="acct-claim-proof",
            environment="PAPER",
            provider_environment="PAPER",
            entity_policy_id="entity-policy-claim-proof",
            instrument_family="SPOT",
        )

    def test_matching_confirmation_event_without_exact_pending_claim_is_not_host_success(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority = AuthorityService(store)
            authority.register_policy(
                AuthorityPolicy.create(
                    policy_id="authority-policy-claim-proof",
                    account_id="acct-claim-proof",
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

            risk_policy = self._risk_policy()
            risk_registry = DurableRiskPolicyRegistry(store)
            scope = self._scope()
            risk_registry.register(
                scope=scope,
                policy_id="risk-policy-claim-proof",
                version=1,
                policy=risk_policy,
                committed_at=NOW - timedelta(seconds=5),
            )
            risk_registry.activate(
                scope=scope,
                policy_id="risk-policy-claim-proof",
                version=1,
                committed_at=NOW - timedelta(seconds=4),
            )

            risk_intent = RiskIntent.create(
                symbol="BTCUSD",
                side="BUY",
                quantity="2",
                price="101.25",
                expected_state_version=7,
                reduce_only=False,
                action="TRADE",
                instrument_type="SPOT",
            )
            pending_registry = DurablePendingIntentRegistry(store)
            pending = pending_registry.register(
                pending_intent_id="pending-claim-proof",
                account_id="acct-claim-proof",
                environment="PAPER",
                policy_id="authority-policy-claim-proof",
                authority_policy_version=3,
                instrument_id=INSTRUMENT_ID,
                instrument_version=9,
                authority_action="ORDER.SUBMIT",
                notional="202.50",
                risk_intent=risk_intent,
                registered_at=NOW - timedelta(seconds=3),
                expires_at=NOW + timedelta(minutes=10),
            )
            DurablePendingIntentFinancialBindingRegistry(store).bind(
                pending_registry,
                pending_intent_id="pending-claim-proof",
                account_id="acct-claim-proof",
                environment="PAPER",
                authority_policy_id="authority-policy-claim-proof",
                authority_policy_version=3,
                resolved_risk_policy=risk_registry.resolve_current(scope),
                reservation_requirements={"CASH:USD": "202.50"},
                at=NOW - timedelta(seconds=2),
            )

            host = JournalBackedHostCommandStore(
                store,
                account_id="acct-claim-proof",
                environment="PAPER",
                session_validator=lambda session, actor, origin, action: (
                    session == "owner-session"
                    and actor == "owner-claim-proof"
                    and origin == "https://local.autotrade.invalid"
                    and action == "CONFIRM_INTENT"
                ),
                request_origin_provider=lambda: "https://local.autotrade.invalid",
                now=lambda: NOW_TEXT,
            )
            accepted = host.submit(
                {
                    "command_id": COMMAND_ID,
                    "expected_state_version": "0",
                    "idempotency_key": "claim-proof-key",
                    "actor": "owner-claim-proof",
                    "session": "owner-session",
                    "account_id": "acct-claim-proof",
                    "environment": "PAPER",
                    "action": "CONFIRM_INTENT",
                    "payload": {"pending_intent_id": "pending-claim-proof"},
                }
            )
            self.assertEqual(accepted.status, "ACCEPTED")

            # Forge only the downstream AuthorityService event with the exact
            # economic bytes that the real composition would use. This is not
            # enough: the authenticated host command must also own the one-way
            # pending-intent claim.
            AuthorityService(store).add_financial_confirmation(
                confirmation_id=COMMAND_ID,
                policy_id=pending.policy_id,
                intent_hash=pending.intent_hash,
                account_id=pending.account_id,
                environment=pending.environment,
                instrument_id=pending.instrument_id,
                instrument_version=pending.instrument_version,
                action=pending.authority_action,
                notional=pending.notional,
                expires_at=pending.expires_at,
                risk_intent=pending.risk_intent,
                risk_policy=risk_policy,
                reservation_requirements={"CASH:USD": "202.50"},
            )
            _pending_readback, claim = pending_registry._read(
                "pending-claim-proof"
            )
            self.assertIsNone(claim)

            completed = host.execute_authority_operation(accepted.operation_id)
            self.assertEqual(completed.phase, "FAILED")
            self.assertEqual(completed.affected_refs, ())
            self.assertTrue(
                any(
                    evidence.get("reason_code") == "authority_state_changed"
                    for evidence in completed.evidence
                )
            )
            _pending_readback, claim_after = pending_registry._read(
                "pending-claim-proof"
            )
            self.assertIsNone(claim_after)

            # Restart must replay FAILED, never upgrade the forged event into a
            # canonical host success.
            restarted = JournalBackedHostCommandStore(
                JournalStore(store.path),
                account_id="acct-claim-proof",
                environment="PAPER",
                session_validator=lambda session, actor, origin, action: True,
                request_origin_provider=lambda: "https://local.autotrade.invalid",
                now=lambda: NOW_TEXT,
            )
            self.assertEqual(
                restarted.get_operation(accepted.operation_id).phase,
                "FAILED",
            )


if __name__ == "__main__":
    unittest.main()
