import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_measurement import (
    RuntimeLoadMeasurementError,
    measure_declared_financial_operation,
)
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan

SHA = "a" * 40
HASH = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="monotonic-race",
        release_sha=SHA,
        configuration_hash=HASH,
        host_fingerprint=HOST,
        strategy_horizon_us=1_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=1,
        min_research_samples=1,
    )


def _expected() -> ExpectedJournalEvent:
    return ExpectedJournalEvent(
        event_id="financial-raced",
        event_type="RuntimeQualificationFinancialEvent",
        aggregate_type="risk_decision",
        aggregate_id="monotonic-race",
        aggregate_version=1,
    )


def _append(store: JournalStore, expected: ExpectedJournalEvent) -> None:
    payload = {"scenario": "monotonic-race", "event_id": expected.event_id}
    store.append_event(
        {
            "event_id": expected.event_id,
            "event_type": expected.event_type,
            "aggregate_type": expected.aggregate_type,
            "aggregate_id": expected.aggregate_id,
            "aggregate_version": str(expected.aggregate_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-03T04:36:00Z",
        }
    )


def _rebind_store(target: JournalStore, source: JournalStore) -> None:
    target.path = source.path
    target._store_identity = source.store_identity


class RuntimeLoadMeasurementStartRaceTests(unittest.TestCase):
    def test_event_committed_only_while_sampling_end_clock_is_not_attributed_to_operation(self):
        with tempfile.TemporaryDirectory() as root:
            store = JournalStore(Path(root) / "runtime-load-measurement.sqlite")
            expected = _expected()
            plan = declare_runtime_event_plan(
                store,
                plan_id="latency-end-race",
                spec=_spec(),
                expected_events=(expected,),
            )
            operation = Mock(return_value="done")
            clock_calls = 0

            def racing_clock() -> int:
                nonlocal clock_calls
                clock_calls += 1
                if clock_calls == 1:
                    return 1_000_000
                _append(store, expected)
                return 1_000_100

            with (
                patch(
                    "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                    side_effect=racing_clock,
                ),
                self.assertRaisesRegex(
                    RuntimeLoadMeasurementError,
                    "canonical operation returned without the predeclared durable financial event",
                ),
            ):
                measure_declared_financial_operation(
                    store,
                    _spec(),
                    plan_id=plan.plan_id,
                    event_id=expected.event_id,
                    operation=operation,
                )

            operation.assert_called_once_with()
            self.assertEqual(clock_calls, 1)
            self.assertIsNone(JournalStore.get_event(store, expected.event_id))

    def test_event_committed_before_start_clock_returns_cannot_be_measured_as_operation_latency(self):
        with tempfile.TemporaryDirectory() as root:
            store = JournalStore(Path(root) / "runtime-load-measurement.sqlite")
            expected = _expected()
            plan = declare_runtime_event_plan(
                store,
                plan_id="latency-start-race",
                spec=_spec(),
                expected_events=(expected,),
            )
            operation = Mock(return_value="done")
            clock_calls = 0

            def racing_clock() -> int:
                nonlocal clock_calls
                clock_calls += 1
                if clock_calls == 1:
                    _append(store, expected)
                    return 1_000_000
                return 1_000_100

            with patch(
                "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                side_effect=racing_clock,
            ):
                try:
                    measure_declared_financial_operation(
                        store,
                        _spec(),
                        plan_id=plan.plan_id,
                        event_id=expected.event_id,
                        operation=operation,
                    )
                except RuntimeLoadMeasurementError:
                    return

            self.fail(
                "a financial event committed before monotonic_start_ns was "
                "accepted as durable latency evidence for the measured operation"
            )

    def test_operation_cannot_rebind_measurement_to_another_journal_generation(self):
        with tempfile.TemporaryDirectory() as root:
            store = JournalStore(Path(root) / "runtime-load-measurement-a.sqlite")
            other = JournalStore(Path(root) / "runtime-load-measurement-b.sqlite")
            expected = _expected()
            plan = declare_runtime_event_plan(
                store,
                plan_id="latency-store-generation-race",
                spec=_spec(),
                expected_events=(expected,),
            )
            seed = ExpectedJournalEvent(
                event_id="other-store-seed",
                event_type="RuntimeQualificationFinancialEvent",
                aggregate_type="risk_decision",
                aggregate_id="other-store-seed",
                aggregate_version=1,
            )
            _append(other, seed)

            def operation() -> str:
                _rebind_store(store, other)
                _append(store, expected)
                return "done"

            with (
                patch(
                    "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                    side_effect=(1_000_000, 1_000_100),
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "journal operation authority changed before connection",
                ),
            ):
                measure_declared_financial_operation(
                    store,
                    _spec(),
                    plan_id=plan.plan_id,
                    event_id=expected.event_id,
                    operation=operation,
                )

            self.assertIsNone(JournalStore.get_event(other, expected.event_id))


if __name__ == "__main__":
    unittest.main()
