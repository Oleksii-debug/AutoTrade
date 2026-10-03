from concurrent.futures import ThreadPoolExecutor, TimeoutError
from hashlib import sha256
import json
from decimal import (
    Decimal,
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    localcontext,
)
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from uuid import NAMESPACE_URL, uuid4, uuid5

from mvp.autotrade_mvp.dispatch import GuardedDispatcher, stable_client_order_id
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.order_projection import OrderProjection
from mvp.autotrade_mvp.simulated_provider import (
    SimulatedProvider,
    SimulatedProviderConflict,
)


class SimulatedProviderTests(unittest.TestCase):
    def test_restart_image_and_account_snapshot_wait_for_complete_fill_commit(self):
        provider = SimulatedProvider(initial_cash="1000", fee_rate="0")
        committing, release, exporting, snapshotting = (Event() for _ in range(4))

        class PausedFillList(list):
            def append(self, value):
                # Cash, position and order were written, but the fill is pending.
                committing.set()
                if not release.wait(5):
                    raise AssertionError("fill commit was not released")
                super().append(value)

        provider.fills = PausedFillList()

        def export():
            exporting.set()
            return provider.export_state()

        def snapshot():
            snapshotting.set()
            return provider.account_snapshot(now="2026-09-24T18:00:01Z")

        with ThreadPoolExecutor(max_workers=3) as executor:
            commit = executor.submit(
                provider.submit_order,
                attempt_id=str(uuid4()), client_order_id="atomic-cut",
                instrument_version="ABC@1", side="BUY", quantity="1",
                price="100", now="2026-09-24T18:00:00Z",
            )
            self.assertTrue(committing.wait(2))
            image_future = executor.submit(export)
            snapshot_future = executor.submit(snapshot)
            try:
                self.assertTrue(exporting.wait(2))
                self.assertTrue(snapshotting.wait(2))
                with self.assertRaises(TimeoutError):
                    image_future.result(timeout=0.05)
                with self.assertRaises(TimeoutError):
                    snapshot_future.result(timeout=0.05)
            finally:
                release.set()
            self.assertEqual(commit.result(timeout=2)["outcome"], "ACKNOWLEDGED")
            image = image_future.result(timeout=2)
            snapshot = snapshot_future.result(timeout=2)
        self.assertEqual(SimulatedProvider.from_state(image).export_state(), image)
        self.assertEqual(snapshot["balances"][0]["total"], "900")
        self.assertEqual(snapshot["positions"][0]["quantity"]["value"], "1")
        self.assertEqual(len(image["fills"]), 1)

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
            now="2026-09-24T20:05:00+02:00",
        )
        self.assertEqual(first, second)
        self.assertEqual(first["provider_received_at"], "2026-09-24T18:00:00Z")
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

    def test_provider_execution_retry_remains_idempotent_after_later_fill(self):
        provider = SimulatedProvider(initial_cash="1000", fee_rate="0")
        provider.submit_order(
            attempt_id="00000000-0000-0000-0000-000000000033",
            client_order_id="retry-after-later-fill",
            instrument_version="ABC@1",
            side="BUY",
            quantity="3",
            price="10",
            now="2026-09-30T18:00:00Z",
            fill_immediately=False,
        )
        first = provider.record_fill(
            client_order_id="retry-after-later-fill",
            provider_execution_id="exec-retry-first",
            quantity="1",
            now="2026-09-30T18:01:00Z",
        )
        provider.record_fill(
            client_order_id="retry-after-later-fill",
            provider_execution_id="exec-retry-later",
            quantity="1",
            now="2026-09-30T18:02:00Z",
        )
        before_retry = provider.export_state()
        replay = provider.record_fill(
            client_order_id="retry-after-later-fill",
            provider_execution_id="exec-retry-first",
            quantity="1",
            now="2026-09-30T18:01:00Z",
        )
        self.assertEqual(replay, first)
        self.assertEqual(provider.export_state(), before_retry)

        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "fill observation cannot precede existing provider history",
        ):
            provider.record_fill(
                client_order_id="retry-after-later-fill",
                provider_execution_id="exec-retry-new-backdated",
                quantity="1",
                now="2026-09-30T18:01:30Z",
            )
        self.assertEqual(provider.export_state(), before_retry)

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

    def test_cancel_retry_remains_idempotent_across_clock_rollback(self):
        provider = SimulatedProvider()
        provider.submit_order(
            attempt_id="00000000-0000-0000-0000-000000000047",
            client_order_id="cancel-clock-rollback",
            instrument_version="ABC@1",
            side="SELL",
            quantity="2",
            price="100",
            now="2026-09-30T18:00:00Z",
            fill_immediately=False,
        )
        first = provider.cancel_order(
            client_order_id="cancel-clock-rollback",
            now="2026-09-30T18:00:01Z",
        )
        before = provider.export_state()

        replay = provider.cancel_order(
            client_order_id="cancel-clock-rollback",
            now="2026-09-30T17:59:59Z",
        )
        self.assertEqual(replay, first)
        self.assertEqual(provider.export_state(), before)

        fresh = SimulatedProvider()
        fresh.submit_order(
            attempt_id="00000000-0000-0000-0000-000000000048",
            client_order_id="cancel-clock-rollback-new",
            instrument_version="ABC@1",
            side="SELL",
            quantity="2",
            price="100",
            now="2026-09-30T18:00:00Z",
            fill_immediately=False,
        )
        fresh_before = fresh.export_state()
        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "cancellation cannot precede order submission",
        ):
            fresh.cancel_order(
                client_order_id="cancel-clock-rollback-new",
                now="2026-09-30T17:59:59Z",
            )
        self.assertEqual(fresh.export_state(), fresh_before)

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

    def test_query_order_never_claims_future_coverage_complete(self):
        provider = SimulatedProvider()
        result = provider.query_order(
            client_order_id="missing-future",
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=True,
            now="2026-09-24T18:00:00Z",
        )
        self.assertEqual(result["verdict"], "INCONCLUSIVE")
        self.assertEqual(
            result["reason_codes"],
            ["coverage_end_after_query_time"],
        )
        self.assertEqual(result["consistency_horizon"], "2026-09-24T18:00:00Z")

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


    def test_transport_local_validation_fails_before_final_guard_or_outbound_send(self):
        provider = SimulatedProvider(initial_cash="1000", fee_rate="0")
        guard_calls = 0

        def guard():
            nonlocal guard_calls
            guard_calls += 1

        cases = (
            {
                "attempt_id": "not-a-uuid",
                "instrument_version": "ABC@1",
                "side": "BUY",
                "quantity": "1",
                "price": "100",
                "now": "2026-09-30T18:00:00Z",
            },
            {
                "attempt_id": "00000000-0000-0000-0000-000000000049",
                "instrument_version": "ABC@1",
                "side": "BUY",
                "quantity": "1e999999999",
                "price": "100",
                "now": "2026-09-30T18:00:00Z",
            },
            {
                "attempt_id": "00000000-0000-0000-0000-000000000050",
                "instrument_version": "ABC@1",
                "side": "BUY",
                "quantity": "1",
                "price": "100",
                "now": "2026-09-30T18:00:00Z",
                "fill_immediately": 1,
            },
        )
        for index, request in enumerate(cases):
            with self.subTest(index=index), self.assertRaises((TypeError, ValueError)):
                provider.transport_send(
                    f"invalid-local-{index}",
                    request,
                    guard,
                )

        self.assertEqual(guard_calls, 0)
        self.assertEqual(provider.outbound_request_count, 0)
        self.assertEqual(provider.orders, {})
        self.assertEqual(provider.activity_fills(), ())
        self.assertEqual(provider.cash, Decimal("1000"))

    def test_transport_identity_conflicts_fail_before_final_guard(self):
        provider = SimulatedProvider(initial_cash="1000", fee_rate="0")
        provider.submit_order(
            attempt_id="00000000-0000-0000-0000-000000000052",
            client_order_id="transport-existing",
            instrument_version="ABC@1",
            side="BUY",
            quantity="1",
            price="10",
            now="2026-09-30T18:00:00Z",
            fill_immediately=False,
        )
        canonical = provider.export_state()
        guard_calls = 0

        def guard():
            nonlocal guard_calls
            guard_calls += 1

        conflict_requests = (
            (
                "transport-other-client",
                {
                    "attempt_id": "00000000-0000-0000-0000-000000000052",
                    "instrument_version": "ABC@1",
                    "side": "BUY",
                    "quantity": "1",
                    "price": "10",
                    "now": "2026-09-30T18:00:01Z",
                    "fill_immediately": False,
                },
            ),
            (
                "transport-existing",
                {
                    "attempt_id": "00000000-0000-0000-0000-000000000053",
                    "instrument_version": "ABC@1",
                    "side": "SELL",
                    "quantity": "1",
                    "price": "10",
                    "now": "2026-09-30T18:00:01Z",
                    "fill_immediately": False,
                },
            ),
            (
                "transport-existing",
                {
                    "attempt_id": "00000000-0000-0000-0000-000000000054",
                    "instrument_version": "ABC@1",
                    "side": "BUY",
                    "quantity": "1",
                    "price": "10",
                    "now": "2026-09-30T17:59:59Z",
                    "fill_immediately": False,
                },
            ),
        )
        for client_order_id, request in conflict_requests:
            with self.subTest(client_order_id=client_order_id), self.assertRaises(
                SimulatedProviderConflict
            ):
                provider.transport_send(client_order_id, request, guard)

        self.assertEqual(guard_calls, 0)
        self.assertEqual(provider.outbound_request_count, 0)
        self.assertEqual(provider.export_state(), canonical)

    def test_transport_fill_resource_overflow_fails_before_final_guard(self):
        maximum = "9" * 256
        provider = SimulatedProvider(initial_cash=maximum, fee_rate="0")
        guard_calls = 0

        def guard():
            nonlocal guard_calls
            guard_calls += 1

        with self.assertRaises(ValueError):
            provider.transport_send(
                "transport-overflow",
                {
                    "attempt_id": "00000000-0000-0000-0000-000000000051",
                    "instrument_version": "ABC@1",
                    "side": "SELL",
                    "quantity": "1",
                    "price": "1",
                    "now": "2026-09-30T18:00:00Z",
                },
                guard,
            )

        self.assertEqual(guard_calls, 0)
        self.assertEqual(provider.outbound_request_count, 0)
        self.assertEqual(provider.orders, {})
        self.assertEqual(provider.activity_fills(), ())
        self.assertEqual(str(provider.cash), maximum)

    def test_final_guard_cannot_mutate_provider_before_prepared_commit(self):
        provider = SimulatedProvider(initial_cash="1000", fee_rate="0")
        before = provider.export_state()
        nested_attempt = "00000000-0000-0000-0000-000000000061"

        def mutate_provider():
            provider.submit_order(
                attempt_id=nested_attempt,
                client_order_id="guard-interleaving-order",
                instrument_version="ABC@1",
                side="BUY",
                quantity="1",
                price="10",
                now="2026-09-30T18:00:00Z",
                fill_immediately=False,
            )

        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "fenced by the final send barrier",
        ):
            provider.transport_send(
                "outer-prepared-order",
                {
                    "attempt_id": "00000000-0000-0000-0000-000000000060",
                    "instrument_version": "ABC@1",
                    "side": "BUY",
                    "quantity": "1",
                    "price": "10",
                    "now": "2026-09-30T18:00:00Z",
                    "fill_immediately": True,
                },
                mutate_provider,
            )

        self.assertEqual(provider.outbound_request_count, 0)
        self.assertEqual(provider.export_state(), before)

    def test_transport_fault_client_order_keys_must_be_canonical(self):
        with self.assertRaisesRegex(
            ValueError,
            "unsupported deterministic fault",
        ):
            SimulatedProvider(
                transport_faults={
                    " spaced-client ": "AFTER_ACCEPT_RESPONSE_LOST",
                }
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


    def test_restart_state_preserves_partial_fill_cancel_and_idempotency(self):
        provider = SimulatedProvider(initial_cash="1000", fee_rate="0.001")
        original_attempt = str(uuid4())
        provider.submit_order(
            attempt_id=original_attempt,
            client_order_id="restart-order",
            instrument_version="ABC@1",
            side="BUY",
            quantity="3",
            price="100",
            now="2026-09-28T18:00:00Z",
            fill_immediately=False,
        )
        provider.record_fill(
            client_order_id="restart-order",
            provider_execution_id="restart-fill-1",
            quantity="1",
            now="2026-09-28T18:00:01Z",
        )
        cancel = provider.cancel_order(
            client_order_id="restart-order",
            now="2026-09-28T18:00:02Z",
            race_execution_id="restart-fill-2",
            race_fill_quantity="0.5",
            race_fill_price="101",
        )
        self.assertEqual(cancel["outcome"], "ACKNOWLEDGED")

        state = provider.export_state()
        restored = SimulatedProvider.from_state(state)
        self.assertEqual(restored.export_state(), state)
        self.assertEqual(restored.cash, provider.cash)
        self.assertEqual(restored.positions, provider.positions)
        self.assertEqual(restored.activity_fills(), provider.activity_fills())
        self.assertEqual(
            restored.query_order(
                client_order_id="restart-order",
                coverage_start="2026-09-28T17:00:00Z",
                coverage_end="2026-09-28T19:00:00Z",
                pagination_complete=True,
                now="2026-09-28T19:00:00Z",
            )["order"]["status"],
            "CANCELLED",
        )

        retry = restored.submit_order(
            attempt_id=original_attempt,
            client_order_id="restart-order",
            instrument_version="ABC@1",
            side="BUY",
            quantity="3",
            price="100",
            now="2026-09-28T20:00:00Z",
            fill_immediately=False,
        )
        self.assertEqual(retry["provider_received_at"], "2026-09-28T18:00:00Z")
        self.assertEqual(len(restored.activity_fills()), 2)
        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "cancelled simulated order",
        ):
            restored.record_fill(
                client_order_id="restart-order",
                provider_execution_id="restart-fill-too-late",
                quantity="0.5",
                now="2026-09-28T20:00:01Z",
            )

    def test_new_retry_attempt_cannot_precede_canonical_order_submission(self):
        provider = SimulatedProvider()
        original_attempt = "00000000-0000-0000-0000-000000000041"
        provider.submit_order(
            attempt_id=original_attempt,
            client_order_id="retry-causal-live",
            instrument_version="ABC@1",
            side="SELL",
            quantity="2",
            price="50",
            now="2026-09-30T18:00:00Z",
            fill_immediately=False,
        )
        before = provider.export_state()

        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "retry cannot precede canonical order submission",
        ):
            provider.submit_order(
                attempt_id="00000000-0000-0000-0000-000000000042",
                client_order_id="retry-causal-live",
                instrument_version="ABC@1",
                side="SELL",
                quantity="2",
                price="50",
                now="2026-09-30T17:59:59Z",
                fill_immediately=False,
            )

        self.assertEqual(provider.export_state(), before)

        same_time = provider.submit_order(
            attempt_id="00000000-0000-0000-0000-000000000043",
            client_order_id="retry-causal-live",
            instrument_version="ABC@1",
            side="SELL",
            quantity="2",
            price="50",
            now="2026-09-30T18:00:00Z",
            fill_immediately=False,
        )
        self.assertEqual(same_time["provider_received_at"], "2026-09-30T18:00:00Z")
        self.assertEqual(
            SimulatedProvider.from_state(provider.export_state()).export_state(),
            provider.export_state(),
        )

    def test_restart_rejects_rehashed_retry_attempt_before_order_submission(self):
        provider = SimulatedProvider()
        provider.submit_order(
            attempt_id="00000000-0000-0000-0000-000000000044",
            client_order_id="retry-causal-restart",
            instrument_version="ABC@1",
            side="BUY",
            quantity="1",
            price="10",
            now="2026-09-30T18:00:00Z",
            fill_immediately=False,
        )
        provider.submit_order(
            attempt_id="00000000-0000-0000-0000-000000000045",
            client_order_id="retry-causal-restart",
            instrument_version="ABC@1",
            side="BUY",
            quantity="1",
            price="10",
            now="2026-09-30T18:00:01Z",
            fill_immediately=False,
        )
        state = provider.export_state()
        retry = next(
            item
            for item in state["attempts"]
            if item["attempt_id"] == "00000000-0000-0000-0000-000000000045"
        )
        retry["submitted_at"] = "2026-09-30T17:59:59Z"
        body = {
            key: value
            for key, value in state.items()
            if key != "state_digest"
        }
        state["state_digest"] = (
            "sha256:"
            + sha256(
                json.dumps(
                    body,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
        )

        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "serialized attempt precedes canonical order submission",
        ):
            SimulatedProvider.from_state(state)

    def test_restart_state_preserves_distinct_retry_attempt_identity(self):
        provider = SimulatedProvider()
        first_attempt = str(uuid4())
        second_attempt = str(uuid4())
        provider.submit_order(
            attempt_id=first_attempt,
            client_order_id="retry-state",
            instrument_version="ABC@1",
            side="SELL",
            quantity="2",
            price="50",
            now="2026-09-28T18:00:00Z",
            fill_immediately=False,
        )
        provider.submit_order(
            attempt_id=second_attempt,
            client_order_id="retry-state",
            instrument_version="ABC@1",
            side="SELL",
            quantity="2",
            price="50",
            now="2026-09-28T18:00:05Z",
            fill_immediately=False,
        )

        restored = SimulatedProvider.from_state(provider.export_state())
        replay = restored.submit_order(
            attempt_id=second_attempt,
            client_order_id="retry-state",
            instrument_version="ABC@1",
            side="SELL",
            quantity="2",
            price="50",
            now="2026-09-28T21:00:00Z",
            fill_immediately=False,
        )
        self.assertEqual(replay["attempt_id"], second_attempt)
        self.assertEqual(replay["provider_received_at"], "2026-09-28T18:00:05Z")
        self.assertEqual(restored.activity_fills(), ())

    def test_restart_state_detects_digest_tamper_before_restore(self):
        provider = SimulatedProvider()
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="tamper-state",
            instrument_version="ABC@1",
            side="BUY",
            quantity="1",
            price="10",
            now="2026-09-28T18:00:00Z",
        )
        state = provider.export_state()
        state["cash"] = "999999"
        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "state digest mismatch",
        ):
            SimulatedProvider.from_state(state)

    def test_restart_state_rejects_recomputed_digest_with_false_financial_state(self):
        provider = SimulatedProvider(initial_cash="1000", fee_rate="0.001")
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="semantic-tamper",
            instrument_version="ABC@1",
            side="BUY",
            quantity="1",
            price="100",
            now="2026-09-28T18:00:00Z",
        )
        state = provider.export_state()
        state["cash"] = "1000"
        body = {
            key: value
            for key, value in state.items()
            if key != "state_digest"
        }
        encoded = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        state["state_digest"] = "sha256:" + sha256(encoded).hexdigest()
        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "cash does not match replayed",
        ):
            SimulatedProvider.from_state(state)

    def test_restart_state_rejects_fill_after_acknowledged_cancel(self):
        provider = SimulatedProvider()
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="cancel-tamper",
            instrument_version="ABC@1",
            side="BUY",
            quantity="2",
            price="100",
            now="2026-09-28T18:00:00Z",
            fill_immediately=False,
        )
        provider.cancel_order(
            client_order_id="cancel-tamper",
            now="2026-09-28T18:00:01Z",
        )
        state = provider.export_state()
        # Inject a canonical-looking later fill and recompute the outer digest.
        order = provider.orders["cancel-tamper"]
        late_provider = SimulatedProvider()
        late_provider.orders["cancel-tamper"] = order
        late_provider._attempts[order.attempt_id] = order
        late_fill = late_provider.record_fill(
            client_order_id="cancel-tamper",
            provider_execution_id="late-after-cancel",
            quantity="1",
            now="2026-09-28T18:00:02Z",
        )
        state["fills"].append(late_fill)
        state["cash"] = late_provider.export_state()["cash"]
        state["positions"] = late_provider.export_state()["positions"]
        body = {
            key: value
            for key, value in state.items()
            if key != "state_digest"
        }
        encoded = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        state["state_digest"] = "sha256:" + sha256(encoded).hexdigest()
        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "fill occurs after acknowledged cancellation",
        ):
            SimulatedProvider.from_state(state)


    def test_restart_detaches_nested_cancel_result_from_input_state(self):
        provider = SimulatedProvider()
        provider.submit_order(
            attempt_id="00000000-0000-0000-0000-000000000046",
            client_order_id="restart-cancel-input-alias",
            instrument_version="ABC@1",
            side="SELL",
            quantity="2",
            price="25",
            now="2026-09-30T18:00:00Z",
            fill_immediately=False,
        )
        provider.cancel_order(
            client_order_id="restart-cancel-input-alias",
            now="2026-09-30T18:00:01Z",
        )
        state = provider.export_state()
        restored = SimulatedProvider.from_state(state)
        canonical = restored.export_state()

        external_result = state["cancel_results"]["restart-cancel-input-alias"]
        external_result["evidence"][0]["sha256"] = "sha256:" + ("0" * 64)
        external_result["evidence"].append({"forged": True})

        self.assertEqual(restored.export_state(), canonical)
        retry = restored.cancel_order(
            client_order_id="restart-cancel-input-alias",
            now="2026-09-30T18:00:02Z",
        )
        self.assertEqual(
            retry,
            canonical["cancel_results"]["restart-cancel-input-alias"],
        )

    def test_restart_state_preserves_cancel_retry_contract(self):
        provider = SimulatedProvider()
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="cancel-restart-retry",
            instrument_version="ABC@1",
            side="SELL",
            quantity="2",
            price="25",
            now="2026-09-28T18:00:00Z",
            fill_immediately=False,
        )
        first = provider.cancel_order(
            client_order_id="cancel-restart-retry",
            now="2026-09-28T18:00:01Z",
            race_execution_id="cancel-restart-fill",
            race_fill_quantity="0.5",
        )
        restored = SimulatedProvider.from_state(provider.export_state())
        same = restored.cancel_order(
            client_order_id="cancel-restart-retry",
            now="2026-09-28T20:00:00Z",
            race_execution_id="cancel-restart-fill",
            race_fill_quantity="0.5",
            race_fill_price="25",
        )
        self.assertEqual(same, first)
        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "changed deterministic race semantics",
        ):
            restored.cancel_order(
                client_order_id="cancel-restart-retry",
                now="2026-09-28T20:00:01Z",
            )

    def test_restart_state_preserves_response_lost_transport_fault_and_remote_truth(self):
        client_order_id = "restart-lost-response"
        provider = SimulatedProvider(
            transport_faults={
                client_order_id: "AFTER_ACCEPT_RESPONSE_LOST",
            }
        )
        attempt_id = str(uuid4())
        with self.assertRaises(TimeoutError):
            provider.transport_send(
                client_order_id,
                {
                    "attempt_id": attempt_id,
                    "instrument_version": "ABC@1",
                    "side": "BUY",
                    "quantity": "1",
                    "price": "100",
                    "now": "2026-09-28T18:00:00Z",
                },
                lambda: None,
            )
        self.assertEqual(provider.outbound_request_count, 1)
        restored = SimulatedProvider.from_state(provider.export_state())
        self.assertEqual(restored.outbound_request_count, 1)
        self.assertEqual(len(restored.activity_fills()), 1)
        found = restored.query_order(
            client_order_id=client_order_id,
            coverage_start="2026-09-28T17:00:00Z",
            coverage_end="2026-09-28T19:00:00Z",
            pagination_complete=True,
            now="2026-09-28T19:00:00Z",
        )
        self.assertEqual(found["verdict"], "FOUND")
        self.assertEqual(found["order"]["status"], "FILLED")
        with self.assertRaises(TimeoutError):
            restored.transport_send(
                client_order_id,
                {
                    "attempt_id": attempt_id,
                    "instrument_version": "ABC@1",
                    "side": "BUY",
                    "quantity": "1",
                    "price": "100",
                    "now": "2026-09-28T20:00:00Z",
                },
                lambda: None,
            )
        self.assertEqual(restored.outbound_request_count, 2)
        self.assertEqual(len(restored.activity_fills()), 1)


    def test_restart_state_rejects_recomputed_cancel_request_race_rewrite(self):
        provider = SimulatedProvider()
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="cancel-request-tamper",
            instrument_version="ABC@1",
            side="BUY",
            quantity="2",
            price="100",
            now="2026-09-28T18:00:00Z",
            fill_immediately=False,
        )
        provider.cancel_order(
            client_order_id="cancel-request-tamper",
            now="2026-09-28T18:00:01Z",
            race_execution_id="race-canonical",
            race_fill_quantity="0.5",
        )
        state = provider.export_state()
        state["cancellation_requests"]["cancel-request-tamper"] = {
            "race_execution_id": "race-canonical",
            "race_fill_quantity": "0.75",
            "race_fill_price": "100",
        }
        body = {
            key: value
            for key, value in state.items()
            if key != "state_digest"
        }
        encoded = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        state["state_digest"] = "sha256:" + sha256(encoded).hexdigest()
        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "cancel race economics",
        ):
            SimulatedProvider.from_state(state)



    def test_restart_state_rejects_unknown_schema_fields_even_with_recomputed_digest(self):
        provider = SimulatedProvider(initial_cash="1000")
        state = provider.export_state()

        mutated = dict(state)
        mutated["unexpected_authority"] = {"enabled": True}
        body = {key: value for key, value in mutated.items() if key != "state_digest"}
        mutated["state_digest"] = "sha256:" + sha256(
            json.dumps(
                body,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(
            ValueError,
            "simulated provider state fields mismatch",
        ):
            SimulatedProvider.from_state(mutated)

        mutated = provider.export_state()
        mutated["config"] = dict(mutated["config"])
        mutated["config"]["unexpected"] = "ignored-before-repair"
        body = {key: value for key, value in mutated.items() if key != "state_digest"}
        mutated["state_digest"] = "sha256:" + sha256(
            json.dumps(
                body,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(
            ValueError,
            "simulated provider state config fields mismatch",
        ):
            SimulatedProvider.from_state(mutated)

    def test_restart_state_rejects_rehashed_unknown_cancel_result_fields(self):
        for outcome in ("ACKNOWLEDGED", "REJECTED"):
            with self.subTest(outcome=outcome):
                provider = SimulatedProvider()
                client_order_id = f"cancel-schema-{outcome.lower()}"
                provider.submit_order(
                    attempt_id=str(uuid4()),
                    client_order_id=client_order_id,
                    instrument_version="ABC@1",
                    side="BUY",
                    quantity="1",
                    price="100",
                    now="2026-09-30T18:00:00Z",
                    fill_immediately=outcome == "REJECTED",
                )
                result = provider.cancel_order(
                    client_order_id=client_order_id,
                    now="2026-09-30T18:00:01Z",
                )
                self.assertEqual(result["outcome"], outcome)

                state = provider.export_state()
                persisted = state["cancel_results"][client_order_id]
                persisted["future_authority"] = True
                sealed = {
                    key: value
                    for key, value in persisted.items()
                    if key != "evidence"
                }
                persisted["evidence"][0]["sha256"] = (
                    "sha256:"
                    + sha256(
                        json.dumps(
                            sealed,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                            allow_nan=False,
                        ).encode("utf-8")
                    ).hexdigest()
                )
                body = {
                    key: value
                    for key, value in state.items()
                    if key != "state_digest"
                }
                state["state_digest"] = (
                    "sha256:"
                    + sha256(
                        json.dumps(
                            body,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                            allow_nan=False,
                        ).encode("utf-8")
                    ).hexdigest()
                )
                with self.assertRaisesRegex(
                    SimulatedProviderConflict,
                    "cancel result has unsupported or missing fields",
                ):
                    SimulatedProvider.from_state(state)

    def test_restart_state_rejects_rehashed_cancel_quantity_lie(self):
        provider = SimulatedProvider()
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="cancel-quantity-tamper",
            instrument_version="ABC@1",
            side="BUY",
            quantity="2",
            price="100",
            now="2026-09-28T18:00:00Z",
            fill_immediately=False,
        )
        provider.record_fill(
            client_order_id="cancel-quantity-tamper",
            provider_execution_id="cancel-quantity-fill",
            quantity="0.5",
            now="2026-09-28T18:00:01Z",
        )
        provider.cancel_order(
            client_order_id="cancel-quantity-tamper",
            now="2026-09-28T18:00:02Z",
        )
        state = provider.export_state()
        result = state["cancel_results"]["cancel-quantity-tamper"]
        result["filled_quantity"] = "1"
        result["remaining_quantity"] = "1"
        sealed = {
            key: value
            for key, value in result.items()
            if key != "evidence"
        }
        result["evidence"][0]["sha256"] = (
            "sha256:"
            + sha256(
                json.dumps(
                    sealed,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
        )
        body = {
            key: value
            for key, value in state.items()
            if key != "state_digest"
        }
        state["state_digest"] = (
            "sha256:"
            + sha256(
                json.dumps(
                    body,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
        )
        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "cancel result quantities",
        ):
            SimulatedProvider.from_state(state)


    def test_restart_state_rejects_rehashed_unknown_schema_field(self):
        provider = SimulatedProvider()
        state = provider.export_state()
        state["future_authority"] = {"enabled": True}
        body = {
            key: value
            for key, value in state.items()
            if key != "state_digest"
        }
        state["state_digest"] = (
            "sha256:"
            + sha256(
                json.dumps(
                    body,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
        )
        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "unsupported or missing fields",
        ):
            SimulatedProvider.from_state(state)


    def test_restart_implicit_fill_and_financial_state_are_decimal_context_invariant(self):
        contexts = (
            (1, ROUND_FLOOR),
            (2, ROUND_CEILING),
            (6, ROUND_HALF_EVEN),
            (10, ROUND_FLOOR),
            (28, ROUND_CEILING),
            (80, ROUND_HALF_EVEN),
        )
        observed = []
        for precision, rounding in contexts:
            with self.subTest(precision=precision, rounding=rounding):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    provider = SimulatedProvider(
                        initial_cash="1000",
                        fee_rate="0.001",
                    )
                    provider.submit_order(
                        attempt_id="00000000-0000-0000-0000-000000000001",
                        client_order_id="context-invariant-fill",
                        instrument_version="ABC@1",
                        side="BUY",
                        quantity="1",
                        price="103",
                        now="2026-09-30T18:00:00Z",
                    )
                    state = provider.export_state()
                    restored = SimulatedProvider.from_state(state)
                    restored_state = restored.export_state()
                    snapshot = restored.account_snapshot(
                        now="2026-09-30T18:01:00Z"
                    )
                self.assertEqual(restored_state, state)
                self.assertEqual(state["cash"], "896.897")
                self.assertEqual(
                    state["fills"][0]["fees"][0]["amount"],
                    "0.103",
                )
                self.assertEqual(snapshot["balances"][0]["total"], "896.897")
                self.assertEqual(
                    snapshot["positions"][0]["quantity"]["value"],
                    "1",
                )
                observed.append((state, snapshot))
        self.assertTrue(all(item == observed[0] for item in observed[1:]))

    def test_exact_resource_failure_precedes_immediate_fill_provider_mutation(self):
        provider = SimulatedProvider(initial_cash="1000", fee_rate="0.001")
        before = provider.export_state()
        maximum = "9" * 256
        with self.assertRaises(ValueError):
            provider.submit_order(
                attempt_id="00000000-0000-0000-0000-000000000002",
                client_order_id="overflow-preflight",
                instrument_version="ABC@1",
                side="BUY",
                quantity=maximum,
                price=maximum,
                now="2026-09-30T18:00:00Z",
            )
        self.assertEqual(provider.export_state(), before)

    def test_decimal_subclass_is_rejected_before_virtual_financial_methods(self):
        calls = []

        class HostileDecimal(Decimal):
            def is_finite(self):
                calls.append("is_finite")
                raise AssertionError("hostile Decimal method executed")

            def as_tuple(self):
                calls.append("as_tuple")
                raise AssertionError("hostile Decimal method executed")

            def normalize(self, *args, **kwargs):
                calls.append("normalize")
                raise AssertionError("hostile Decimal method executed")

        with self.assertRaisesRegex(
            ValueError,
            "initial_cash must be a finite decimal",
        ):
            SimulatedProvider(initial_cash=HostileDecimal("1000"))
        self.assertEqual(calls, [])

        provider = SimulatedProvider()
        before = provider.export_state()
        with self.assertRaisesRegex(
            ValueError,
            "quantity must be a finite decimal",
        ):
            provider.submit_order(
                attempt_id="00000000-0000-0000-0000-000000000003",
                client_order_id="hostile-decimal",
                instrument_version="ABC@1",
                side="BUY",
                quantity=HostileDecimal("1"),
                price="10",
                now="2026-09-30T18:00:00Z",
            )
        self.assertEqual(calls, [])
        self.assertEqual(provider.export_state(), before)


    def test_restart_preserves_explicit_deterministic_looking_execution_identity(self):
        provider = SimulatedProvider()
        provider.submit_order(
            attempt_id="00000000-0000-0000-0000-000000000004",
            client_order_id="explicit-looks-implicit",
            instrument_version="ABC@1",
            side="BUY",
            quantity="1",
            price="10",
            now="2026-09-30T18:00:00Z",
            fill_immediately=False,
        )
        order = provider.orders["explicit-looks-implicit"]
        execution_id = (
            "exec-"
            + sha256(order.provider_order_id.encode("utf-8")).hexdigest()[:24]
        )
        fill = provider.record_fill(
            client_order_id="explicit-looks-implicit",
            provider_execution_id=execution_id,
            quantity="1",
            price="10",
            now="2026-09-30T18:00:01Z",
        )
        implicit_fill_id = str(
            uuid5(
                NAMESPACE_URL,
                "https://sim.autotrade.local/fill/" + order.provider_order_id,
            )
        )
        explicit_fill_id = str(
            uuid5(
                NAMESPACE_URL,
                "https://sim.autotrade.local/fill/" + execution_id,
            )
        )
        self.assertNotEqual(fill["fill_id"], implicit_fill_id)
        self.assertEqual(fill["fill_id"], explicit_fill_id)
        state = provider.export_state()
        restored = SimulatedProvider.from_state(state)
        self.assertEqual(restored.export_state(), state)
        self.assertEqual(restored.activity_fills()[0], fill)


    def test_provider_event_observation_cannot_move_backward(self):
        provider = SimulatedProvider(initial_cash="1000", fee_rate="0")
        provider.submit_order(
            attempt_id="00000000-0000-0000-0000-000000000021",
            client_order_id="causal-provider-history",
            instrument_version="ABC@1",
            side="BUY",
            quantity="3",
            price="10",
            now="2026-09-30T18:00:00Z",
            fill_immediately=False,
        )
        pristine = provider.export_state()
        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "fill cannot precede order submission",
        ):
            provider.record_fill(
                client_order_id="causal-provider-history",
                provider_execution_id="exec-before-submit",
                quantity="1",
                now="2026-09-30T17:59:59Z",
            )
        self.assertEqual(provider.export_state(), pristine)

        provider.record_fill(
            client_order_id="causal-provider-history",
            provider_execution_id="exec-first",
            quantity="1",
            now="2026-09-30T18:05:00Z",
        )
        after_fill = provider.export_state()
        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "fill observation cannot precede existing provider history",
        ):
            provider.record_fill(
                client_order_id="causal-provider-history",
                provider_execution_id="exec-backdated",
                quantity="1",
                now="2026-09-30T18:04:00Z",
            )
        self.assertEqual(provider.export_state(), after_fill)

        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "cancellation cannot precede existing fill observation",
        ):
            provider.cancel_order(
                client_order_id="causal-provider-history",
                now="2026-09-30T18:01:00Z",
            )
        self.assertEqual(provider.export_state(), after_fill)

        for suffix, race in (("plain", False), ("race", True)):
            with self.subTest(cancel=suffix):
                candidate = SimulatedProvider(initial_cash="1000", fee_rate="0")
                candidate.submit_order(
                    attempt_id=(
                        "00000000-0000-0000-0000-000000000022"
                        if not race
                        else "00000000-0000-0000-0000-000000000023"
                    ),
                    client_order_id=f"cancel-before-submit-{suffix}",
                    instrument_version="ABC@1",
                    side="BUY",
                    quantity="2",
                    price="10",
                    now="2026-09-30T18:00:00Z",
                    fill_immediately=False,
                )
                before = candidate.export_state()
                kwargs = {}
                if race:
                    kwargs = {
                        "race_execution_id": "exec-cancel-race-before-submit",
                        "race_fill_quantity": "1",
                    }
                with self.assertRaisesRegex(
                    SimulatedProviderConflict,
                    "cancellation cannot precede order submission",
                ):
                    candidate.cancel_order(
                        client_order_id=f"cancel-before-submit-{suffix}",
                        now="2026-09-30T17:59:59Z",
                        **kwargs,
                    )
                self.assertEqual(candidate.export_state(), before)

    def test_restart_rejects_rehashed_pre_submission_fill_and_cancel_chronology(self):
        def reseal(state):
            body = {
                key: value
                for key, value in state.items()
                if key != "state_digest"
            }
            state["state_digest"] = (
                "sha256:"
                + sha256(
                    json.dumps(
                        body,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest()
            )

        filled = SimulatedProvider(initial_cash="1000", fee_rate="0")
        filled.submit_order(
            attempt_id="00000000-0000-0000-0000-000000000024",
            client_order_id="restart-fill-causality",
            instrument_version="ABC@1",
            side="BUY",
            quantity="1",
            price="10",
            now="2026-09-30T18:00:00Z",
            fill_immediately=False,
        )
        filled.record_fill(
            client_order_id="restart-fill-causality",
            provider_execution_id="exec-restart-causality",
            quantity="1",
            now="2026-09-30T18:01:00Z",
        )
        fill_state = filled.export_state()
        fill_state["orders"][0]["submitted_at"] = "2026-09-30T18:02:00Z"
        fill_state["attempts"][0]["submitted_at"] = "2026-09-30T18:02:00Z"
        reseal(fill_state)
        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "fill cannot precede order submission",
        ):
            SimulatedProvider.from_state(fill_state)

        cancelled = SimulatedProvider(initial_cash="1000", fee_rate="0")
        cancelled.submit_order(
            attempt_id="00000000-0000-0000-0000-000000000025",
            client_order_id="restart-cancel-causality",
            instrument_version="ABC@1",
            side="BUY",
            quantity="1",
            price="10",
            now="2026-09-30T18:00:00Z",
            fill_immediately=False,
        )
        cancelled.cancel_order(
            client_order_id="restart-cancel-causality",
            now="2026-09-30T18:01:00Z",
        )
        cancel_state = cancelled.export_state()
        cancel_state["orders"][0]["submitted_at"] = "2026-09-30T18:02:00Z"
        cancel_state["attempts"][0]["submitted_at"] = "2026-09-30T18:02:00Z"
        reseal(cancel_state)
        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "serialized cancellation precedes order submission",
        ):
            SimulatedProvider.from_state(cancel_state)


    def test_fill_and_cancel_results_are_detached_from_canonical_provider_state(self):
        provider = SimulatedProvider(initial_cash="1000", fee_rate="0")
        provider.submit_order(
            attempt_id="00000000-0000-0000-0000-000000000034",
            client_order_id="detached-provider-facts",
            instrument_version="ABC@1",
            side="BUY",
            quantity="2",
            price="10",
            now="2026-09-30T18:00:00Z",
            fill_immediately=False,
        )
        fill = provider.record_fill(
            client_order_id="detached-provider-facts",
            provider_execution_id="exec-detached-provider-facts",
            quantity="1",
            now="2026-09-30T18:01:00Z",
        )
        canonical_after_fill = provider.export_state()

        fill["provider_execution_id"] = "caller-mutated"
        fill["last_quantity"]["value"] = "2"
        fill["evidence"][0]["sha256"] = "sha256:" + ("0" * 64)
        self.assertEqual(provider.export_state(), canonical_after_fill)

        activity = provider.activity_fills()
        activity[0]["last_quantity"]["value"] = "2"
        activity[0]["evidence"].clear()
        self.assertEqual(provider.export_state(), canonical_after_fill)

        query = provider.query_order(
            client_order_id="detached-provider-facts",
            coverage_start="2026-09-30T17:00:00Z",
            coverage_end="2026-09-30T19:00:00Z",
            pagination_complete=True,
            now="2026-09-30T19:00:00Z",
        )
        self.assertEqual(query["order"]["status"], "PARTIALLY_FILLED")
        self.assertEqual(query["order"]["filled_quantity"], "1")
        self.assertEqual(query["order"]["remaining_quantity"], "1")
        self.assertEqual(str(provider.cash), "990")
        self.assertEqual(str(provider.positions["ABC@1"]), "1")

        cancel = provider.cancel_order(
            client_order_id="detached-provider-facts",
            now="2026-09-30T18:02:00Z",
        )
        canonical_after_cancel = provider.export_state()
        canonical_cancel = canonical_after_cancel["cancel_results"]["detached-provider-facts"]

        cancel["outcome"] = "REJECTED"
        cancel["cancelled_at"] = "2099-01-01T00:00:00Z"
        cancel["evidence"][0]["sha256"] = "sha256:" + ("f" * 64)
        self.assertEqual(provider.export_state(), canonical_after_cancel)

        retry = provider.cancel_order(
            client_order_id="detached-provider-facts",
            now="2026-09-30T18:02:00Z",
        )
        self.assertEqual(retry, canonical_cancel)
        retry["evidence"].clear()
        retry["remaining_quantity"] = "0"
        self.assertEqual(provider.export_state(), canonical_after_cancel)


if __name__ == "__main__":
    unittest.main()
