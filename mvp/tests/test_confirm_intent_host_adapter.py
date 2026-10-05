from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import operator_authority_commands as operator_commands
from mvp.autotrade_mvp.authority import (
    AuthorityPolicy,
    AuthorityService,
    InstrumentVersionIdentity,
)
from mvp.autotrade_mvp.durable_host_api import JournalBackedHostCommandStore
from mvp.autotrade_mvp.host_actions import required_roles_for_host_action
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


NOW = datetime(2026, 10, 5, 0, 45, tzinfo=timezone.utc)
NOW_TEXT = NOW.isoformat().replace("+00:00", "Z")
INSTRUMENT_ID = "11111111-1111-1111-1111-111111111111"
COMMAND_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class _ActorFlipMapping(Mapping[str, object]):
    """Mapping whose actor differs across repeated Mapping.get() calls."""

    def __init__(self, source: dict[str, object]) -> None:
        self._source = dict(source)
        self.actor_gets = 0

    def __getitem__(self, key: str) -> object:
        return self._source[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._source)

    def __len__(self) -> int:
        return len(self._source)

    def get(self, key: str, default=None):
        if key == "actor":
            self.actor_gets += 1
            if self.actor_gets == 1:
                return "attacker-before-auth"
        return self._source.get(key, default)


class ConfirmIntentHostAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = f"{self.directory.name}/journal.sqlite3"
        self.store = JournalStore(self.path)
        self.pending = self._setup_financial_authority(self.store)

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

    def _setup_financial_authority(
        self,
        store: JournalStore,
    ) -> DurablePendingIntentRegistry:
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
        pending = DurablePendingIntentRegistry(store)
        pending.register(
            pending_intent_id="pending-host-confirm-1",
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
        DurablePendingIntentFinancialBindingRegistry(store).bind(
            pending,
            pending_intent_id="pending-host-confirm-1",
            account_id="acct-1",
            environment="PAPER",
            authority_policy_id="authority-policy-1",
            authority_policy_version=3,
            resolved_risk_policy=risk_registry.resolve_current(scope),
            reservation_requirements={"CASH:USD": "202.50"},
            at=NOW,
        )
        return pending

    def _register_second_risk_policy(
        self,
    ) -> tuple[DurableRiskPolicyRegistry, RiskPolicyScope]:
        risk_registry = DurableRiskPolicyRegistry(self.store)
        scope = self._scope()
        risk_registry.register(
            scope=scope,
            policy_id="risk-policy-1",
            version=2,
            policy=self._risk_policy(max_single_notional="900"),
            committed_at=NOW + timedelta(seconds=1),
        )
        return risk_registry, scope

    def host(self) -> JournalBackedHostCommandStore:
        return JournalBackedHostCommandStore(
            self.store,
            account_id="acct-1",
            environment="PAPER",
            session_validator=lambda session, actor, origin, action: (
                session == "session-owner"
                and actor == "owner-1"
                and origin == "https://local.autotrade.invalid"
                and action == "CONFIRM_INTENT"
            ),
            request_origin_provider=lambda: "https://local.autotrade.invalid",
            now=lambda: NOW_TEXT,
        )

    @staticmethod
    def command(*, payload=None, actor="owner-1", session="session-owner"):
        return {
            "command_id": COMMAND_ID,
            "expected_state_version": "0",
            "idempotency_key": "confirm-intent-key-1",
            "actor": actor,
            "session": session,
            "account_id": "acct-1",
            "environment": "PAPER",
            "action": "CONFIRM_INTENT",
            "payload": (
                {"pending_intent_id": "pending-host-confirm-1"}
                if payload is None
                else payload
            ),
        }

    def test_action_is_owner_only(self) -> None:
        self.assertEqual(
            required_roles_for_host_action("CONFIRM_INTENT"),
            frozenset({"OWNER"}),
        )

    def test_submit_binds_authenticated_actor_without_financial_fields(self) -> None:
        host = self.host()
        result = host.submit(self.command())
        self.assertEqual(result.status, "ACCEPTED")
        accepted = host.events_after(0)[0]
        self.assertEqual(accepted.kind, "COMMAND_ACCEPTED")
        action_payload = accepted.payload["action_payload"]
        self.assertEqual(action_payload["actor_id"], "owner-1")
        self.assertEqual(action_payload["confirmation_id"], COMMAND_ID)
        self.assertEqual(
            action_payload["pending_intent_id"],
            "pending-host-confirm-1",
        )
        for forbidden in (
            "quantity",
            "price",
            "notional",
            "risk_intent",
            "risk_policy",
            "reservation_requirements",
        ):
            self.assertNotIn(forbidden, action_payload)

        replay = host.submit(self.command())
        self.assertEqual(replay, result)
        self.assertEqual(host.state_version, 1)

    def test_submit_snapshots_mapping_before_actor_authentication(self) -> None:
        host = self.host()
        hostile = _ActorFlipMapping(self.command())
        result = host.submit(hostile)
        self.assertEqual(result.status, "ACCEPTED")
        accepted = host.events_after(0)[0]
        self.assertEqual(accepted.payload["actor"], "owner-1")
        self.assertEqual(
            accepted.payload["action_payload"]["actor_id"],
            "owner-1",
        )
        self.assertEqual(hostile.actor_gets, 0)

    def test_client_financial_override_is_rejected_before_host_mutation(self) -> None:
        host = self.host()
        with self.assertRaisesRegex(ValueError, "only pending_intent_id"):
            host.submit(
                self.command(
                    payload={
                        "pending_intent_id": "pending-host-confirm-1",
                        "notional": "999999",
                    }
                )
            )
        self.assertEqual(host.state_version, 0)
        self.assertNotIn(COMMAND_ID, AuthorityService(self.store)._confirmations)

    def test_execute_persists_confirmation_and_terminal_canonical_evidence(self) -> None:
        host = self.host()
        accepted = host.submit(self.command())
        self.assertIsNotNone(accepted.operation_id)
        completed = host.execute_authority_operation(accepted.operation_id)
        self.assertEqual(completed.phase, "SUCCEEDED")
        self.assertEqual(
            completed.affected_refs,
            ("authority-confirmation:" + COMMAND_ID,),
        )
        self.assertEqual(len(completed.evidence), 1)
        self.assertEqual(
            completed.evidence[0]["event_type"],
            "AuthorityConfirmationAdded",
        )
        confirmation = AuthorityService(self.store)._confirmations[COMMAND_ID]
        self.assertEqual(confirmation.account_id, "acct-1")
        self.assertEqual(
            confirmation.notional,
            self.pending._read("pending-host-confirm-1")[0].notional,
        )
        self.assertIsNotNone(confirmation.financial_binding_hash)

        restarted = self.host()
        replayed = restarted.get_operation(accepted.operation_id)
        self.assertEqual(replayed.phase, "SUCCEEDED")
        self.assertEqual(replayed.evidence, completed.evidence)
        self.assertEqual(
            restarted.execute_authority_operation(accepted.operation_id),
            replayed,
        )

    def test_risk_reactivation_after_claim_cannot_mint_confirmation(self) -> None:
        host = self.host()
        accepted = host.submit(self.command())
        risk_registry, scope = self._register_second_risk_policy()
        original_claim = DurablePendingIntentRegistry.claim_confirmation

        def claim_then_reactivate(registry, *args, **kwargs):
            claimed = original_claim(registry, *args, **kwargs)
            risk_registry.activate(
                scope=scope,
                policy_id="risk-policy-1",
                version=2,
                committed_at=NOW + timedelta(seconds=2),
            )
            return claimed

        with patch.object(
            DurablePendingIntentRegistry,
            "claim_confirmation",
            claim_then_reactivate,
        ):
            failed = host.execute_authority_operation(accepted.operation_id)

        self.assertEqual(failed.phase, "FAILED")
        self.assertNotIn(COMMAND_ID, AuthorityService(self.store)._confirmations)
        _pending, claim = self.pending._read("pending-host-confirm-1")
        self.assertIsNotNone(claim)

    def test_risk_reactivation_during_authority_append_is_cut_fenced(self) -> None:
        host = self.host()
        accepted = host.submit(self.command())
        risk_registry, scope = self._register_second_risk_policy()
        original_add = AuthorityService.add_financial_confirmation
        raced = False

        def reactivate_then_add(authority, *args, **kwargs):
            nonlocal raced
            if not raced:
                raced = True
                risk_registry.activate(
                    scope=scope,
                    policy_id="risk-policy-1",
                    version=2,
                    committed_at=NOW + timedelta(seconds=2),
                )
            return original_add(authority, *args, **kwargs)

        with patch.object(
            AuthorityService,
            "add_financial_confirmation",
            reactivate_then_add,
        ):
            failed = host.execute_authority_operation(accepted.operation_id)

        self.assertTrue(raced)
        self.assertEqual(failed.phase, "FAILED")
        self.assertNotIn(COMMAND_ID, AuthorityService(self.store)._confirmations)
        _pending, claim = self.pending._read("pending-host-confirm-1")
        self.assertIsNotNone(claim)

    def test_terminal_readback_rejects_stale_policy_confirmation_with_exact_claim(self) -> None:
        host = self.host()
        accepted = host.submit(self.command())
        risk_registry, scope = self._register_second_risk_policy()
        binding = DurablePendingIntentFinancialBindingRegistry(self.store)._load(
            "pending-host-confirm-1"
        )
        stale_policy = self._risk_policy()

        def bypass_safe_composition(_journal, **kwargs):
            pending = self.pending.claim_confirmation(
                "pending-host-confirm-1",
                confirmation_id=COMMAND_ID,
                actor_id="owner-1",
                account_id="acct-1",
                environment="PAPER",
                policy_id="authority-policy-1",
                authority_policy_version=3,
                at=kwargs["accepted_at"],
            )
            risk_registry.activate(
                scope=scope,
                policy_id="risk-policy-1",
                version=2,
                committed_at=NOW + timedelta(seconds=2),
            )
            AuthorityService(self.store).add_financial_confirmation(
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
                risk_policy=stale_policy,
                reservation_requirements=dict(binding.reservation_requirements),
            )

        with patch.object(
            operator_commands._confirm_impl,
            "confirm_pending_intent",
            bypass_safe_composition,
        ):
            failed = host.execute_authority_operation(accepted.operation_id)

        self.assertEqual(failed.phase, "FAILED")
        self.assertIn(COMMAND_ID, AuthorityService(self.store)._confirmations)
        _pending, claim = self.pending._read("pending-host-confirm-1")
        self.assertEqual(claim["confirmation_id"], COMMAND_ID)
        self.assertEqual(claim["actor_id"], "owner-1")

    def test_stale_authority_cut_fails_without_claiming_pending_intent(self) -> None:
        host = self.host()
        accepted = host.submit(self.command())
        AuthorityService(self.store).register_policy(
            AuthorityPolicy.create(
                policy_id="unrelated-policy",
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
                autonomous=True,
                protection_only=False,
                version=1,
            )
        )
        failed = host.execute_authority_operation(accepted.operation_id)
        self.assertEqual(failed.phase, "FAILED")
        pending_events = self.store.load_events(
            "pending_financial_intent",
            "pending-host-confirm-1",
        )
        self.assertEqual(len(pending_events), 1)
        self.assertNotIn(COMMAND_ID, AuthorityService(self.store)._confirmations)

    def test_unauthenticated_actor_never_reaches_canonicalization(self) -> None:
        host = self.host()
        with self.assertRaises(PermissionError):
            host.submit(self.command(actor="attacker", session="bad-session"))
        self.assertEqual(host.state_version, 0)


if __name__ == "__main__":
    unittest.main()
