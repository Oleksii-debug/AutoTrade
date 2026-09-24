from decimal import Decimal
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.provider_core import ProviderCoreError
from mvp.autotrade_mvp.simulated_contract_harness import (
    SimulatedProviderContractHarness,
    SubmissionDirective,
)
from mvp.autotrade_mvp.simulated_provider import (
    SimulatedProvider,
    SimulatedProviderConflict,
)


def request(*, attempt_id=None, now="2026-09-24T18:00:00Z", **overrides):
    value = {
        "attempt_id": attempt_id or str(uuid4()),
        "instrument_version": "ABC@1",
        "side": "BUY",
        "quantity": "1",
        "price": "100",
        "now": now,
        "fill_immediately": False,
    }
    value.update(overrides)
    return value


class SimulatedProviderContractHarnessTests(unittest.TestCase):
    def test_ack_reject_and_both_unknown_realities_are_deterministic(self):
        harness = SimulatedProviderContractHarness(
            quota_capacity="20",
            recovery_quota_reserve="2",
        )
        harness.script_submission(
            "reject",
            SubmissionDirective("REJECTED", reason_code="venue_rejected"),
        )
        harness.script_submission(
            "unknown-lost",
            SubmissionDirective("UNKNOWN", persist_unknown=False),
        )
        harness.script_submission(
            "unknown-persisted",
            SubmissionDirective("UNKNOWN", persist_unknown=True),
        )

        guard_calls = []

        def guard():
            guard_calls.append("checked")

        acknowledged = harness.transport_send("ack", request(), guard)
        rejected = harness.transport_send("reject", request(), guard)
        unknown_lost = harness.transport_send("unknown-lost", request(), guard)
        unknown_persisted = harness.transport_send(
            "unknown-persisted",
            request(),
            guard,
        )

        self.assertEqual(acknowledged["outcome"], "ACKNOWLEDGED")
        self.assertEqual(rejected["outcome"], "REJECTED")
        self.assertEqual(unknown_lost["outcome"], "UNKNOWN")
        self.assertEqual(unknown_persisted["outcome"], "UNKNOWN")
        self.assertFalse(rejected["reconciliation_required"])
        self.assertTrue(unknown_lost["reconciliation_required"])
        self.assertTrue(unknown_persisted["reconciliation_required"])
        self.assertEqual(unknown_lost["retry_disposition"], "NEVER")
        self.assertEqual(unknown_persisted["retry_disposition"], "NEVER")
        self.assertNotIn("unknown-lost", harness.provider.orders)
        self.assertIn("unknown-persisted", harness.provider.orders)
        self.assertEqual(harness.provider.outbound_request_count, 4)
        self.assertEqual(len(guard_calls), 4)

    def test_history_lag_keeps_unknown_inconclusive_until_consistency_horizon(self):
        harness = SimulatedProviderContractHarness(
            quota_capacity="5",
            recovery_quota_reserve="1",
        )
        harness.script_submission(
            "lagged",
            SubmissionDirective(
                "UNKNOWN",
                persist_unknown=True,
                history_visible_at="2026-09-24T18:05:00Z",
            ),
        )
        harness.transport_send("lagged", request(), lambda: None)

        early = harness.query_order(
            client_order_id="lagged",
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T18:04:00Z",
            pagination_complete=True,
            now="2026-09-24T18:04:00Z",
        )
        self.assertEqual(early["verdict"], "INCONCLUSIVE")
        self.assertIn("provider_history_lag", early["reason_codes"])

        visible = harness.query_order(
            client_order_id="lagged",
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T18:06:00Z",
            pagination_complete=True,
            now="2026-09-24T18:06:00Z",
        )
        self.assertEqual(visible["verdict"], "FOUND")

    def test_history_lag_rejects_invalid_coverage_and_evidence_is_canonical_utc(self):
        harness = SimulatedProviderContractHarness()
        harness.script_submission(
            "lagged-canonical",
            SubmissionDirective(
                "UNKNOWN",
                persist_unknown=True,
                history_visible_at="2026-09-24T18:05:00Z",
            ),
        )
        harness.transport_send(
            "lagged-canonical",
            request(now="2026-09-24T20:00:00+02:00"),
            lambda: None,
        )

        early = harness.query_order(
            client_order_id="lagged-canonical",
            coverage_start="2026-09-24T19:00:00+02:00",
            coverage_end="2026-09-24T20:04:00+02:00",
            pagination_complete=True,
            now="2026-09-24T20:04:00+02:00",
        )
        self.assertEqual(early["verdict"], "INCONCLUSIVE")
        self.assertEqual(
            early["evidence"][0]["observed_at"],
            "2026-09-24T18:04:00Z",
        )

        with self.assertRaisesRegex(ValueError, "must not precede"):
            harness.query_order(
                client_order_id="lagged-canonical",
                coverage_start="2026-09-24T18:10:00Z",
                coverage_end="2026-09-24T18:00:00Z",
                pagination_complete=True,
                now="2026-09-24T18:04:00Z",
            )
        with self.assertRaisesRegex(TypeError, "pagination_complete"):
            harness.query_order(
                client_order_id="lagged-canonical",
                coverage_start="2026-09-24T17:00:00Z",
                coverage_end="2026-09-24T18:00:00Z",
                pagination_complete=1,
                now="2026-09-24T18:04:00Z",
            )

    def test_quota_is_reserved_for_recovery_and_blocks_before_send(self):
        provider = SimulatedProvider()
        harness = SimulatedProviderContractHarness(
            provider,
            quota_capacity="2",
            recovery_quota_reserve="1",
        )
        harness.transport_send(
            "first",
            request(),
            lambda: None,
            purpose="TRADING",
        )
        self.assertEqual(provider.outbound_request_count, 1)
        with self.assertRaisesRegex(ProviderCoreError, "reserve"):
            harness.transport_send(
                "second",
                request(),
                lambda: None,
                purpose="RESEARCH",
            )
        self.assertEqual(provider.outbound_request_count, 1)

        harness.transport_send(
            "recovery",
            request(),
            lambda: None,
            purpose="RECOVERY",
        )
        self.assertEqual(provider.outbound_request_count, 2)

    def test_final_guard_rejection_does_not_consume_unsent_provider_quota(self):
        provider = SimulatedProvider()
        harness = SimulatedProviderContractHarness(
            provider,
            quota_capacity="2",
            recovery_quota_reserve="1",
        )

        def blocked():
            raise PermissionError("authority revoked")

        with self.assertRaises(PermissionError):
            harness.transport_send("blocked", request(), blocked, purpose="TRADING")
        self.assertEqual(provider.outbound_request_count, 0)
        self.assertEqual(harness.quota.used, Decimal("0"))

        harness.transport_send("allowed", request(), lambda: None, purpose="TRADING")
        self.assertEqual(provider.outbound_request_count, 1)
        self.assertEqual(harness.quota.used, Decimal("1"))

    def test_clock_skew_blocks_before_final_guard_and_transport(self):
        provider = SimulatedProvider()
        harness = SimulatedProviderContractHarness(
            provider,
            quota_capacity="5",
            recovery_quota_reserve="1",
            maximum_clock_skew_seconds=2,
        )
        guard_calls = []
        with self.assertRaisesRegex(ProviderCoreError, "clock skew"):
            harness.transport_send(
                "clock",
                request(provider_now="2026-09-24T18:00:03Z"),
                lambda: guard_calls.append("called"),
            )
        self.assertEqual(guard_calls, [])
        self.assertEqual(provider.outbound_request_count, 0)

    def test_stream_event_cannot_be_mutated_after_observation(self):
        harness = SimulatedProviderContractHarness()
        source = {"kind": "book", "levels": [{"price": "100", "size": "2"}]}
        event = harness.emit_stream_event(
            sequence=1,
            observed_at="2026-09-24T20:00:00+02:00",
            payload=source,
        )
        source["kind"] = "tampered"
        source["levels"][0]["price"] = "1"
        self.assertEqual(event.observed_at, "2026-09-24T18:00:00Z")
        self.assertEqual(event.payload["kind"], "book")
        self.assertEqual(event.payload["levels"][0]["price"], "100")
        with self.assertRaises(TypeError):
            event.payload["kind"] = "tampered"
        with self.assertRaises(TypeError):
            event.payload["levels"][0]["price"] = "1"

    def test_stream_payload_rejects_binary_float_and_unknown_mutable_objects(self):
        harness = SimulatedProviderContractHarness()
        with self.assertRaisesRegex(TypeError, "binary float"):
            harness.emit_stream_event(
                sequence=1,
                observed_at="2026-09-24T18:00:00Z",
                payload={"price": 100.1},
            )
        with self.assertRaisesRegex(TypeError, "immutable JSON scalars"):
            harness.emit_stream_event(
                sequence=1,
                observed_at="2026-09-24T18:00:00Z",
                payload={"levels": {"mutable", "set"}},
            )

    def test_stream_payload_preserves_exact_decimal_as_immutable_value(self):
        harness = SimulatedProviderContractHarness()
        event = harness.emit_stream_event(
            sequence=1,
            observed_at="2026-09-24T18:00:00Z",
            payload={
                "price": Decimal("100.10"),
                "nested": [{"size": Decimal("2.5")}],
                "trading": False,
            },
        )
        self.assertEqual(event.payload["price"], Decimal("100.10"))
        self.assertEqual(event.payload["nested"][0]["size"], Decimal("2.5"))
        self.assertIs(event.payload["trading"], False)

    def test_stream_gap_is_explicit_even_when_first_event_is_contiguous(self):
        harness = SimulatedProviderContractHarness()
        harness.emit_stream_event(
            sequence=1,
            observed_at="2026-09-24T18:00:00Z",
            payload={"kind": "quote"},
        )
        harness.emit_stream_event(
            sequence=3,
            observed_at="2026-09-24T18:00:02Z",
            payload={"kind": "trade"},
        )
        replay = harness.stream_after(0)
        self.assertTrue(replay["gap_detected"])
        self.assertEqual(replay["gap_at_sequence"], 2)
        self.assertEqual([event.sequence for event in replay["events"]], [1, 3])

    def test_fee_correction_is_append_only_idempotent_and_exact(self):
        provider = SimulatedProvider(initial_cash="1000", fee_rate="0.001")
        harness = SimulatedProviderContractHarness(
            provider,
            quota_capacity="5",
            recovery_quota_reserve="1",
        )
        harness.transport_send(
            "filled",
            request(fill_immediately=True),
            lambda: None,
        )
        fill_before = dict(provider.activity_fills()[0])
        cash_before = provider.cash
        execution_id = fill_before["provider_execution_id"]

        first = harness.record_fee_correction(
            correction_id="fee-1",
            provider_execution_id=execution_id,
            fee_delta="0.25",
            now="2026-09-24T18:01:00Z",
        )
        second = harness.record_fee_correction(
            correction_id="fee-1",
            provider_execution_id=execution_id,
            fee_delta=Decimal("0.25"),
            now="2026-09-24T18:01:00Z",
        )
        self.assertEqual(first, second)
        self.assertEqual(provider.cash, cash_before - Decimal("0.25"))
        self.assertEqual(dict(provider.activity_fills()[0]), fill_before)
        self.assertEqual(len(harness.activity_corrections()), 1)

        with self.assertRaises(SimulatedProviderConflict):
            harness.record_fee_correction(
                correction_id="fee-1",
                provider_execution_id=execution_id,
                fee_delta="0.30",
                now="2026-09-24T18:01:00Z",
            )
        with self.assertRaises(TypeError):
            harness.record_fee_correction(
                correction_id="fee-float",
                provider_execution_id=execution_id,
                fee_delta=0.1,
                now="2026-09-24T18:02:00Z",
            )

    def test_outage_health_is_degraded_without_claiming_orders_cancelled(self):
        harness = SimulatedProviderContractHarness()
        health = harness.health(
            now="2026-09-24T18:00:00Z",
            outage=True,
        )
        self.assertEqual(health["status"], "DEGRADED")
        self.assertIn("scripted_provider_outage", health["reason_codes"])
        self.assertEqual(health["next_action"], "reconcile_before_new_risk")
        self.assertNotIn("cancel", repr(health).lower())

    def test_script_identity_is_immutable(self):
        harness = SimulatedProviderContractHarness()
        harness.script_submission("same", SubmissionDirective("REJECTED"))
        harness.script_submission("same", SubmissionDirective("REJECTED"))
        with self.assertRaises(SimulatedProviderConflict):
            harness.script_submission("same", SubmissionDirective("UNKNOWN"))


if __name__ == "__main__":
    unittest.main()
