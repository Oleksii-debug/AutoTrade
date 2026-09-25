from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    GuardedDispatcher,
    stable_client_order_id,
)
from mvp.autotrade_mvp.durable_order_projection import (
    DurableOrderBookProjection,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reconciliation import (
    ProviderFillEvidence,
    SnapshotConsistencyEvidence,
    UnknownSubmission,
    reconcile_account,
)
from mvp.autotrade_mvp.simulated_provider import SimulatedProvider


NOW = "2026-09-25T05:50:00Z"
LATER = "2026-09-25T05:51:00Z"
INSTRUMENT = "ABC@1"
ACCOUNT = "sim-account"
PROVIDER = "SIMULATED"


def projection(store: JournalStore) -> DurableOrderBookProjection:
    return DurableOrderBookProjection(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment="SIMULATION",
        host_id="sim-host",
        owner_epoch="1",
    )


def fill_evidence(fill, client_order_id: str) -> ProviderFillEvidence:
    fee = fill["fees"][0]
    return ProviderFillEvidence.create(
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment="SIMULATION",
        provider_execution_id=fill["provider_execution_id"],
        client_order_id=client_order_id,
        instrument=fill["instrument_version"],
        quantity=fill["last_quantity"]["value"],
        price=fill["last_price"],
        fee_amount=fee["amount"],
        fee_currency=fee["currency"],
        trade_time=fill["trade_time"],
    )


def reconcile_simulated(
    provider: SimulatedProvider,
    *,
    client_order_id: str,
    fill,
    unknown_submissions=(),
):
    snapshot = provider.account_snapshot(now=LATER)
    positions = {
        item["instrument_version"]: item["quantity"]["value"]
        for item in snapshot["positions"]
    }
    cash = snapshot["balances"][0]["total"]
    return reconcile_account(
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment="SIMULATION",
        local_cash={"USD": cash},
        provider_cash={"USD": cash},
        local_positions=positions,
        provider_positions=positions,
        local_execution_ids=[fill["provider_execution_id"]],
        provider_fills=[fill_evidence(fill, client_order_id)],
        snapshot_consistency=SnapshotConsistencyEvidence(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment="SIMULATION",
            mode="ATOMIC",
            query_started_at=LATER,
            query_completed_at=LATER,
        ),
        unknown_submissions=unknown_submissions,
        searched_client_order_ids=(client_order_id,),
        coverage_start=NOW,
        coverage_end=LATER,
        pagination_complete=True,
        provider_activity_provider_id=PROVIDER,
        provider_activity_account_id=ACCOUNT,
    )


class DispatchOrderProjectionIntegrationTests(unittest.TestCase):
    def test_dispatch_ack_fill_projection_and_reconciliation_share_identity(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            provider = SimulatedProvider(
                account_id=ACCOUNT,
                initial_cash="1000",
                fee_rate="0.001",
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id=ACCOUNT,
                owner_token="sim-owner",
            )
            intent_id = "intent-projected-ack"
            client_order_id = stable_client_order_id(
                "simulated",
                intent_id,
                environment="SIMULATION",
                account_id=ACCOUNT,
            )
            orders = projection(store)
            orders.create_order(
                event_key="intent-created:ack",
                client_order_id=client_order_id,
                instrument=INSTRUMENT,
                side="BUY",
                requested_quantity="2",
                committed_at=NOW,
            )

            attempt_id = "11111111-1111-4111-8111-111111111111"
            request = {
                "attempt_id": attempt_id,
                "instrument_version": INSTRUMENT,
                "side": "BUY",
                "quantity": "2",
                "price": "100",
                "now": NOW,
            }
            dispatched = dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id=intent_id,
                intent_hash="sha256:" + "a" * 64,
                provider="simulated",
                request=request,
                now=NOW,
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=provider.transport_send,
            )

            self.assertEqual(dispatched.status, "SENT")
            self.assertEqual(dispatched.client_order_id, client_order_id)
            self.assertEqual(dispatched.response["client_order_id"], client_order_id)
            self.assertEqual(dispatched.response["outcome"], "ACKNOWLEDGED")
            self.assertEqual(provider.outbound_request_count, 1)

            projected_submission = orders.sync_submission_attempt(
                attempt_id=attempt_id,
            )
            self.assertEqual(len(projected_submission), 2)
            send_started, acknowledged = projected_submission
            self.assertEqual(send_started.snapshot.state, "SEND_STARTED")
            self.assertEqual(
                send_started.snapshot.submission_attempt_id,
                attempt_id,
            )
            # The provider may already have an execution in its activity feed,
            # but acknowledgement alone is never permitted to invent that fill.
            self.assertEqual(acknowledged.snapshot.state, "WORKING")
            self.assertEqual(
                acknowledged.snapshot.submission_attempt_id,
                attempt_id,
            )
            self.assertEqual(acknowledged.snapshot.filled_quantity, Decimal("0"))

            fill = provider.activity_fills()[0]
            filled = orders.record_fill(
                event_key="provider-fill:ack",
                client_order_id=client_order_id,
                fill_id=fill["fill_id"],
                provider_execution_id=fill["provider_execution_id"],
                quantity=fill["last_quantity"]["value"],
                price=fill["last_price"],
                committed_at=fill["receipt_time"],
            )
            self.assertEqual(filled.snapshot.state, "FILLED")
            self.assertEqual(filled.snapshot.filled_quantity, Decimal("2"))

            reconciled = reconcile_simulated(
                provider,
                client_order_id=client_order_id,
                fill=fill,
            )
            self.assertTrue(reconciled.complete)
            self.assertFalse(reconciled.blocks_new_risk)
            self.assertEqual(
                reconciled.matched_execution_ids,
                (fill["provider_execution_id"],),
            )

            restarted = projection(store)
            restored = restarted.order(client_order_id).snapshot()
            self.assertEqual(restored.state, "FILLED")
            self.assertEqual(
                restored.provider_order_id,
                dispatched.response["provider_order_id"],
            )
            self.assertEqual(restored.filled_quantity, Decimal("2"))
            self.assertEqual(restored.submission_attempt_id, attempt_id)

    def test_ambiguous_send_reconciles_without_blind_retry_or_fabricated_ack(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            intent_id = "intent-projected-unknown"
            client_order_id = stable_client_order_id(
                "simulated",
                intent_id,
                environment="SIMULATION",
                account_id=ACCOUNT,
            )
            provider = SimulatedProvider(
                account_id=ACCOUNT,
                initial_cash="1000",
                fee_rate="0.001",
                transport_faults={
                    client_order_id: "AFTER_ACCEPT_RESPONSE_LOST",
                },
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id=ACCOUNT,
                owner_token="sim-owner",
            )
            orders = projection(store)
            orders.create_order(
                event_key="intent-created:unknown",
                client_order_id=client_order_id,
                instrument=INSTRUMENT,
                side="BUY",
                requested_quantity="1",
                committed_at=NOW,
            )

            attempt_id = "22222222-2222-4222-8222-222222222222"
            request = {
                "attempt_id": attempt_id,
                "instrument_version": INSTRUMENT,
                "side": "BUY",
                "quantity": "1",
                "price": "100",
                "now": NOW,
            }
            first = dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id=intent_id,
                intent_hash="sha256:" + "b" * 64,
                provider="simulated",
                request=request,
                now=NOW,
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=provider.transport_send,
            )
            self.assertEqual(first.status, "UNKNOWN")
            self.assertIsNone(first.response)
            self.assertEqual(provider.outbound_request_count, 1)

            projected_submission = orders.sync_submission_attempt(
                attempt_id=attempt_id,
            )
            self.assertEqual(len(projected_submission), 2)
            send_started, unknown = projected_submission
            self.assertEqual(send_started.snapshot.state, "SEND_STARTED")
            self.assertEqual(unknown.snapshot.state, "UNKNOWN")
            self.assertEqual(
                unknown.snapshot.submission_attempt_id,
                attempt_id,
            )
            self.assertIsNone(unknown.snapshot.provider_order_id)
            self.assertEqual(unknown.snapshot.filled_quantity, Decimal("0"))

            fill = provider.activity_fills()[0]
            unresolved = UnknownSubmission.create(
                attempt_id=attempt_id,
                intent_id=intent_id,
                provider_id=PROVIDER,
                account_id=ACCOUNT,
                environment="SIMULATION",
                client_order_id=client_order_id,
                started_at=NOW,
            )
            reconciled = reconcile_simulated(
                provider,
                client_order_id=client_order_id,
                fill=fill,
                unknown_submissions=(unresolved,),
            )
            self.assertTrue(reconciled.complete)
            self.assertEqual(
                reconciled.submission_resolutions[0].outcome,
                "OBSERVED_EXECUTION",
            )

            filled = orders.record_fill(
                event_key="reconciled-fill",
                client_order_id=client_order_id,
                fill_id=fill["fill_id"],
                provider_execution_id=fill["provider_execution_id"],
                quantity=fill["last_quantity"]["value"],
                price=fill["last_price"],
                committed_at=fill["receipt_time"],
            )
            self.assertEqual(filled.snapshot.state, "FILLED")
            self.assertIsNone(filled.snapshot.provider_order_id)

            retry = dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id=intent_id,
                intent_hash="sha256:" + "b" * 64,
                provider="simulated",
                request=request,
                now=LATER,
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=provider.transport_send,
            )
            self.assertEqual(retry.status, "UNKNOWN")
            self.assertEqual(provider.outbound_request_count, 1)
            resync = orders.sync_submission_attempt(attempt_id=attempt_id)
            self.assertEqual(len(resync), 2)
            self.assertTrue(all(not item.inserted for item in resync))

            restarted = projection(store)
            restored = restarted.order(client_order_id).snapshot()
            self.assertEqual(restored.state, "FILLED")
            self.assertEqual(restored.filled_quantity, Decimal("1"))
            self.assertIsNone(restored.provider_order_id)
            self.assertEqual(restored.submission_attempt_id, attempt_id)

    def test_blocked_dispatch_does_not_invent_external_send_started_state(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            provider = SimulatedProvider(account_id=ACCOUNT)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id=ACCOUNT,
                owner_token="sim-owner",
            )
            intent_id = "intent-projected-blocked"
            client_order_id = stable_client_order_id(
                "simulated",
                intent_id,
                environment="SIMULATION",
                account_id=ACCOUNT,
            )
            orders = projection(store)
            orders.create_order(
                event_key="intent-created:blocked",
                client_order_id=client_order_id,
                instrument=INSTRUMENT,
                side="BUY",
                requested_quantity="1",
                committed_at=NOW,
            )
            attempt_id = "33333333-3333-4333-8333-333333333333"
            blocked = dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id=intent_id,
                intent_hash="sha256:" + "c" * 64,
                provider="simulated",
                request={
                    "attempt_id": attempt_id,
                    "instrument_version": INSTRUMENT,
                    "side": "BUY",
                    "quantity": "1",
                    "price": "100",
                    "now": NOW,
                },
                now=NOW,
                authority_check=lambda _intent_hash, _now: (
                    False,
                    "policy-blocked",
                ),
                transport_send=provider.transport_send,
            )
            self.assertEqual(blocked.status, "BLOCKED")
            self.assertEqual(provider.outbound_request_count, 0)
            self.assertEqual(
                orders.sync_submission_attempt(attempt_id=attempt_id),
                (),
            )
            snap = orders.order(client_order_id).snapshot()
            self.assertEqual(snap.state, "PENDING")
            self.assertIsNone(snap.submission_attempt_id)


if __name__ == "__main__":
    unittest.main()
