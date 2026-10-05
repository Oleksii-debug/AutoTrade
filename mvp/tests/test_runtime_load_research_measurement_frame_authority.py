from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan
from mvp.autotrade_mvp.runtime_load_research_measurement import (
    ExpectedResearchInterferenceSample,
    RuntimeLoadResearchMeasurementError,
    declare_research_interference_plan,
    measure_declared_research_interference,
)


SHA = "a" * 40
HASH = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="research-frame-authority",
        release_sha=SHA,
        configuration_hash=HASH,
        host_fingerprint=HOST,
        strategy_horizon_us=5_000,
        max_p95_financial_latency_us=5_000,
        max_financial_staleness_us=5_000,
        max_research_interference_us=5_000,
        min_financial_samples=1,
        min_research_samples=1,
    )


def _measurement_frame():
    frame = inspect.currentframe()
    if frame is None or frame.f_back is None or frame.f_back.f_back is None:
        raise AssertionError("research measurement frame is unavailable")
    return frame.f_back.f_back


class RuntimeLoadResearchFrameAuthorityTests(unittest.TestCase):
    def _plans(self, root: str):
        store = JournalStore(Path(root) / "research-frame-authority.sqlite")
        financial = declare_runtime_event_plan(
            store,
            plan_id="financial-frame-plan",
            spec=_spec(),
            expected_events=(
                ExpectedJournalEvent(
                    event_id="financial-frame-event",
                    event_type="RuntimeQualificationFinancialEvent",
                    aggregate_type="risk_decision",
                    aggregate_id="research-frame-authority",
                    aggregate_version=1,
                ),
            ),
        )
        research = declare_research_interference_plan(
            store,
            _spec(),
            plan_id="research-frame-plan",
            financial_plan_id=financial.plan_id,
            expected_samples=(
                ExpectedResearchInterferenceSample(
                    sample_id="research-frame-sample",
                    phase="cpu-pressure",
                ),
            ),
        )
        return store, research

    def _assert_no_research_measurement(self, store: JournalStore) -> None:
        event_types = tuple(
            event["event_type"]
            for event in store.load_events_after_journal_sequence(0)
        )
        self.assertNotIn(
            "RuntimeQualificationResearchInterferenceMeasured",
            event_types,
        )

    def test_python313_frame_locals_cannot_hide_research_journal_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            store, research = self._plans(root)

            def operation() -> None:
                measurement_frame = _measurement_frame()
                pre_sequence = measurement_frame.f_locals["pre_sequence"]
                measurement_frame.f_locals["require_post_callback_authority"] = (
                    lambda: None
                )
                measurement_frame.f_locals["current_sequence"] = (
                    lambda _store: pre_sequence
                )
                payload = {"kind": "frame-local-hidden-side-effect"}
                store.append_event(
                    {
                        "event_id": "frame-local-hidden-side-effect",
                        "event_type": "HostControlObserved",
                        "aggregate_type": "HOST_CONTROL",
                        "aggregate_id": "frame-local-hidden-side-effect",
                        "aggregate_version": "1",
                        "payload": payload,
                        "payload_hash": payload_digest(payload),
                        "committed_at": "2026-10-04T06:58:00Z",
                    }
                )

            with (
                patch(
                    "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
                    side_effect=(1_000, 2_000),
                ),
                self.assertRaisesRegex(
                    RuntimeLoadResearchMeasurementError,
                    "mutated the qualification JournalStore",
                ),
            ):
                measure_declared_research_interference(
                    store,
                    _spec(),
                    plan_id=research.plan_id,
                    sample_id="research-frame-sample",
                    operation=operation,
                )

            event_types = tuple(
                event["event_type"]
                for event in store.load_events_after_journal_sequence(0)
            )
            self.assertIn("HostControlObserved", event_types)
            self._assert_no_research_measurement(store)

    def test_python313_frame_locals_cannot_replace_terminal_research_clock(self):
        with tempfile.TemporaryDirectory() as root:
            store, research = self._plans(root)

            def operation() -> str:
                measurement_frame = _measurement_frame()
                measurement_frame.f_locals["clock"] = lambda: 1_001
                return "pressure-finished"

            with patch(
                "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
                side_effect=(1_000, 1_001_000),
            ):
                result, sample = measure_declared_research_interference(
                    store,
                    _spec(),
                    plan_id=research.plan_id,
                    sample_id="research-frame-sample",
                    operation=operation,
                )

            self.assertEqual(result, "pressure-finished")
            self.assertEqual(sample.monotonic_start_ns, 1_000)
            self.assertEqual(sample.monotonic_end_ns, 1_001_000)
            self.assertEqual(sample.interference_us, 1_000)

    def test_nested_research_plan_mutation_is_rejected_before_evidence_commit(self):
        with tempfile.TemporaryDirectory() as root:
            store, research = self._plans(root)

            def operation() -> None:
                measurement_frame = _measurement_frame()
                loaded_plan = measurement_frame.f_locals["plan"]
                object.__setattr__(
                    loaded_plan.expected_samples[0],
                    "phase",
                    "callback-forged-phase",
                )
                measurement_frame.f_locals["require_post_callback_authority"] = (
                    lambda: None
                )

            with (
                patch(
                    "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
                    side_effect=(2_000, 3_000),
                ),
                self.assertRaisesRegex(
                    RuntimeLoadResearchMeasurementError,
                    "plan digest changed during callback",
                ),
            ):
                measure_declared_research_interference(
                    store,
                    _spec(),
                    plan_id=research.plan_id,
                    sample_id="research-frame-sample",
                    operation=operation,
                )

            self._assert_no_research_measurement(store)


if __name__ == "__main__":
    unittest.main()
