import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import mvp.autotrade_mvp.runtime_load_measurement as measurement_module
from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_measurement import (
    RuntimeLoadMeasurementError,
    evaluate_monotonic_declared_runtime_budget,
    load_declared_financial_latency_samples,
    measure_declared_financial_operation,
)
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan


SHA = "a" * 40
HASH = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)
EVENT_TYPE = "RuntimeQualificationFinancialEvent"


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="monotonic-load",
        release_sha=SHA,
        configuration_hash=HASH,
        host_fingerprint=HOST,
        strategy_horizon_us=1_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=2,
        min_research_samples=1,
    )


def _event(
    event_id: str,
    version: int,
    *,
    aggregate_id: str = "monotonic-load",
) -> ExpectedJournalEvent:
    return ExpectedJournalEvent(
        event_id=event_id,
        event_type=EVENT_TYPE,
        aggregate_type="risk_decision",
        aggregate_id=aggregate_id,
        aggregate_version=version,
    )


def _append(
    store: JournalStore,
    expected: ExpectedJournalEvent,
    *,
    event_type: str | None = None,
) -> None:
    payload = {"scenario": "monotonic-load", "event_id": expected.event_id}
    store.append_event(
        {
            "event_id": expected.event_id,
            "event_type": event_type or expected.event_type,
            "aggregate_type": expected.aggregate_type,
            "aggregate_id": expected.aggregate_id,
            "aggregate_version": str(expected.aggregate_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-03T04:00:00Z",
        }
    )


class RuntimeLoadMeasurementTests(unittest.TestCase):
    def _store(self, root: str) -> JournalStore:
        return JournalStore(Path(root) / "runtime-load-measurement.sqlite")

    def test_measurement_uses_monotonic_clock_and_is_durable(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = _event("financial-1", 1)
            plan = declare_runtime_event_plan(
                store,
                plan_id="latency-1",
                spec=_spec(),
                expected_events=(expected,),
            )

            def operation():
                _append(store, expected)
                return "done"

            with patch(
                "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                side_effect=(1_000_000, 1_001_500),
            ):
                result, sample = measure_declared_financial_operation(
                    store,
                    _spec(),
                    plan_id=plan.plan_id,
                    event_id=expected.event_id,
                    operation=operation,
                )

            self.assertEqual(result, "done")
            self.assertEqual(sample.latency_us, 2)
            self.assertEqual(sample.monotonic_start_ns, 1_000_000)
            self.assertEqual(sample.monotonic_end_ns, 1_001_500)
            self.assertGreater(
                sample.measurement_journal_sequence,
                sample.event_journal_sequence,
            )
            reloaded = load_declared_financial_latency_samples(
                store,
                _spec(),
                plan_id=plan.plan_id,
            )
            self.assertEqual(len(reloaded), 1)
            self.assertEqual(reloaded[0].digest, sample.digest)

    def test_monotonic_samples_feed_existing_budget_without_caller_latency_values(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            first = _event("financial-1", 1)
            second = _event("financial-2", 2)
            plan = declare_runtime_event_plan(
                store,
                plan_id="latency-evaluate",
                spec=_spec(),
                expected_events=(first, second),
            )

            with patch(
                "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                side_effect=(1_000_000, 1_100_000, 2_000_000, 2_200_000),
            ):
                measure_declared_financial_operation(
                    store,
                    _spec(),
                    plan_id=plan.plan_id,
                    event_id=first.event_id,
                    operation=lambda: _append(store, first),
                )
                measure_declared_financial_operation(
                    store,
                    _spec(),
                    plan_id=plan.plan_id,
                    event_id=second.event_id,
                    operation=lambda: _append(store, second),
                )

            decision, evidence, loaded_plan, samples = (
                evaluate_monotonic_declared_runtime_budget(
                    _spec(),
                    store,
                    plan_id=plan.plan_id,
                    financial_staleness_us=(100, 200),
                    research_interference_us=(50,),
                    reconnect_backlog_remaining=0,
                    declared_duration_us=1_000_000,
                    observed_duration_us=1_000_000,
                )
            )
            self.assertEqual(decision.status, "PASS")
            self.assertEqual(tuple(item.latency_us for item in samples), (100, 200))
            self.assertEqual(evidence.missing_event_ids, ())
            self.assertEqual(loaded_plan.digest, plan.digest)

    def test_preexisting_event_cannot_be_replayed_as_measured_latency(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = _event("financial-1", 1)
            plan = declare_runtime_event_plan(
                store,
                plan_id="latency-preexisting",
                spec=_spec(),
                expected_events=(expected,),
            )
            _append(store, expected)
            operation = Mock()

            with self.assertRaisesRegex(
                RuntimeLoadMeasurementError,
                "already exists before monotonic measurement",
            ):
                measure_declared_financial_operation(
                    store,
                    _spec(),
                    plan_id=plan.plan_id,
                    event_id=expected.event_id,
                    operation=operation,
                )
            operation.assert_not_called()

    def test_right_id_with_wrong_binding_cannot_issue_latency_sample(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = _event("financial-1", 1)
            plan = declare_runtime_event_plan(
                store,
                plan_id="latency-wrong-binding",
                spec=_spec(),
                expected_events=(expected,),
            )

            def operation():
                _append(store, expected, event_type="UnrelatedEvent")

            with (
                patch(
                    "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                    side_effect=(10, 20),
                ),
                self.assertRaisesRegex(
                    RuntimeLoadMeasurementError,
                    "does not match its predeclared binding",
                ),
            ):
                measure_declared_financial_operation(
                    store,
                    _spec(),
                    plan_id=plan.plan_id,
                    event_id=expected.event_id,
                    operation=operation,
                )

            event_types = tuple(
                event["event_type"]
                for event in store.load_events_after_journal_sequence(0)
            )
            self.assertNotIn(
                "RuntimeQualificationFinancialLatencyMeasured",
                event_types,
            )

    def test_operation_cannot_replace_post_callback_measurement_authority(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = _event("financial-1", 1)
            plan = declare_runtime_event_plan(
                store,
                plan_id="latency-authority-mutation",
                spec=_spec(),
                expected_events=(expected,),
            )
            original = measurement_module._require_expected_event

            def operation():
                _append(store, expected)
                measurement_module._require_expected_event = (
                    lambda *_args, **_kwargs: {"journal_sequence": 1}
                )

            try:
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                        side_effect=(100, 200),
                    ),
                    self.assertRaisesRegex(
                        RuntimeLoadMeasurementError,
                        "measurement authority changed during financial operation: "
                        "_require_expected_event",
                    ),
                ):
                    measure_declared_financial_operation(
                        store,
                        _spec(),
                        plan_id=plan.plan_id,
                        event_id=expected.event_id,
                        operation=operation,
                    )
            finally:
                measurement_module._require_expected_event = original

            event_types = tuple(
                event["event_type"]
                for event in store.load_events_after_journal_sequence(0)
            )
            self.assertNotIn(
                "RuntimeQualificationFinancialLatencyMeasured",
                event_types,
            )

    def test_operation_cannot_replace_payload_digest_transitive_hash_authority(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = _event("financial-1", 1)
            plan = declare_runtime_event_plan(
                store,
                plan_id="latency-digest-authority-mutation",
                spec=_spec(),
                expected_events=(expected,),
            )
            digest_namespace = measurement_module.payload_digest.__globals__
            original = digest_namespace["sha256"]

            def operation():
                _append(store, expected)
                digest_namespace["sha256"] = lambda *_args, **_kwargs: None

            try:
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                        side_effect=(100, 200),
                    ),
                    self.assertRaisesRegex(
                        RuntimeLoadMeasurementError,
                        "measurement transitive authority changed during financial "
                        "operation: payload_digest.sha256",
                    ),
                ):
                    measure_declared_financial_operation(
                        store,
                        _spec(),
                        plan_id=plan.plan_id,
                        event_id=expected.event_id,
                        operation=operation,
                    )
            finally:
                digest_namespace["sha256"] = original

            event_types = tuple(
                event["event_type"]
                for event in store.load_events_after_journal_sequence(0)
            )
            self.assertNotIn(
                "RuntimeQualificationFinancialLatencyMeasured",
                event_types,
            )

    def test_operation_cannot_replace_plan_digest_binding(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = _event("financial-1", 1)
            plan = declare_runtime_event_plan(
                store,
                plan_id="latency-plan-digest-authority-mutation",
                spec=_spec(),
                expected_events=(expected,),
            )
            namespace = type(plan).digest.fget.__globals__
            original = namespace["payload_digest"]

            def operation():
                _append(store, expected)
                namespace["payload_digest"] = lambda *_args, **_kwargs: HASH

            try:
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                        side_effect=(100, 200),
                    ),
                    self.assertRaisesRegex(
                        RuntimeLoadMeasurementError,
                        "measurement transitive authority changed during financial "
                        "operation: DeclaredRuntimeEventPlan.digest.payload_digest",
                    ),
                ):
                    measure_declared_financial_operation(
                        store,
                        _spec(),
                        plan_id=plan.plan_id,
                        event_id=expected.event_id,
                        operation=operation,
                    )
            finally:
                namespace["payload_digest"] = original

            event_types = tuple(
                event["event_type"]
                for event in store.load_events_after_journal_sequence(0)
            )
            self.assertNotIn(
                "RuntimeQualificationFinancialLatencyMeasured",
                event_types,
            )

    def test_operation_without_durable_financial_event_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = _event("financial-1", 1)
            plan = declare_runtime_event_plan(
                store,
                plan_id="latency-missing-event",
                spec=_spec(),
                expected_events=(expected,),
            )
            with (
                patch(
                    "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                    side_effect=(100, 200),
                ),
                self.assertRaisesRegex(
                    RuntimeLoadMeasurementError,
                    "without the predeclared durable financial event",
                ),
            ):
                measure_declared_financial_operation(
                    store,
                    _spec(),
                    plan_id=plan.plan_id,
                    event_id=expected.event_id,
                    operation=lambda: None,
                )

    def test_backwards_monotonic_interval_never_becomes_latency_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = _event("financial-1", 1)
            plan = declare_runtime_event_plan(
                store,
                plan_id="latency-backwards",
                spec=_spec(),
                expected_events=(expected,),
            )
            with (
                patch(
                    "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                    side_effect=(200, 100),
                ),
                self.assertRaisesRegex(
                    RuntimeLoadMeasurementError,
                    "invalid interval",
                ),
            ):
                measure_declared_financial_operation(
                    store,
                    _spec(),
                    plan_id=plan.plan_id,
                    event_id=expected.event_id,
                    operation=lambda: _append(store, expected),
                )

    def test_loader_requires_measurement_for_every_declared_financial_event(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = _event("financial-1", 1)
            plan = declare_runtime_event_plan(
                store,
                plan_id="latency-unmeasured",
                spec=_spec(),
                expected_events=(expected,),
            )
            _append(store, expected)
            with self.assertRaisesRegex(
                RuntimeLoadMeasurementError,
                "lacks a durable monotonic latency sample",
            ):
                load_declared_financial_latency_samples(
                    store,
                    _spec(),
                    plan_id=plan.plan_id,
                )

    def test_measurement_aggregate_rejects_out_of_plan_order_publication(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            first = _event("financial-1", 1, aggregate_id="financial-a")
            second = _event("financial-2", 1, aggregate_id="financial-b")
            plan = declare_runtime_event_plan(
                store,
                plan_id="latency-order",
                spec=_spec(),
                expected_events=(first, second),
            )
            with (
                patch(
                    "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                    side_effect=(100, 200),
                ),
                self.assertRaises(ValueError),
            ):
                measure_declared_financial_operation(
                    store,
                    _spec(),
                    plan_id=plan.plan_id,
                    event_id=second.event_id,
                    operation=lambda: _append(store, second),
                )


if __name__ == "__main__":
    unittest.main()
