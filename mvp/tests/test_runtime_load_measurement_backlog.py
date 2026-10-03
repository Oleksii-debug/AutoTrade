import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_measurement import (
    RuntimeLoadMeasurementError,
    evaluate_monotonic_declared_runtime_budget,
    measure_declared_financial_operation,
)
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan


SHA = "a" * 40
HASH = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="durable-backlog",
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
        event_id="durable-backlog-financial-1",
        event_type="RuntimeQualificationFinancialEvent",
        aggregate_type="durable_backlog_fixture",
        aggregate_id="durable-backlog",
        aggregate_version=1,
    )


def _append_with_outbox(store: JournalStore, expected: ExpectedJournalEvent) -> None:
    payload = {"scenario": "durable-backlog", "event_id": expected.event_id}
    store.append_event(
        {
            "event_id": expected.event_id,
            "event_type": expected.event_type,
            "aggregate_type": expected.aggregate_type,
            "aggregate_id": expected.aggregate_id,
            "aggregate_version": str(expected.aggregate_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-03T04:10:00Z",
        },
        outbox_topic="financial.events",
    )


class RuntimeLoadMeasurementBacklogTests(unittest.TestCase):
    def _measured_store(self, root: str):
        store = JournalStore(Path(root) / "runtime-load-backlog.sqlite")
        spec = _spec()
        expected = _expected()
        plan = declare_runtime_event_plan(
            store,
            plan_id="durable-backlog-plan",
            spec=spec,
            expected_events=(expected,),
        )
        with patch(
            "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
            side_effect=(1_000_000, 1_100_000),
        ):
            measure_declared_financial_operation(
                store,
                spec,
                plan_id=plan.plan_id,
                event_id=expected.event_id,
                operation=lambda: _append_with_outbox(store, expected),
            )
        self.assertEqual(store.pending_outbox_count(), 1)
        return store, spec, plan

    def test_caller_cannot_assert_zero_over_nonzero_durable_backlog(self):
        with tempfile.TemporaryDirectory() as root:
            store, spec, plan = self._measured_store(root)
            with self.assertRaisesRegex(
                RuntimeLoadMeasurementError,
                "assertion conflicts with durable outbox",
            ):
                evaluate_monotonic_declared_runtime_budget(
                    spec,
                    store,
                    plan_id=plan.plan_id,
                    financial_staleness_us=(100,),
                    research_interference_us=(50,),
                    reconnect_backlog_remaining=0,
                )

    def test_omitted_backlog_is_derived_and_nonzero_backlog_fails_budget(self):
        with tempfile.TemporaryDirectory() as root:
            store, spec, plan = self._measured_store(root)
            decision, evidence, loaded_plan, samples = (
                evaluate_monotonic_declared_runtime_budget(
                    spec,
                    store,
                    plan_id=plan.plan_id,
                    financial_staleness_us=(100,),
                    research_interference_us=(50,),
                )
            )
            self.assertEqual(loaded_plan.digest, plan.digest)
            self.assertEqual(len(samples), 1)
            self.assertEqual(evidence.missing_event_ids, ())
            self.assertEqual(decision.status, "FAIL")
            self.assertIn("reconnect_backlog_not_drained", decision.reasons)
            self.assertEqual(decision.metrics["reconnect_backlog_remaining"], 1)


if __name__ == "__main__":
    unittest.main()
