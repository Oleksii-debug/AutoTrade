from __future__ import annotations

from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.pending_intents as pending_intents_module
from mvp.autotrade_mvp.pending_intents import DurablePendingIntentRegistry, PendingIntentError
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.risk import RiskIntent


NOW = datetime(2026, 10, 4, 20, 0, tzinfo=timezone.utc)
INSTRUMENT_ID = "11111111-1111-1111-1111-111111111111"


class PendingIntentRetryRebindingAuthorityTests(unittest.TestCase):
    @staticmethod
    def _registry(directory: str) -> tuple[JournalStore, DurablePendingIntentRegistry]:
        store = JournalStore(f"{directory}/journal.sqlite3")
        return store, DurablePendingIntentRegistry(store)

    @staticmethod
    def _register(registry: DurablePendingIntentRegistry, *, pending_id: str = "pending-retry"):
        return registry.register(
            pending_intent_id=pending_id,
            account_id="acct-1",
            environment="PAPER",
            policy_id="policy-1",
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
                instrument_type="SPOT",
            ),
            registered_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )

    @staticmethod
    def _claim(
        registry: DurablePendingIntentRegistry,
        pending_id: str,
        *,
        at: datetime,
        actor_id: str = "owner-1",
        confirmation_id: str = "confirm-1",
    ):
        return registry.claim_confirmation(
            pending_id,
            confirmation_id=confirmation_id,
            actor_id=actor_id,
            account_id="acct-1",
            environment="PAPER",
            policy_id="policy-1",
            authority_policy_version=3,
            at=at,
        )

    def test_exact_retry_preserves_first_claimed_at_before_and_after_expiry(self) -> None:
        with TemporaryDirectory() as directory:
            store, registry = self._registry(directory)
            pending = self._register(registry)
            first_at = NOW + timedelta(seconds=10)
            first = self._claim(registry, pending.pending_intent_id, at=first_at)
            self.assertEqual(first, pending)

            restarted = DurablePendingIntentRegistry(JournalStore(store.path))
            self.assertEqual(
                self._claim(
                    restarted,
                    pending.pending_intent_id,
                    at=NOW + timedelta(minutes=2),
                ),
                pending,
            )
            self.assertEqual(
                self._claim(
                    restarted,
                    pending.pending_intent_id,
                    at=NOW + timedelta(minutes=7),
                ),
                pending,
            )

            events = JournalStore.load_events(
                store,
                "pending_financial_intent",
                pending.pending_intent_id,
            )
            self.assertEqual(len(events), 2)
            claim = events[1]
            self.assertEqual(
                claim["payload"]["claimed_at"],
                first_at.isoformat().replace("+00:00", "Z"),
            )
            self.assertEqual(
                claim["committed_at"],
                first_at.isoformat().replace("+00:00", "Z"),
            )

    def test_retry_cannot_rewrite_actor_or_confirmation_after_expiry(self) -> None:
        with TemporaryDirectory() as directory:
            store, registry = self._registry(directory)
            pending = self._register(registry)
            self._claim(
                registry,
                pending.pending_intent_id,
                at=NOW + timedelta(seconds=10),
            )
            restarted = DurablePendingIntentRegistry(JournalStore(store.path))

            for changed in (
                {"actor_id": "owner-2"},
                {"confirmation_id": "confirm-2"},
            ):
                with self.subTest(changed=changed):
                    with self.assertRaisesRegex(
                        PendingIntentError,
                        "already claimed by another confirmation",
                    ):
                        self._claim(
                            restarted,
                            pending.pending_intent_id,
                            at=NOW + timedelta(minutes=7),
                            **changed,
                        )

            self.assertEqual(
                len(
                    JournalStore.load_events(
                        store,
                        "pending_financial_intent",
                        pending.pending_intent_id,
                    )
                ),
                2,
            )

    def test_retry_time_cannot_precede_durable_original_claim(self) -> None:
        with TemporaryDirectory() as directory:
            _store, registry = self._registry(directory)
            pending = self._register(registry)
            first_at = NOW + timedelta(seconds=10)
            self._claim(registry, pending.pending_intent_id, at=first_at)

            with self.assertRaisesRegex(PendingIntentError, "cannot precede"):
                self._claim(
                    registry,
                    pending.pending_intent_id,
                    at=NOW + timedelta(seconds=9),
                )

    def test_public_store_identity_rebind_cannot_retarget_registry_generation(self) -> None:
        with TemporaryDirectory() as directory:
            store, registry = self._registry(directory)
            public_calls: list[str] = []

            def forged_identity(_store):
                public_calls.append("store_identity")
                return object()

            with patch.object(
                JournalStore,
                "store_identity",
                new=property(forged_identity),
            ):
                pending = self._register(registry, pending_id="identity-rebind")
                resolved = registry.resolve(
                    pending.pending_intent_id,
                    account_id="acct-1",
                    environment="PAPER",
                    policy_id="policy-1",
                    authority_policy_version=3,
                    at=NOW + timedelta(seconds=1),
                )

            self.assertEqual(public_calls, [])
            self.assertEqual(resolved, pending)
            self.assertEqual(
                len(
                    JournalStore.load_events(
                        store,
                        "pending_financial_intent",
                        pending.pending_intent_id,
                    )
                ),
                1,
            )

    def test_public_payload_digest_rebind_cannot_forge_pending_envelopes(self) -> None:
        with TemporaryDirectory() as directory:
            store, registry = self._registry(directory)
            public_calls: list[str] = []

            def forged_digest(_payload):
                public_calls.append("payload_digest")
                return "sha256:" + "0" * 64

            with patch.object(
                pending_intents_module,
                "payload_digest",
                new=forged_digest,
            ):
                pending = self._register(registry, pending_id="digest-rebind")
                self._claim(
                    registry,
                    pending.pending_intent_id,
                    at=NOW + timedelta(seconds=1),
                )

            self.assertEqual(public_calls, [])
            events = JournalStore.load_events(
                store,
                "pending_financial_intent",
                pending.pending_intent_id,
            )
            self.assertEqual(len(events), 2)
            self.assertNotEqual(events[0]["payload_hash"], "sha256:" + "0" * 64)
            self.assertNotEqual(events[1]["payload_hash"], "sha256:" + "0" * 64)


if __name__ == "__main__":
    unittest.main()
