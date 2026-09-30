from hashlib import sha256
import json
from decimal import (
    Decimal,
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    localcontext,
)
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

    def test_submission_times_and_history_horizon_are_canonical_utc(self):
        harness = SimulatedProviderContractHarness()
        directive = SubmissionDirective(
            "UNKNOWN",
            persist_unknown=False,
            history_visible_at="2026-09-24T20:05:00+02:00",
        )
        self.assertEqual(directive.history_visible_at, "2026-09-24T18:05:00Z")
        harness.script_submission("canonical-time", directive)
        result = harness.transport_send(
            "canonical-time",
            request(now="2026-09-24T20:00:00+02:00"),
            lambda: None,
        )
        self.assertEqual(result["provider_received_at"], "2026-09-24T18:00:00Z")
        self.assertEqual(
            result["evidence"][0]["observed_at"],
            "2026-09-24T18:00:00Z",
        )

    def test_fill_immediately_requires_strict_boolean_before_send(self):
        provider = SimulatedProvider()
        harness = SimulatedProviderContractHarness(provider)
        guard_calls = []
        with self.assertRaisesRegex(TypeError, "fill_immediately"):
            harness.transport_send(
                "bad-bool",
                request(fill_immediately=1),
                lambda: guard_calls.append("called"),
            )
        self.assertEqual(guard_calls, [])
        self.assertEqual(provider.outbound_request_count, 0)
        self.assertEqual(harness.quota.used, Decimal("0"))

    def test_invalid_order_fields_fail_before_guard_quota_and_outbound_boundary(self):
        invalid_requests = (
            request(attempt_id="not-a-uuid"),
            request(side="HOLD"),
            request(quantity="0"),
            request(price="-1"),
            request(instrument_version=" "),
        )
        for index, candidate in enumerate(invalid_requests):
            with self.subTest(index=index):
                provider = SimulatedProvider()
                harness = SimulatedProviderContractHarness(
                    provider,
                    quota_capacity="5",
                    recovery_quota_reserve="1",
                )
                guard_calls = []
                with self.assertRaises((TypeError, ValueError)):
                    harness.transport_send(
                        f"invalid-{index}",
                        candidate,
                        lambda: guard_calls.append("called"),
                    )
                self.assertEqual(guard_calls, [])
                self.assertEqual(provider.outbound_request_count, 0)
                self.assertEqual(harness.quota.used, Decimal("0"))

    def test_missing_required_order_field_fails_before_send_boundary(self):
        provider = SimulatedProvider()
        harness = SimulatedProviderContractHarness(provider)
        candidate = request()
        del candidate["quantity"]
        guard_calls = []
        with self.assertRaises((TypeError, ValueError)):
            harness.transport_send(
                "missing-quantity",
                candidate,
                lambda: guard_calls.append("called"),
            )
        self.assertEqual(guard_calls, [])
        self.assertEqual(provider.outbound_request_count, 0)
        self.assertEqual(harness.quota.used, Decimal("0"))


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

    def test_zero_cost_request_cannot_bypass_quota_purpose_validation(self):
        provider = SimulatedProvider()
        harness = SimulatedProviderContractHarness(provider)
        guard_calls = []

        with self.assertRaisesRegex(ProviderCoreError, "unknown quota purpose"):
            harness.transport_send(
                "invalid-purpose",
                request(),
                lambda: guard_calls.append("called"),
                purpose="MAINTENANCE",
                quota_cost="0",
            )

        self.assertEqual(guard_calls, [])
        self.assertEqual(provider.outbound_request_count, 0)
        self.assertEqual(harness.quota.used, Decimal("0"))

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
            now="2026-09-24T20:01:00+02:00",
        )
        self.assertEqual(first, second)
        self.assertEqual(first["observed_at"], "2026-09-24T18:01:00Z")
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


    def test_harness_restart_preserves_unknown_lag_quota_stream_and_correction(self):
        provider = SimulatedProvider(initial_cash="1000", fee_rate="0.001")
        harness = SimulatedProviderContractHarness(
            provider,
            quota_capacity="8",
            recovery_quota_reserve="2",
            maximum_clock_skew_seconds=3,
        )
        harness.script_submission(
            "restart-unknown",
            SubmissionDirective(
                "UNKNOWN",
                persist_unknown=True,
                reason_code="response_lost",
                history_visible_at="2026-09-28T18:05:00Z",
            ),
        )
        harness.transport_send(
            "restart-unknown",
            request(
                now="2026-09-28T18:00:00Z",
                fill_immediately=True,
            ),
            lambda: None,
        )
        execution_id = provider.activity_fills()[0]["provider_execution_id"]
        correction = harness.record_fee_correction(
            correction_id="restart-fee",
            provider_execution_id=execution_id,
            fee_delta="0.25",
            now="2026-09-28T18:01:00Z",
        )
        harness.emit_stream_event(
            sequence=1,
            observed_at="2026-09-28T18:02:00Z",
            payload={
                "price": Decimal("100.10"),
                "nested": [{"size": Decimal("2.5")}],
            },
        )
        state = harness.export_state()
        restored = SimulatedProviderContractHarness.from_state(state)

        self.assertEqual(restored.export_state(), state)
        self.assertEqual(restored.quota.used, Decimal("1"))
        self.assertEqual(restored.quota.capacity, Decimal("8"))
        self.assertEqual(restored.quota.recovery_reserve, Decimal("2"))
        self.assertEqual(restored.provider.cash, provider.cash)
        self.assertEqual(
            dict(restored.activity_corrections()[0]),
            dict(correction),
        )
        stream = restored.stream_after(0)
        self.assertEqual([event.sequence for event in stream["events"]], [1])
        self.assertEqual(
            stream["events"][0].payload["price"],
            Decimal("100.10"),
        )
        self.assertEqual(
            stream["events"][0].payload["nested"][0]["size"],
            Decimal("2.5"),
        )

        early = restored.query_order(
            client_order_id="restart-unknown",
            coverage_start="2026-09-28T17:00:00Z",
            coverage_end="2026-09-28T18:04:00Z",
            pagination_complete=True,
            now="2026-09-28T18:04:00Z",
        )
        self.assertEqual(early["verdict"], "INCONCLUSIVE")
        self.assertIn("provider_history_lag", early["reason_codes"])
        with self.assertRaisesRegex(
            SimulatedProviderConflict,
            "blind retry is forbidden",
        ):
            restored.transport_send(
                "restart-unknown",
                request(
                    now="2026-09-28T18:06:00Z",
                    fill_immediately=True,
                ),
                lambda: None,
            )

    def test_harness_restart_rejects_recomputed_correction_evidence_tamper(self):
        provider = SimulatedProvider(initial_cash="1000")
        harness = SimulatedProviderContractHarness(provider)
        harness.transport_send(
            "correction-tamper",
            request(fill_immediately=True),
            lambda: None,
        )
        harness.record_fee_correction(
            correction_id="fee-tamper",
            provider_execution_id=provider.activity_fills()[0]["provider_execution_id"],
            fee_delta="0.25",
            now="2026-09-28T18:01:00Z",
        )
        state = harness.export_state()
        state["corrections"][0]["fee_delta"] = "0.50"
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
            "correction.*canonical evidence",
        ):
            SimulatedProviderContractHarness.from_state(state)


    def test_submission_directive_rejects_truthy_persist_unknown_alias(self):
        with self.assertRaisesRegex(TypeError, "persist_unknown must be boolean"):
            SubmissionDirective("UNKNOWN", persist_unknown=1)


    def test_harness_restart_rejects_rehashed_submission_time_rewrite(self):
        harness = SimulatedProviderContractHarness()
        harness.script_submission(
            "time-tamper",
            SubmissionDirective("UNKNOWN", persist_unknown=True),
        )
        harness.transport_send(
            "time-tamper",
            request(now="2026-09-28T18:00:00Z"),
            lambda: None,
        )
        state = harness.export_state()
        state["submission_started_at"]["time-tamper"] = "2026-09-28T17:00:00Z"
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
            "submission-start time",
        ):
            SimulatedProviderContractHarness.from_state(state)


    def test_harness_restart_rejects_rehashed_unknown_recursive_stream_fields(self):
        harness = SimulatedProviderContractHarness()
        harness.emit_stream_event(
            sequence=1,
            observed_at="2026-09-28T18:00:00Z",
            payload={
                "price": Decimal("100.10"),
                "nested": [{"size": Decimal("2.5"), "live": True}],
            },
        )
        pristine = harness.export_state()

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

        def find_kind(value, kind):
            if isinstance(value, dict):
                if value.get("kind") == kind:
                    return value
                for nested in value.values():
                    found = find_kind(nested, kind)
                    if found is not None:
                        return found
            elif isinstance(value, list):
                for nested in value:
                    found = find_kind(nested, kind)
                    if found is not None:
                        return found
            return None

        for kind in ("mapping", "sequence", "decimal", "scalar"):
            with self.subTest(kind=kind):
                state = json.loads(json.dumps(pristine))
                payload = state["stream_events"][0]["payload"]
                node = find_kind(payload, kind)
                self.assertIsNotNone(node)
                node["future"] = True
                reseal(state)
                with self.assertRaisesRegex(
                    ValueError,
                    rf"encoded {kind} state has unsupported or missing fields",
                ):
                    SimulatedProviderContractHarness.from_state(state)


    def test_harness_restart_rejects_rehashed_unknown_schema_field(self):
        harness = SimulatedProviderContractHarness()
        state = harness.export_state()
        state["future_retry_authority"] = True
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
            SimulatedProviderContractHarness.from_state(state)


    def test_fee_correction_restart_preserves_application_chronology_at_envelope(self):
        maximum = "9" * 256
        provider = SimulatedProvider(
            initial_cash=("9" * 255) + "8",
            fee_rate="0",
        )
        provider.submit_order(
            attempt_id="00000000-0000-0000-0000-000000000013",
            client_order_id="correction-order-boundary",
            instrument_version="ABC@1",
            side="SELL",
            quantity="1",
            price="1",
            now="2026-09-30T18:00:00Z",
        )
        self.assertEqual(str(provider.cash), maximum)
        execution_id = provider.activity_fills()[0]["provider_execution_id"]
        harness = SimulatedProviderContractHarness(provider)
        harness.record_fee_correction(
            correction_id="z-debit",
            provider_execution_id=execution_id,
            fee_delta="1",
            now="2026-09-30T18:01:00Z",
        )
        harness.record_fee_correction(
            correction_id="a-rebate",
            provider_execution_id=execution_id,
            fee_delta="-1",
            now="2026-09-30T18:02:00Z",
        )
        self.assertEqual(str(harness.provider.cash), maximum)

        state = harness.export_state()
        self.assertEqual(
            [item["correction_id"] for item in state["corrections"]],
            ["z-debit", "a-rebate"],
        )
        restored = SimulatedProviderContractHarness.from_state(state)
        self.assertEqual(restored.export_state(), state)
        self.assertEqual(str(restored.provider.cash), maximum)

    def test_fee_correction_export_reverse_unwind_avoids_delta_sum_overflow(self):
        maximum = "9" * 256
        provider = SimulatedProvider(initial_cash="0", fee_rate="0")
        provider.submit_order(
            attempt_id="00000000-0000-0000-0000-000000000014",
            client_order_id="correction-aggregate-boundary",
            instrument_version="ABC@1",
            side="SELL",
            quantity="1",
            price=maximum,
            now="2026-09-30T18:00:00Z",
        )
        self.assertEqual(str(provider.cash), maximum)
        execution_id = provider.activity_fills()[0]["provider_execution_id"]
        harness = SimulatedProviderContractHarness(provider)

        # Both live transitions are admissible: M -> 0 -> -M. The aggregate
        # correction delta 2M has 257 integer digits and is not itself an
        # admissible exact-decimal value, so export must reverse the actual
        # chronology rather than construct that aggregate.
        harness.record_fee_correction(
            correction_id="first-max-charge",
            provider_execution_id=execution_id,
            fee_delta=maximum,
            now="2026-09-30T18:01:00Z",
        )
        harness.record_fee_correction(
            correction_id="second-max-charge",
            provider_execution_id=execution_id,
            fee_delta=maximum,
            now="2026-09-30T18:02:00Z",
        )
        self.assertEqual(str(harness.provider.cash), "-" + maximum)

        state = harness.export_state()
        self.assertEqual(state["provider_base_state"]["cash"], maximum)
        self.assertEqual(
            [item["correction_id"] for item in state["corrections"]],
            ["first-max-charge", "second-max-charge"],
        )
        restored = SimulatedProviderContractHarness.from_state(state)
        self.assertEqual(restored.export_state(), state)
        self.assertEqual(str(restored.provider.cash), "-" + maximum)

    def test_fee_correction_restart_is_decimal_context_invariant(self):
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
                        attempt_id="00000000-0000-0000-0000-000000000011",
                        client_order_id="correction-context",
                        instrument_version="ABC@1",
                        side="BUY",
                        quantity="1",
                        price="103",
                        now="2026-09-30T18:00:00Z",
                    )
                    execution_id = provider.activity_fills()[0][
                        "provider_execution_id"
                    ]
                    harness = SimulatedProviderContractHarness(provider)
                    harness.record_fee_correction(
                        correction_id="context-correction-1",
                        provider_execution_id=execution_id,
                        fee_delta="0.0000000001",
                        now="2026-09-30T18:01:00Z",
                    )
                    harness.record_fee_correction(
                        correction_id="context-correction-2",
                        provider_execution_id=execution_id,
                        fee_delta="-0.00000000009",
                        now="2026-09-30T18:02:00Z",
                    )
                    state = harness.export_state()
                    restored = SimulatedProviderContractHarness.from_state(state)
                    restored_state = restored.export_state()
                    snapshot = restored.provider.account_snapshot(
                        now="2026-09-30T18:03:00Z"
                    )
                self.assertEqual(restored_state, state)
                self.assertEqual(
                    snapshot["balances"][0]["total"],
                    "896.89699999999",
                )
                self.assertEqual(
                    state["provider_base_state"]["cash"],
                    "896.897",
                )
                observed.append(
                    (
                        state,
                        snapshot,
                        restored.activity_corrections(),
                    )
                )
        self.assertTrue(all(item == observed[0] for item in observed[1:]))

    def test_fee_correction_output_overflow_leaves_harness_state_unchanged(self):
        maximum = "9" * 256
        provider = SimulatedProvider(initial_cash=maximum, fee_rate="0")
        provider.submit_order(
            attempt_id="00000000-0000-0000-0000-000000000012",
            client_order_id="correction-overflow",
            instrument_version="ABC@1",
            side="BUY",
            quantity="1",
            price="1",
            now="2026-09-30T18:00:00Z",
        )
        execution_id = provider.activity_fills()[0]["provider_execution_id"]
        harness = SimulatedProviderContractHarness(provider)
        before = harness.export_state()
        with self.assertRaises(ValueError):
            harness.record_fee_correction(
                correction_id="overflow-rebate",
                provider_execution_id=execution_id,
                fee_delta="-2",
                now="2026-09-30T18:01:00Z",
            )
        self.assertEqual(harness.export_state(), before)


    def test_harness_decimal_subclass_is_rejected_before_virtual_methods(self):
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
            "quota_capacity must be a finite decimal",
        ):
            SimulatedProviderContractHarness(
                quota_capacity=HostileDecimal("100"),
            )
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
