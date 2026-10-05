import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_evidence import (
    ExpectedJournalEvent,
    RuntimeLoadEvidenceError,
)
from mvp.autotrade_mvp.runtime_load_measurement import (
    evaluate_monotonic_declared_runtime_budget,
    measure_declared_financial_operation,
)
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan


SHA = "a" * 40
CONFIG = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="wp65-full-cut",
        release_sha=SHA,
        configuration_hash=CONFIG,
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
        event_id="fin-expected",
        event_type="RuntimeQualificationFinancialEvent",
        aggregate_type="risk_decision",
        aggregate_id="wp65-full-cut",
        aggregate_version=1,
    )


def _append_expected(store: JournalStore, expected: ExpectedJournalEvent) -> None:
    payload = {"event_id": expected.event_id, "kind": "fixture"}
    store.append_event(
        {
            "event_id": expected.event_id,
            "event_type": expected.event_type,
            "aggregate_type": expected.aggregate_type,
            "aggregate_id": expected.aggregate_id,
            "aggregate_version": str(expected.aggregate_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-03T06:40:00Z",
        }
    )


def _append_extra(store: JournalStore, *, aggregate_type: str) -> None:
    payload = {"event_id": "fin-extra", "kind": aggregate_type}
    store.append_event(
        {
            "event_id": "fin-extra",
            "event_type": "QualificationExtraEvent",
            "aggregate_type": aggregate_type,
            "aggregate_id": "fin-extra",
            "aggregate_version": "1",
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-03T06:40:01Z",
        }
    )


class RuntimeLoadEvidenceFullCutTests(unittest.TestCase):
    def _measured_store(self, root: str) -> tuple[JournalStore, str]:
        store = JournalStore(Path(root) / "runtime-load-full-cut.sqlite")
        expected = _expected()
        plan = declare_runtime_event_plan(
            store,
            plan_id="full-cut-plan",
            spec=_spec(),
            expected_events=(expected,),
        )
        with patch(
            "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
            side_effect=(1_000_000, 1_100_000),
        ):
            measure_declared_financial_operation(
                store,
                _spec(),
                plan_id=plan.plan_id,
                event_id=expected.event_id,
                operation=lambda: _append_expected(store, expected),
            )
        return store, plan.plan_id

    def test_monotonic_budget_rejects_undeclared_canonical_financial_event(self):
        with tempfile.TemporaryDirectory() as root:
            store, plan_id = self._measured_store(root)
            _append_extra(store, aggregate_type="risk_decision")

            with self.assertRaisesRegex(
                RuntimeLoadEvidenceError,
                "undeclared financial event",
            ):
                evaluate_monotonic_declared_runtime_budget(
                    _spec(),
                    store,
                    plan_id=plan_id,
                    financial_staleness_us=(100,),
                    research_interference_us=(50,),
                    reconnect_backlog_remaining=0,
                    declared_duration_us=1_000_000,
                    observed_duration_us=1_000_000,
                )

    def test_monotonic_budget_rejects_unknown_durable_aggregate(self):
        with tempfile.TemporaryDirectory() as root:
            store, plan_id = self._measured_store(root)
            _append_extra(store, aggregate_type="future_unknown_runtime_writer")

            with self.assertRaisesRegex(
                RuntimeLoadEvidenceError,
                "unclassified durable aggregate",
            ):
                evaluate_monotonic_declared_runtime_budget(
                    _spec(),
                    store,
                    plan_id=plan_id,
                    financial_staleness_us=(100,),
                    research_interference_us=(50,),
                    reconnect_backlog_remaining=0,
                    declared_duration_us=1_000_000,
                    observed_duration_us=1_000_000,
                )


if __name__ == "__main__":
    unittest.main()
