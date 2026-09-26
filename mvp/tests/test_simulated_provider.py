from hashlib import sha256
import json
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.dispatch import GuardedDispatcher, stable_client_order_id
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.order_projection import OrderProjection
from mvp.autotrade_mvp.simulated_provider import (
    SimulatedProvider,
    SimulatedProviderConflict,
)


class SimulatedProviderTests(unittest.TestCase):
    def test_submission_fill_and_snapshot_are_deterministic(self):
        provider = SimulatedProvider(initial_cash="1000", fee_rate="0.001")
        attempt_id = str(uuid4())
        first = provider.submit_order(
            attempt_id=attempt_id,
            client_order_id="client-1",
            instrument_version="ABC@1",
            side="BUY",
            quantity="2",
            price="100",
            now="2026-09-24T18:00:00Z",
        )
        second = provider.submit_order(
            attempt_id=attempt_id,
            client_order_id="client-1",
            instrument_version="ABC@1",
            side="BUY",
            quantity="2",
            price="100",
            now="2026-09-24T18:00:00Z",
        )
        self.assertEqual(first, second)
        self.assertEqual(len(provider.activity_fills()), 1)
        snapshot = provider.account_snapshot(now="2026-09-24T18:01:00Z")
        self.assertEqual(snapshot["balances"][0]["total"], "799.8")
        self.assertEqual(snapshot["positions"][0]["quantity"]["value"], "2")

    def test_changed_attempt_or_client_identity_conflicts(self):
        provider = SimulatedProvider()
        attempt_id = str(uuid4())
        provider.submit_order(
            attempt_id=attempt_id,
            client_order_id="client-1",
            instrument_version="ABC@1",
            side="BUY",
            quantity="1",
            price="100",
            now="2026-09-24T18:00:00Z",
        )
        with self.assertRaises(SimulatedProviderConflict):
            provider.submit_order(
                attempt_id=attempt_id,
                client_order_id="client-1",
                instrument_version="ABC@1",
                side="BUY",
                quantity="2",
                price="100",
                now="2026-09-24T18:00:00Z",
            )

    def test_partial_fill_remains_open_with_exact_remaining_quantity(self):
        provider = SimulatedProvider(initial_cash="1000", fee_rate="0.001")
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="partial-1",
            instrument_version="ABC@1",
            side="BUY",
            quantity="4",
            price="100",
            now="2026-09-24T18:00:00Z",
            fill_immediately=False,
        )
        first = provider.record_fill(
            client_order_id="partial-1",
            provider_execution_id="exec-partial-1",
            quantity="1",
            now="2026-09-24T18:00:01Z",
        )
        self.assertEqual(first["last_quantity"]["value"], "1")
        self.assertEqual(provider.cash, provider.initial_cash - 100 - provider.fee_rate * 100)
        self.assertEqual(provider.positions["ABC@1"], 1)

        snapshot = provider.account_snapshot(now="2026-09-24T18:00:02Z")
        self.assertEqual(len(snapshot["open_orders"]), 1)
        open_order = snapshot["open_orders"][0]
        self.assertEqual(open_order["quantity"], "4")
        self.assertEqual(open_order["filled_quantity"], "1")
        self.assertEqual(open_order["remaining_quantity"], "3")

        query = provider.query_order(
            client_order_id="partial-1",
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=True,
            now="2026-09-24T19:00:00Z",
        )
        self.assertEqual(query["order"]["status"], "PARTIALLY_FILLED")
        self.assertEqual(query["order"]["filled_quantity"], "1")
        self.assertEqual(query["order"]["remaining_quantity"], "3")

    def test_provider_execution_retry_is_idempotent_and_conflict_safe(self):
        provider = SimulatedProvider(initial_cash="1000")
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="retry-fill",
            instrument_version="ABC@1",
            side="BUY",
            quantity="3",
            price="100",
            now="2026-09-24T18:00:00Z",
            fill_immediately=False,
        )
        first = provider.record_fill(
            client_order_id="retry-fill",
            provider_execution_id="exec-stable",
            quantity="1",
            price="101",
            now="2026-09-24T18:00:01Z",
        )
        cash_after_first = provider.cash
        position_after_first = provider.positions["ABC@1"]
        second = provider.record_fill(
            client_order_id="retry-fill",
            provider_execution_id="exec-stable",
            quantity="1",
            price="101",
            now="2026-09-24T18:00:01Z",
        )
        self.assertEqual(first, second)
        self.assertEqual(provider.cash, cash_after_first)
        self.assertEqual(provider.positions["ABC@1"], position_after_first)
        self.assertEqual(len(provider.activity_fills()), 1)

        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "different simulated fill content",
        ):
            provider.record_fill(
                client_order_id="retry-fill",
                provider_execution_id="exec-stable",
                quantity="2",
                price="101",
                now="2026-09-24T18:00:01Z",
            )
        self.assertEqual(provider.cash, cash_after_first)
        self.assertEqual(provider.positions["ABC@1"], position_after_first)
        self.assertEqual(len(provider.activity_fills()), 1)

    def test_partial_fill_overfill_is_rejected_before_economic_mutation(self):
        provider = SimulatedProvider(initial_cash="1000")
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="overfill",
            instrument_version="ABC@1",
            side="BUY",
            quantity="2",
            price="100",
            now="2026-09-24T18:00:00Z",
            fill_immediately=False,
        )
        provider.record_fill(
            client_order_id="overfill",
            provider_execution_id="exec-1",
            quantity="1",
            now="2026-09-24T18:00:01Z",
        )
        cash_before = provider.cash
        position_before = provider.positions["ABC@1"]
        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "would overfill",
        ):
            provider.record_fill(
                client_order_id="overfill",
                provider_execution_id="exec-2",
                quantity="1.1",
                now="2026-09-24T18:00:02Z",
            )
        self.assertEqual(provider.cash, cash_before)
        self.assertEqual(provider.positions["ABC@1"], position_before)
        self.assertEqual(len(provider.activity_fills()), 1)

    def test_second_partial_execution_can_complete_order(self):
        provider = SimulatedProvider(initial_cash="1000")
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="complete-partials",
            instrument_version="ABC@1",
            side="BUY",
            quantity="2",
            price="100",
            now="2026-09-24T18:00:00Z",
            fill_immediately=False,
        )
        provider.record_fill(
            client_order_id="complete-partials",
            provider_execution_id="exec-a",
            quantity="0.75",
            now="2026-09-24T18:00:01Z",
        )
        provider.record_fill(
            client_order_id="complete-partials",
            provider_execution_id="exec-b",
            quantity="1.25",
            now="2026-09-24T18:00:02Z",
        )
        snapshot = provider.account_snapshot(now="2026-09-24T18:00:03Z")
        self.assertEqual(snapshot["open_orders"], [])
        query = provider.query_order(
            client_order_id="complete-partials",
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=True,
            now="2026-09-24T19:00:00Z",
        )
        self.assertEqual(query["order"]["status"], "FILLED")
        self.assertEqual(query["order"]["filled_quantity"], "2")
        self.assertEqual(query["order"]["remaining_quantity"], "0")

    def test_cancel_evidence_digest_binds_acknowledged_verdict(self):
        provider = SimulatedProvider(initial_cash="1000")
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="cancel-evidence",
            instrument_version="ABC@1",
            side="BUY",
            quantity="2",
            price="100",
            now="2026-09-24T18:00:00Z",
            fill_immediately=False,
        )
        cancel = provider.cancel_order(
            client_order_id="cancel-evidence",
            now="2026-09-24T18:00:01Z",
        )
        evidence = cancel["evidence"][0]
        sealed_payload = {
            key: value
            for key, value in cancel.items()
            if key != "evidence"
        }
        encoded = json.dumps(
            sealed_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(cancel["outcome"], "ACKNOWLEDGED")
        self.assertEqual(
            evidence["sha256"],
            "sha256:" + sha256(encoded).hexdigest(),
        )

    def test_cancel_evidence_digest_binds_rejected_verdict_and_reason(self):
        provider = SimulatedProvider(initial_cash="1000")
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="cancel-rejected-evidence",
            instrument_version="ABC@1",
            side="BUY",
            quantity="1",
            price="100",
            now="2026-09-24T18:00:00Z",
            fill_immediately=True,
        )
        cancel = provider.cancel_order(
            client_order_id="cancel-rejected-evidence",
            now="2026-09-24T18:00:01Z",
        )
        evidence = cancel["evidence"][0]
        sealed_payload = {
            key: value
            for key, value in cancel.items()
            if key != "evidence"
        }
        encoded = json.dumps(
            sealed_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(cancel["outcome"], "REJECTED")
        self.assertEqual(cancel["reason_code"], "ALREADY_FILLED")
        self.assertEqual(
            evidence["sha256"],
            "sha256:" + sha256(encoded).hexdigest(),
        )

    def test_cancel_fill_race_preserves_fill_then_closes_remaining_order(self):
        provider = SimulatedProvider(initial_cash="1000", fee_rate="0.001")
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="race-1",
            instrument_version="ABC@1",
            side="BUY",
            quantity="3",
            price="100",
            now="2026-09-24T18:00:00Z",
            fill_immediately=False,
        )
        cancel = provider.cancel_order(
            client_order_id="race-1",
            now="2026-09-24T18:00:01Z",
            race_execution_id="exec-race",
            race_fill_quantity="1",
        )
        self.assertEqual(cancel["outcome"], "ACKNOWLEDGED")
        self.assertEqual(cancel["filled_quantity"], "1")
        self.assertEqual(cancel["remaining_quantity"], "2")
        self.assertEqual(cancel["race_execution_id"], "exec-race")
        self.assertEqual(len(provider.activity_fills()), 1)
        self.assertEqual(provider.positions["ABC@1"], 1)
        self.assertEqual(provider.cash, provider.initial_cash - 100 - provider.fee_rate * 100)
        self.assertEqual(
            provider.account_snapshot(now="2026-09-24T18:00:02Z")["open_orders"],
            [],
        )

        query = provider.query_order(
            client_order_id="race-1",
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=True,
            now="2026-09-24T19:00:00Z",
        )
        self.assertEqual(query["order"]["status"], "CANCELLED")
        self.assertEqual(query["order"]["filled_quantity"], "1")
        self.assertEqual(query["order"]["remaining_quantity"], "2")
        self.assertEqual(query["order"]["cancelled_at"], "2026-09-24T18:00:01Z")

        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "cancelled simulated order",
        ):
            provider.record_fill(
                client_order_id="race-1",
                provider_execution_id="exec-too-late",
                quantity="1",
                now="2026-09-24T18:00:02Z",
            )

    def test_cancel_loses_race_when_fill_consumes_entire_remainder(self):
        provider = SimulatedProvider(initial_cash="1000")
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="race-full",
            instrument_version="ABC@1",
            side="BUY",
            quantity="1",
            price="100",
            now="2026-09-24T18:00:00Z",
            fill_immediately=False,
        )
        cancel = provider.cancel_order(
            client_order_id="race-full",
            now="2026-09-24T18:00:01Z",
            race_execution_id="exec-race-full",
            race_fill_quantity="1",
        )
        self.assertEqual(cancel["outcome"], "REJECTED")
        self.assertEqual(cancel["reason_code"], "ALREADY_FILLED")
        self.assertEqual(cancel["filled_quantity"], "1")
        self.assertEqual(cancel["remaining_quantity"], "0")
        self.assertNotIn("cancelled_at", cancel)

        query = provider.query_order(
            client_order_id="race-full",
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=True,
            now="2026-09-24T19:00:00Z",
        )
        self.assertEqual(query["order"]["status"], "FILLED")
        self.assertNotIn("cancelled_at", query["order"])
        self.assertEqual(
            provider.account_snapshot(now="2026-09-24T18:00:02Z")["open_orders"],
            [],
        )

    def test_cancel_fill_race_drives_canonical_order_projection_without_erasing_fill(self):
        provider = SimulatedProvider(initial_cash="1000")
        attempt_id = str(uuid4())
        submission = provider.submit_order(
            attempt_id=attempt_id,
            client_order_id="projection-race",
            instrument_version="ABC@1",
            side="BUY",
            quantity="3",
            price="100",
            now="2026-09-24T18:00:00Z",
            fill_immediately=False,
        )
        projection = OrderProjection(
            provider_id="SIMULATED",
            account_id="sim-account",
            environment="SIMULATION",
            client_order_id="projection-race",
            instrument="ABC@1",
            side="BUY",
            requested_quantity="3",
        )
        projection.mark_send_started(attempt_id=attempt_id)
        projection.acknowledge(
            attempt_id=attempt_id,
            provider_order_id=submission["provider_order_id"],
            status=submission["outcome"],
        )
        self.assertEqual(projection.state, "WORKING")

        first = provider.record_fill(
            client_order_id="projection-race",
            provider_execution_id="exec-before-cancel",
            quantity="1",
            now="2026-09-24T18:00:01Z",
        )
        projection.record_fill(
            fill_id=first["fill_id"],
            provider_execution_id=first["provider_execution_id"],
            quantity=first["last_quantity"]["value"],
            price=first["last_price"],
        )
        self.assertEqual(projection.state, "PARTIALLY_FILLED")

        projection.request_cancel(command_id="cancel-projection-race")
        cancel = provider.cancel_order(
            client_order_id="projection-race",
            now="2026-09-24T18:00:02Z",
            race_execution_id="exec-during-cancel",
            race_fill_quantity="0.5",
        )
        race_fill = provider.activity_fills()[-1]
        projection.record_fill(
            fill_id=race_fill["fill_id"],
            provider_execution_id=race_fill["provider_execution_id"],
            quantity=race_fill["last_quantity"]["value"],
            price=race_fill["last_price"],
        )
        self.assertEqual(projection.state, "PARTIALLY_FILLED_CANCEL_REQUESTED")
        self.assertEqual(cancel["outcome"], "ACKNOWLEDGED")
        projection.confirm_cancel()

        snapshot = projection.snapshot()
        self.assertEqual(snapshot.state, "PARTIALLY_FILLED_CANCELLED")
        self.assertEqual(str(snapshot.filled_quantity), "1.5")
        self.assertEqual(str(snapshot.open_quantity), "1.5")
        self.assertTrue(snapshot.cancel_confirmed)
        self.assertEqual(
            provider.query_order(
                client_order_id="projection-race",
                coverage_start="2026-09-24T17:00:00Z",
                coverage_end="2026-09-24T19:00:00Z",
                pagination_complete=True,
                now="2026-09-24T19:00:00Z",
            )["order"]["status"],
            "CANCELLED",
        )

    def test_fill_wins_cancel_race_resolves_projection_pending_action(self):
        provider = SimulatedProvider(initial_cash="1000")
        attempt_id = str(uuid4())
        submission = provider.submit_order(
            attempt_id=attempt_id,
            client_order_id="projection-fill-wins",
            instrument_version="ABC@1",
            side="BUY",
            quantity="1",
            price="100",
            now="2026-09-24T18:00:00Z",
            fill_immediately=False,
        )
        projection = OrderProjection(
            provider_id="SIMULATED",
            account_id="sim-account",
            environment="SIMULATION",
            client_order_id="projection-fill-wins",
            instrument="ABC@1",
            side="BUY",
            requested_quantity="1",
        )
        projection.mark_send_started(attempt_id=attempt_id)
        projection.acknowledge(
            attempt_id=attempt_id,
            provider_order_id=submission["provider_order_id"],
            status=submission["outcome"],
        )
        projection.request_cancel(command_id="cancel-fill-wins")

        cancel = provider.cancel_order(
            client_order_id="projection-fill-wins",
            now="2026-09-24T18:00:01Z",
            race_execution_id="exec-fill-wins",
            race_fill_quantity="1",
        )
        self.assertEqual(cancel["outcome"], "REJECTED")
        self.assertEqual(cancel["reason_code"], "ALREADY_FILLED")

        fill = provider.activity_fills()[-1]
        projection.record_fill(
            fill_id=fill["fill_id"],
            provider_execution_id=fill["provider_execution_id"],
            quantity=fill["last_quantity"]["value"],
            price=fill["last_price"],
        )
        self.assertEqual(projection.state, "FILLED")
        self.assertTrue(projection.snapshot().cancel_requested)

        projection.reject_cancel(
            command_id="cancel-fill-wins",
            reason_code=cancel["reason_code"],
        )
        snapshot = projection.snapshot()
        self.assertEqual(snapshot.state, "FILLED")
        self.assertFalse(snapshot.cancel_requested)
        self.assertFalse(snapshot.cancel_confirmed)
        self.assertIsNone(snapshot.cancel_command_id)

    def test_cancel_retry_is_idempotent_but_changed_race_semantics_conflict(self):
        provider = SimulatedProvider()
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="cancel-retry",
            instrument_version="ABC@1",
            side="SELL",
            quantity="2",
            price="100",
            now="2026-09-24T18:00:00Z",
            fill_immediately=False,
        )
        first = provider.cancel_order(
            client_order_id="cancel-retry",
            now="2026-09-24T18:00:01Z",
        )
        second = provider.cancel_order(
            client_order_id="cancel-retry",
            now="2026-09-24T18:00:05Z",
        )
        self.assertEqual(first, second)
        self.assertEqual(provider.activity_fills(), ())
        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "changed deterministic race semantics",
        ):
            provider.cancel_order(
                client_order_id="cancel-retry",
                now="2026-09-24T18:00:05Z",
                race_execution_id="late-race",
                race_fill_quantity="1",
            )

    def test_cancel_race_retry_canonicalizes_implicit_order_price(self):
        provider = SimulatedProvider()
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="race-price",
            instrument_version="ABC@1",
            side="SELL",
            quantity="2",
            price="100",
            now="2026-09-24T18:00:00Z",
            fill_immediately=False,
        )
        first = provider.cancel_order(
            client_order_id="race-price",
            now="2026-09-24T18:00:01Z",
            race_execution_id="exec-race-price",
            race_fill_quantity="0.5",
        )
        second = provider.cancel_order(
            client_order_id="race-price",
            now="2026-09-24T18:00:02Z",
            race_execution_id="exec-race-price",
            race_fill_quantity="0.5",
            race_fill_price="100",
        )
        self.assertEqual(first, second)
        self.assertEqual(len(provider.activity_fills()), 1)

    def test_cancel_rejects_orphan_race_price_before_mutation(self):
        provider = SimulatedProvider()
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="race-price-invalid",
            instrument_version="ABC@1",
            side="BUY",
            quantity="1",
            price="100",
            now="2026-09-24T18:00:00Z",
            fill_immediately=False,
        )
        with self.assertRaisesRegex(
            ValueError,
            "race_fill_price requires a deterministic race execution",
        ):
            provider.cancel_order(
                client_order_id="race-price-invalid",
                now="2026-09-24T18:00:01Z",
                race_fill_price="101",
            )
        self.assertEqual(provider.activity_fills(), ())
        self.assertEqual(
            len(provider.account_snapshot(now="2026-09-24T18:00:02Z")["open_orders"]),
            1,
        )

    def test_query_order_never_claims_absence_with_incomplete_pagination(self):
        provider = SimulatedProvider()
        result = provider.query_order(
            client_order_id="missing",
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=False,
            now="2026-09-24T19:00:00Z",
        )
        self.assertEqual(result["verdict"], "INCONCLUSIVE")
        complete = provider.query_order(
            client_order_id="missing",
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=True,
            now="2026-09-24T19:00:00Z",
        )
        self.assertEqual(complete["verdict"], "PROVEN_ABSENT")

    def test_dispatcher_calls_provider_once_after_final_guard(self):
        with TemporaryDirectory() as directory:
            provider = SimulatedProvider()
            dispatcher = GuardedDispatcher(
                JournalStore(f"{directory}/journal.sqlite3"),
                environment="SIMULATION",
                account_id="sim-account",
                owner_token="test-owner",
            )
            checks = 0

            def authority(intent_hash, now):
                nonlocal checks
                checks += 1
                return True, "allowed"

            attempt_id = str(uuid4())
            result = dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id=str(uuid4()),
                intent_hash="sha256:" + "1" * 64,
                provider="simulated",
                request={
                    "attempt_id": attempt_id,
                    "instrument_version": "ABC@1",
                    "side": "BUY",
                    "quantity": "1",
                    "price": "100",
                    "now": "2026-09-24T18:00:00Z",
                },
                now="2026-09-24T18:00:00Z",
                authority_check=authority,
                transport_send=provider.transport_send,
            )
            self.assertEqual(result.status, "SENT")
            self.assertEqual(provider.outbound_request_count, 1)
            self.assertEqual(checks, 2)

    def test_provider_rejects_truthy_fill_immediately_alias(self):
        provider = SimulatedProvider()
        with self.assertRaisesRegex(TypeError, "fill_immediately must be boolean"):
            provider.submit_order(
                attempt_id=str(uuid4()),
                client_order_id="truthy-fill",
                instrument_version="ABC@1",
                side="BUY",
                quantity="1",
                price="100",
                now="2026-09-24T18:00:00Z",
                fill_immediately=1,
            )
        self.assertEqual(provider.orders, {})
        self.assertEqual(provider.activity_fills(), ())

    def test_provider_rejects_binary_float_economics(self):
        provider = SimulatedProvider()
        with self.assertRaises(TypeError):
            provider.submit_order(
                attempt_id=str(uuid4()),
                client_order_id="client-1",
                instrument_version="ABC@1",
                side="BUY",
                quantity=1.0,
                price="100",
                now="2026-09-24T18:00:00Z",
            )


    def test_outage_before_final_guard_is_blocked_without_outbound_send(self):
        with TemporaryDirectory() as directory:
            intent_id = str(uuid4())
            attempt_id = str(uuid4())
            client_order_id = stable_client_order_id(
                "simulated",
                intent_id,
                environment="SIMULATION",
                account_id="sim-account",
            )
            provider = SimulatedProvider(
                transport_faults={client_order_id: "BEFORE_SEND_OUTAGE"}
            )
            dispatcher = GuardedDispatcher(
                JournalStore(f"{directory}/journal.sqlite3"),
                environment="SIMULATION",
                account_id="sim-account",
                owner_token="test-owner",
            )
            result = dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id=intent_id,
                intent_hash="sha256:" + "2" * 64,
                provider="simulated",
                request={
                    "attempt_id": attempt_id,
                    "instrument_version": "ABC@1",
                    "side": "BUY",
                    "quantity": "1",
                    "price": "100",
                    "now": "2026-09-24T18:00:00Z",
                },
                now="2026-09-24T18:00:00Z",
                authority_check=lambda *_: (True, "allowed"),
                transport_send=provider.transport_send,
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "transport_failed_before_send")
            self.assertEqual(provider.outbound_request_count, 0)
            self.assertEqual(provider.activity_fills(), ())

    def test_lost_response_after_provider_acceptance_becomes_unknown_and_never_resends(self):
        with TemporaryDirectory() as directory:
            intent_id = str(uuid4())
            attempt_id = str(uuid4())
            client_order_id = stable_client_order_id(
                "simulated",
                intent_id,
                environment="SIMULATION",
                account_id="sim-account",
            )
            provider = SimulatedProvider(
                transport_faults={client_order_id: "AFTER_ACCEPT_RESPONSE_LOST"}
            )
            dispatcher = GuardedDispatcher(
                JournalStore(f"{directory}/journal.sqlite3"),
                environment="SIMULATION",
                account_id="sim-account",
                owner_token="test-owner",
            )
            request = {
                "attempt_id": attempt_id,
                "instrument_version": "ABC@1",
                "side": "BUY",
                "quantity": "1",
                "price": "100",
                "now": "2026-09-24T18:00:00Z",
            }
            result = dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id=intent_id,
                intent_hash="sha256:" + "3" * 64,
                provider="simulated",
                request=request,
                now="2026-09-24T18:00:00Z",
                authority_check=lambda *_: (True, "allowed"),
                transport_send=provider.transport_send,
            )
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "transport_result_ambiguous")
            self.assertEqual(provider.outbound_request_count, 1)
            self.assertEqual(len(provider.activity_fills()), 1)

            found = provider.query_order(
                client_order_id=client_order_id,
                coverage_start="2026-09-24T17:00:00Z",
                coverage_end="2026-09-24T19:00:00Z",
                pagination_complete=True,
                now="2026-09-24T19:00:00Z",
            )
            self.assertEqual(found["verdict"], "FOUND")
            replay = dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id=intent_id,
                intent_hash="sha256:" + "3" * 64,
                provider="simulated",
                request=request,
                now="2026-09-24T19:00:00Z",
                authority_check=lambda *_: (True, "allowed"),
                transport_send=provider.transport_send,
            )
            self.assertEqual(replay.status, "UNKNOWN")
            self.assertEqual(provider.outbound_request_count, 1)


if __name__ == "__main__":
    unittest.main()
