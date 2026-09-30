from decimal import Decimal
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    stable_client_order_id,
    submission_attempt_aggregate_id,
)
from mvp.autotrade_mvp.durable_order_projection import (
    DurableOrderBookProjection,
)
from mvp.autotrade_mvp.order_projection import OrderProjectionConflict
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
    def test_sha_bound_exact_json_submission_projects_ack_without_inventing_fill(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            provider = SimulatedProvider(
                account_id=ACCOUNT, initial_cash="1000", fee_rate="0.001"
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id=ACCOUNT,
                owner_token="sim-owner",
            )
            intent_id = "intent-exact-raw-projection"
            client_order_id = stable_client_order_id(
                "simulated", intent_id,
                environment="SIMULATION", account_id=ACCOUNT,
            )
            orders = projection(store)
            orders.create_order(
                event_key="intent-created:exact-raw",
                client_order_id=client_order_id,
                instrument=INSTRUMENT,
                side="BUY",
                requested_quantity="2",
                committed_at=NOW,
            )
            attempt_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
            request = {
                "attempt_id": attempt_id,
                "instrument_version": INSTRUMENT,
                "side": "BUY",
                "quantity": "2",
                "price": "100",
                "now": NOW,
                "fill_immediately": False,
            }

            def exact_send(cid, frozen_request, final_guard):
                provider_reply = provider.transport_send(
                    cid, frozen_request, final_guard
                )
                exact_text = json.dumps(
                    provider_reply,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                self.assertTrue(exact_text.endswith("}"))
                # Extra exact provider field exercises Decimal conversion
                # without changing the simulated ACK identity/evidence.
                exact_text = exact_text[:-1] + ',"diagnostic_price":65000.10}'
                return ExactJsonTransportResponse(exact_text.encode("utf-8"))

            outcome = dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id=intent_id,
                intent_hash="sha256:" + "b" * 64,
                provider="simulated",
                request=request,
                now=NOW,
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=exact_send,
            )
            self.assertEqual(outcome.status, "SENT")
            self.assertEqual(
                outcome.response["diagnostic_price"].as_tuple(),
                Decimal("65000.10").as_tuple(),
            )
            terminal = store.load_events(
                "submission_attempt",
                submission_attempt_aggregate_id(
                    environment="SIMULATION",
                    account_id=ACCOUNT,
                    attempt_id=attempt_id,
                ),
            )[-1]
            self.assertEqual(terminal["event_type"], "SubmissionSent")
            self.assertNotIn("response", terminal["payload"])
            self.assertEqual(
                terminal["payload"]["response_encoding"], "utf-8-json"
            )
            # Simulate a corrupt *read of the original journal evidence*,
            # without mutating the real durable event. No forged ACK/fill
            # may be projected from SHA-mismatched exact provider bytes.
            aggregate_id = submission_attempt_aggregate_id(
                environment="SIMULATION",
                account_id=ACCOUNT,
                attempt_id=attempt_id,
            )
            original_load_events = store.load_events

            def corrupt_exact_source(aggregate_type, selected_id, *args, **kwargs):
                events = original_load_events(
                    aggregate_type, selected_id, *args, **kwargs
                )
                if aggregate_type != "submission_attempt" or selected_id != aggregate_id:
                    return events
                corrupted = list(events)
                sent = dict(corrupted[-1])
                sent_payload = dict(sent["payload"])
                sent_payload["response_sha256"] = "sha256:" + "0" * 64
                sent["payload"] = sent_payload
                corrupted[-1] = sent
                return corrupted

            with patch.object(
                store, "load_events", side_effect=corrupt_exact_source
            ):
                with self.assertRaisesRegex(
                    OrderProjectionConflict,
                    "exact submission response evidence is invalid",
                ):
                    orders.sync_submission_attempt(attempt_id=attempt_id)
            self.assertEqual(
                orders.order(client_order_id).snapshot().state,
                "SEND_STARTED",
            )
            self.assertEqual(
                orders.order(client_order_id).snapshot().filled_quantity,
                Decimal("0"),
            )
            # The same original SHA-bound journal source is still valid.
            # Replaying its already-projected send-start fact must be
            # idempotent and yield exactly one WORKING ACK, never a fill.
            projected = orders.sync_submission_attempt(attempt_id=attempt_id)
            self.assertEqual(len(projected), 2)
            self.assertEqual(projected[0].snapshot.state, "SEND_STARTED")
            self.assertEqual(projected[1].snapshot.state, "WORKING")
            self.assertEqual(
                projected[1].snapshot.provider_order_id,
                outcome.response["provider_order_id"],
            )
            self.assertEqual(
                projected[1].snapshot.filled_quantity, Decimal("0")
            )
            restarted = projection(store).order(client_order_id).snapshot()
            self.assertEqual(restarted.state, "WORKING")
            self.assertEqual(restarted.filled_quantity, Decimal("0"))
            self.assertEqual(provider.outbound_request_count, 1)

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


    def test_partial_exact_submission_markers_never_project_legacy_ack(self):
        variants = ("text-only", "hash-only", "encoding-only", "wrong-encoding", "mixed")
        for variant in variants:
            with self.subTest(variant=variant), TemporaryDirectory() as directory:
                store = JournalStore(f"{directory}/journal.sqlite3")
                dispatcher = GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id=ACCOUNT,
                    owner_token="sim-owner",
                )
                intent_id = "intent-partial-exact-" + variant
                attempt_id = "attempt-partial-exact-" + variant
                client_order_id = stable_client_order_id(
                    "simulated", intent_id,
                    environment="SIMULATION", account_id=ACCOUNT,
                )
                orders = projection(store)
                orders.create_order(
                    event_key="intent-created:" + variant,
                    client_order_id=client_order_id,
                    instrument=INSTRUMENT,
                    side="BUY",
                    requested_quantity="1",
                    committed_at=NOW,
                )
                response = {
                    "attempt_id": attempt_id,
                    "client_order_id": client_order_id,
                    "provider_order_id": "provider-" + variant,
                    "outcome": "ACKNOWLEDGED",
                }
                raw = json.dumps(
                    response,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
                dispatched = dispatcher.dispatch(
                    attempt_id=attempt_id,
                    intent_id=intent_id,
                    intent_hash="sha256:" + "d" * 64,
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
                    authority_check=lambda _hash, _now: (True, "allowed"),
                    transport_send=lambda _cid, _request, guard: (
                        guard() or ExactJsonTransportResponse(raw)
                    ),
                )
                self.assertEqual(dispatched.status, "SENT")
                aggregate_id = submission_attempt_aggregate_id(
                    environment="SIMULATION",
                    account_id=ACCOUNT,
                    attempt_id=attempt_id,
                )
                original_load_events = store.load_events
                exact_events = original_load_events("submission_attempt", aggregate_id)
                exact_payload = dict(exact_events[-1]["payload"])

                if variant == "text-only":
                    replacement = {
                        "client_order_id": client_order_id,
                        "response_text": exact_payload["response_text"],
                    }
                elif variant == "hash-only":
                    replacement = {
                        "client_order_id": client_order_id,
                        "response_sha256": exact_payload["response_sha256"],
                    }
                elif variant == "encoding-only":
                    replacement = {
                        "client_order_id": client_order_id,
                        "response_encoding": "utf-8-json",
                    }
                elif variant == "wrong-encoding":
                    replacement = dict(exact_payload)
                    replacement["response_encoding"] = "json"
                else:
                    replacement = {
                        "client_order_id": client_order_id,
                        "response_text": exact_payload["response_text"],
                        "response": response,
                    }

                def partial_exact_source(aggregate_type, selected_id, *args, **kwargs):
                    events = original_load_events(
                        aggregate_type, selected_id, *args, **kwargs
                    )
                    if aggregate_type != "submission_attempt" or selected_id != aggregate_id:
                        return events
                    altered = list(events)
                    terminal = dict(altered[-1])
                    terminal["payload"] = replacement
                    altered[-1] = terminal
                    return altered

                with patch.object(
                    store, "load_events", side_effect=partial_exact_source
                ):
                    with self.assertRaisesRegex(
                        OrderProjectionConflict,
                        "exact submission response evidence is (invalid|unavailable)",
                    ):
                        orders.sync_submission_attempt(attempt_id=attempt_id)
                snapshot = orders.order(client_order_id).snapshot()
                self.assertEqual(snapshot.state, "SEND_STARTED")
                self.assertEqual(snapshot.filled_quantity, Decimal("0"))



if __name__ == "__main__":
    unittest.main()
