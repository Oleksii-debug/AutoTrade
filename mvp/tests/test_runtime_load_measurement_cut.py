import tempfile
import unittest
from pathlib import Path

import mvp.autotrade_mvp.runtime_load_measurement as measurement
from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan


SHA = "a" * 40
HASH = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="measurement-cut",
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
        event_id="measurement-cut-financial-1",
        event_type="RuntimeQualificationFinancialEvent",
        aggregate_type="measurement_cut_fixture",
        aggregate_id="measurement-cut",
        aggregate_version=1,
    )


def _append_financial(store: JournalStore, expected: ExpectedJournalEvent) -> dict[str, object]:
    payload = {"scenario": "measurement-cut", "event_id": expected.event_id}
    store.append_event(
        {
            "event_id": expected.event_id,
            "event_type": expected.event_type,
            "aggregate_type": expected.aggregate_type,
            "aggregate_id": expected.aggregate_id,
            "aggregate_version": "1",
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-03T04:05:00Z",
        }
    )
    event = store.get_event(expected.event_id)
    assert event is not None
    return event


class RuntimeLoadMeasurementCutTests(unittest.TestCase):
    def test_retained_sample_cannot_claim_pre_operation_cut_before_plan(self):
        with tempfile.TemporaryDirectory() as root:
            store = JournalStore(Path(root) / "measurement-cut.sqlite")
            spec = _spec()
            expected = _expected()
            plan = declare_runtime_event_plan(
                store,
                plan_id="measurement-cut-plan",
                spec=spec,
                expected_events=(expected,),
            )
            self.assertGreater(plan.declared_journal_sequence, 0)
            financial_event = _append_financial(store, expected)

            start_ns = 1_000_000
            end_ns = 1_001_000
            payload = {
                "schema_version": "1.0.0",
                "plan_id": plan.plan_id,
                "plan_digest": plan.digest,
                "spec_digest": plan.spec_digest,
                "expected_event": expected.payload,
                "event_journal_sequence": financial_event["journal_sequence"],
                "event_payload_hash": financial_event["payload_hash"],
                "pre_operation_journal_sequence": plan.declared_journal_sequence - 1,
                "monotonic_start_ns": start_ns,
                "monotonic_end_ns": end_ns,
                "latency_us": 1,
            }
            store.append_event(
                {
                    "event_id": measurement._measurement_event_id(
                        plan.plan_id,
                        expected.event_id,
                    ),
                    "event_type": "RuntimeQualificationFinancialLatencyMeasured",
                    "aggregate_type": "runtime_qualification_latency",
                    "aggregate_id": plan.plan_id,
                    "aggregate_version": "1",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": "2026-10-03T04:05:01Z",
                }
            )

            with self.assertRaisesRegex(
                measurement.RuntimeLoadMeasurementError,
                "pre-operation cut predates its durable plan",
            ):
                measurement.load_declared_financial_latency_samples(
                    store,
                    spec,
                    plan_id=plan.plan_id,
                )


if __name__ == "__main__":
    unittest.main()
