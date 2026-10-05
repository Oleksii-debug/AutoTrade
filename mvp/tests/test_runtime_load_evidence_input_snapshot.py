import tempfile
import unittest
from pathlib import Path

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_evidence import (
    ExpectedJournalEvent,
    RuntimeLoadEvidenceError,
)
from mvp.autotrade_mvp.runtime_load_plan import (
    declare_runtime_event_plan,
    evaluate_declared_runtime_budget,
)


SHA = "a" * 40
CONFIG = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="wp65-input-snapshot",
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
        aggregate_id="wp65-input-snapshot",
        aggregate_version=1,
    )


def _append_expected(store: JournalStore, expected: ExpectedJournalEvent) -> None:
    payload = {"event_id": expected.event_id, "kind": "expected"}
    store.append_event(
        {
            "event_id": expected.event_id,
            "event_type": expected.event_type,
            "aggregate_type": expected.aggregate_type,
            "aggregate_id": expected.aggregate_id,
            "aggregate_version": str(expected.aggregate_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-03T06:50:00Z",
        }
    )


def _append_extra_financial(store: JournalStore) -> None:
    payload = {"event_id": "fin-extra", "kind": "risk"}
    store.append_event(
        {
            "event_id": "fin-extra",
            "event_type": "QualificationExtraEvent",
            "aggregate_type": "risk_decision",
            "aggregate_id": "fin-extra",
            "aggregate_version": "1",
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-03T06:50:01Z",
        }
    )


class RuntimeLoadEvidenceInputSnapshotTests(unittest.TestCase):
    def test_executable_metric_container_cannot_mutate_journal_after_plan_read(self):
        with tempfile.TemporaryDirectory() as root:
            store = JournalStore(Path(root) / "runtime-load-input-snapshot.sqlite")
            expected = _expected()
            plan = declare_runtime_event_plan(
                store,
                plan_id="input-snapshot-plan",
                spec=_spec(),
                expected_events=(expected,),
            )
            _append_expected(store, expected)

            class InjectingMetricList(list[int]):
                def __init__(self) -> None:
                    super().__init__([100])
                    self.iterated = False

                def __iter__(self):
                    self.iterated = True
                    _append_extra_financial(store)
                    return super().__iter__()

            hostile = InjectingMetricList()
            before = JournalStore.current_journal_sequence(store)
            with self.assertRaisesRegex(
                RuntimeLoadEvidenceError,
                "financial_staleness_us must be an exact tuple or list",
            ):
                evaluate_declared_runtime_budget(
                    _spec(),
                    store,
                    plan_id=plan.plan_id,
                    financial_latency_us=(100,),
                    financial_staleness_us=hostile,
                    research_interference_us=(50,),
                    reconnect_backlog_remaining=0,
                    declared_duration_us=1_000_000,
                    observed_duration_us=1_000_000,
                )

            self.assertFalse(hostile.iterated)
            self.assertEqual(JournalStore.current_journal_sequence(store), before)
            self.assertIsNone(JournalStore.get_event(store, "fin-extra"))


if __name__ == "__main__":
    unittest.main()
