from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.pending_intents import (
    DurablePendingIntentRegistry,
    PendingFinancialIntent,
    PendingIntentError,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.risk import RiskIntent


NOW = datetime(2026, 10, 4, 20, 0, tzinfo=timezone.utc)
INSTRUMENT_ID = "11111111-1111-1111-1111-111111111111"


class HostileTimezone(tzinfo):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def utcoffset(self, _dt):
        self.calls.append("utcoffset")
        return timedelta(0)

    def dst(self, _dt):
        self.calls.append("dst")
        return timedelta(0)

    def tzname(self, _dt):
        self.calls.append("tzname")
        return "UTC"


class PendingIntentAuthorityTests(unittest.TestCase):
    @staticmethod
    def _intent(*, quantity: str = "2", price: str = "101.25") -> RiskIntent:
        return RiskIntent.create(
            symbol="BTCUSD",
            side="BUY",
            quantity=quantity,
            price=price,
            expected_state_version=7,
            reduce_only=False,
            action="TRADE",
            instrument_type="SPOT",
        )

    @staticmethod
    def _register(
        registry: DurablePendingIntentRegistry,
        *,
        pending_id: str = "pending-order-1",
        quantity: str = "2",
        price: str = "101.25",
        authority_action: str = "ORDER.SUBMIT",
        notional: str = "202.50",
    ):
        return registry.register(
            pending_intent_id=pending_id,
            account_id="acct-1",
            environment="PAPER",
            policy_id="policy-1",
            authority_policy_version=3,
            instrument_id=INSTRUMENT_ID,
            instrument_version=9,
            authority_action=authority_action,
            notional=notional,
            risk_intent=PendingIntentAuthorityTests._intent(
                quantity=quantity,
                price=price,
            ),
            registered_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )

    def test_restart_resolves_exact_server_owned_economics(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            registry = DurablePendingIntentRegistry(store)
            created = self._register(registry)

            restarted = DurablePendingIntentRegistry(JournalStore(store.path))
            resolved = restarted.resolve(
                created.pending_intent_id,
                account_id="acct-1",
                environment="PAPER",
                policy_id="policy-1",
                authority_policy_version=3,
                at=NOW + timedelta(seconds=1),
            )

            self.assertEqual(resolved, created)
            self.assertEqual(resolved.authority_action, "ORDER.SUBMIT")
            self.assertEqual(resolved.notional, Decimal("202.50"))
            self.assertEqual(resolved.risk_intent.quantity, Decimal("2"))
            self.assertEqual(resolved.risk_intent.price, Decimal("101.25"))
            self.assertTrue(resolved.intent_hash.startswith("sha256:"))

    def test_direct_pending_value_construction_is_not_authority(self) -> None:
        with self.assertRaisesRegex(
            PendingIntentError,
            "durable server registry",
        ):
            PendingFinancialIntent(
                pending_intent_id="forged",
                account_id="acct-1",
                environment="PAPER",
                policy_id="policy-1",
                authority_policy_version=3,
                instrument_id=INSTRUMENT_ID,
                instrument_version=9,
                authority_action="ORDER.SUBMIT",
                notional=Decimal("202.50"),
                risk_intent=self._intent(),
                registered_at=NOW.isoformat().replace("+00:00", "Z"),
                expires_at=(NOW + timedelta(minutes=5)).isoformat().replace(
                    "+00:00", "Z"
                ),
                intent_hash="sha256:" + "0" * 64,
            )

    def test_scope_swap_is_rejected_without_mutation(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            registry = DurablePendingIntentRegistry(store)
            created = self._register(registry)
            before = JournalStore.current_journal_sequence(store)

            cases = (
                {"account_id": "acct-2"},
                {"environment": "LIVE"},
                {"policy_id": "policy-2"},
                {"authority_policy_version": 4},
            )
            for changed in cases:
                kwargs = {
                    "account_id": "acct-1",
                    "environment": "PAPER",
                    "policy_id": "policy-1",
                    "authority_policy_version": 3,
                    "at": NOW + timedelta(seconds=1),
                }
                kwargs.update(changed)
                with self.subTest(changed=changed):
                    with self.assertRaisesRegex(PendingIntentError, "scope differs"):
                        registry.resolve(created.pending_intent_id, **kwargs)

            self.assertEqual(JournalStore.current_journal_sequence(store), before)

    def test_expired_pending_intent_cannot_be_confirmed(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            registry = DurablePendingIntentRegistry(store)
            created = self._register(registry)

            with self.assertRaisesRegex(PendingIntentError, "not current"):
                registry.claim_confirmation(
                    created.pending_intent_id,
                    confirmation_id="confirm-1",
                    actor_id="owner-1",
                    account_id="acct-1",
                    environment="PAPER",
                    policy_id="policy-1",
                    authority_policy_version=3,
                    at=NOW + timedelta(minutes=5),
                )

    def test_same_pending_id_cannot_rebind_economics_or_authority_scope(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            registry = DurablePendingIntentRegistry(store)
            original = self._register(registry)
            retried = self._register(registry)
            self.assertEqual(retried, original)

            conflicting = (
                {"quantity": "3"},
                {"price": "101.26"},
                {"authority_action": "ORDER.CANCEL"},
                {"notional": "202.51"},
            )
            for changed in conflicting:
                with self.subTest(changed=changed):
                    with self.assertRaisesRegex(
                        PendingIntentError, "conflicting durable state"
                    ):
                        self._register(registry, **changed)

            events = JournalStore.load_events(
                store,
                "pending_financial_intent",
                original.pending_intent_id,
            )
            self.assertEqual(len(events), 1)

    def test_claim_is_single_confirmation_and_exact_retry_is_idempotent(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            registry = DurablePendingIntentRegistry(store)
            created = self._register(registry)
            at = NOW + timedelta(seconds=2)

            first = registry.claim_confirmation(
                created.pending_intent_id,
                confirmation_id="confirm-1",
                actor_id="owner-1",
                account_id="acct-1",
                environment="PAPER",
                policy_id="policy-1",
                authority_policy_version=3,
                at=at,
            )
            retry = registry.claim_confirmation(
                created.pending_intent_id,
                confirmation_id="confirm-1",
                actor_id="owner-1",
                account_id="acct-1",
                environment="PAPER",
                policy_id="policy-1",
                authority_policy_version=3,
                at=at,
            )
            self.assertEqual(first, created)
            self.assertEqual(retry, created)

            restarted = DurablePendingIntentRegistry(JournalStore(store.path))
            self.assertEqual(
                restarted.claim_confirmation(
                    created.pending_intent_id,
                    confirmation_id="confirm-1",
                    actor_id="owner-1",
                    account_id="acct-1",
                    environment="PAPER",
                    policy_id="policy-1",
                    authority_policy_version=3,
                    at=at,
                ),
                created,
            )

            for confirmation_id, actor_id in (
                ("confirm-2", "owner-1"),
                ("confirm-1", "owner-2"),
            ):
                with self.subTest(
                    confirmation_id=confirmation_id,
                    actor_id=actor_id,
                ):
                    with self.assertRaisesRegex(PendingIntentError, "already claimed"):
                        restarted.claim_confirmation(
                            created.pending_intent_id,
                            confirmation_id=confirmation_id,
                            actor_id=actor_id,
                            account_id="acct-1",
                            environment="PAPER",
                            policy_id="policy-1",
                            authority_policy_version=3,
                            at=at,
                        )

            events = JournalStore.load_events(
                store,
                "pending_financial_intent",
                created.pending_intent_id,
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "PendingFinancialIntentRegistered",
                    "PendingFinancialIntentConfirmationClaimed",
                ],
            )

    def test_confirmation_lookup_has_no_caller_financial_override(self) -> None:
        with TemporaryDirectory() as directory:
            registry = DurablePendingIntentRegistry(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            created = self._register(registry)
            with self.assertRaises(TypeError):
                registry.claim_confirmation(
                    created.pending_intent_id,
                    confirmation_id="confirm-1",
                    actor_id="owner-1",
                    account_id="acct-1",
                    environment="PAPER",
                    policy_id="policy-1",
                    authority_policy_version=3,
                    at=NOW + timedelta(seconds=1),
                    notional="999999",
                )

    def test_mutated_risk_intent_is_rejected_before_journal_mutation(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            registry = DurablePendingIntentRegistry(store)
            intent = self._intent()
            object.__setattr__(intent, "side", "HOLD")
            before = JournalStore.current_journal_sequence(store)

            with self.assertRaisesRegex(PendingIntentError, "no longer canonical"):
                registry.register(
                    pending_intent_id="mutated",
                    account_id="acct-1",
                    environment="PAPER",
                    policy_id="policy-1",
                    authority_policy_version=3,
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=9,
                    authority_action="ORDER.SUBMIT",
                    notional="202.50",
                    risk_intent=intent,
                    registered_at=NOW,
                    expires_at=NOW + timedelta(minutes=5),
                )

            self.assertEqual(JournalStore.current_journal_sequence(store), before)

    def test_hostile_timezone_is_rejected_without_callback_or_journal_mutation(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            registry = DurablePendingIntentRegistry(store)
            hostile = HostileTimezone()
            registered_at = datetime(2026, 10, 4, 20, 0, tzinfo=hostile)
            before = JournalStore.current_journal_sequence(store)

            with self.assertRaisesRegex(PendingIntentError, "built-in timezone"):
                registry.register(
                    pending_intent_id="hostile-time",
                    account_id="acct-1",
                    environment="PAPER",
                    policy_id="policy-1",
                    authority_policy_version=3,
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=9,
                    authority_action="ORDER.SUBMIT",
                    notional="202.50",
                    risk_intent=self._intent(),
                    registered_at=registered_at,
                    expires_at=NOW + timedelta(minutes=5),
                )

            self.assertEqual(hostile.calls, [])
            self.assertEqual(JournalStore.current_journal_sequence(store), before)

    def test_public_journal_method_rebind_cannot_redirect_registry(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            registry = DurablePendingIntentRegistry(store)
            public_calls: list[str] = []
            installed_append = JournalStore.append_event
            installed_load = JournalStore.load_events

            def rebound_append(target, event, *args, **kwargs):
                public_calls.append("append")
                return installed_append(target, event, *args, **kwargs)

            def rebound_load(target, aggregate_type, aggregate_id):
                public_calls.append("load")
                return installed_load(target, aggregate_type, aggregate_id)

            with patch.object(JournalStore, "append_event", new=rebound_append), patch.object(
                JournalStore, "load_events", new=rebound_load
            ):
                created = self._register(registry, pending_id="rebind")
                resolved = registry.resolve(
                    created.pending_intent_id,
                    account_id="acct-1",
                    environment="PAPER",
                    policy_id="policy-1",
                    authority_policy_version=3,
                    at=NOW + timedelta(seconds=1),
                )

            self.assertEqual(public_calls, [])
            self.assertEqual(resolved, created)


if __name__ == "__main__":
    unittest.main()
