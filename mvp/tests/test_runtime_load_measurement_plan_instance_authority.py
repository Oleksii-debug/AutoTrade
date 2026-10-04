import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
EVENT_TYPE = "RuntimeQualificationFinancialEvent"


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="plan-instance-authority",
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


def _event(event_id: str, version: int) -> ExpectedJournalEvent:
    return ExpectedJournalEvent(
        event_id=event_id,
        event_type=EVENT_TYPE,
        aggregate_type="risk_decision",
        aggregate_id="plan-instance-authority",
        aggregate_version=version,
    )


def _append(store: JournalStore, expected: ExpectedJournalEvent) -> None:
    payload = {"event_id": expected.event_id, "scenario": "plan-instance-authority"}
    store.append_event(
        {
            "event_id": expected.event_id,
            "event_type": expected.event_type,
            "aggregate_type": expected.aggregate_type,
            "aggregate_id": expected.aggregate_id,
            "aggregate_version": str(expected.aggregate_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-04T06:30:00Z",
        }
    )


def _measurement_frame():
    frame = inspect.currentframe()
    if frame is None or frame.f_back is None or frame.f_back.f_back is None:
        raise AssertionError("measurement frame is unavailable")
    return frame.f_back.f_back


class RuntimeLoadMeasurementPlanInstanceAuthorityTests(unittest.TestCase):
    def _store(self, root: str) -> JournalStore:
        return JournalStore(Path(root) / "runtime-load-plan-instance-authority.sqlite")

    def _assert_no_latency_measurement(self, store: JournalStore) -> None:
        event_types = tuple(
            event["event_type"]
            for event in store.load_events_after_journal_sequence(0)
        )
        self.assertNotIn("RuntimeQualificationFinancialLatencyMeasured", event_types)

    def test_callback_cannot_mutate_selected_expected_event_through_caller_frame(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = _event("financial-selected", 1)
            plan = declare_runtime_event_plan(
                store,
                plan_id="selected-event-mutation",
                spec=_spec(),
                expected_events=(expected,),
            )

            def operation():
                loaded_expected = _measurement_frame().f_locals["expected"]
                object.__setattr__(
                    loaded_expected,
                    "event_type",
                    "CallbackForgedFinancialEvent",
                )
                _append(store, loaded_expected)

            with (
                patch(
                    "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                    side_effect=(100, 200),
                ),
                self.assertRaisesRegex(
                    RuntimeLoadMeasurementError,
                    "durable plan event instance state changed during financial operation: event_type",
                ),
            ):
                measure_declared_financial_operation(
                    store,
                    _spec(),
                    plan_id=plan.plan_id,
                    event_id=expected.event_id,
                    operation=operation,
                )

            self._assert_no_latency_measurement(store)

    def test_callback_cannot_mutate_unselected_plan_event_used_by_plan_digest(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            first = _event("financial-first", 1)
            second = _event("financial-second", 2)
            plan = declare_runtime_event_plan(
                store,
                plan_id="unselected-event-mutation",
                spec=_spec(),
                expected_events=(first, second),
            )

            def operation():
                loaded_plan = _measurement_frame().f_locals["plan"]
                object.__setattr__(
                    loaded_plan.expected_events[1],
                    "aggregate_id",
                    "callback-forged-aggregate",
                )
                _append(store, first)

            with (
                patch(
                    "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                    side_effect=(300, 400),
                ),
                self.assertRaisesRegex(
                    RuntimeLoadMeasurementError,
                    "durable plan event instance state changed during financial operation: aggregate_id",
                ),
            ):
                measure_declared_financial_operation(
                    store,
                    _spec(),
                    plan_id=plan.plan_id,
                    event_id=first.event_id,
                    operation=operation,
                )

            self._assert_no_latency_measurement(store)

    def test_callback_cannot_mutate_loaded_plan_scalar_through_caller_frame(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = _event("financial-plan", 1)
            plan = declare_runtime_event_plan(
                store,
                plan_id="plan-scalar-mutation",
                spec=_spec(),
                expected_events=(expected,),
            )

            def operation():
                loaded_plan = _measurement_frame().f_locals["plan"]
                object.__setattr__(
                    loaded_plan,
                    "spec_digest",
                    "sha256:" + ("d" * 64),
                )
                _append(store, expected)

            with (
                patch(
                    "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                    side_effect=(500, 600),
                ),
                self.assertRaisesRegex(
                    RuntimeLoadMeasurementError,
                    "durable plan instance state changed during financial operation: spec_digest",
                ),
            ):
                measure_declared_financial_operation(
                    store,
                    _spec(),
                    plan_id=plan.plan_id,
                    event_id=expected.event_id,
                    operation=operation,
                )

            self._assert_no_latency_measurement(store)

    def test_callback_cannot_swap_loaded_plan_to_compatible_subclass(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = _event("financial-plan-class", 1)
            plan = declare_runtime_event_plan(
                store,
                plan_id="plan-class-mutation",
                spec=_spec(),
                expected_events=(expected,),
            )

            def operation():
                loaded_plan = _measurement_frame().f_locals["plan"]
                forged_plan_type = type(
                    "ForgedDeclaredRuntimeEventPlan",
                    (type(loaded_plan),),
                    {"__slots__": ()},
                )
                object.__setattr__(loaded_plan, "__class__", forged_plan_type)
                _append(store, expected)

            with (
                patch(
                    "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                    side_effect=(700, 800),
                ),
                self.assertRaisesRegex(
                    RuntimeLoadMeasurementError,
                    "durable plan exact class changed during financial operation",
                ),
            ):
                measure_declared_financial_operation(
                    store,
                    _spec(),
                    plan_id=plan.plan_id,
                    event_id=expected.event_id,
                    operation=operation,
                )

            self._assert_no_latency_measurement(store)


if __name__ == "__main__":
    unittest.main()
