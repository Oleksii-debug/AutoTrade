from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetError, RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_qualification import (
    RuntimeCampaignPlan,
    begin_runtime_campaign,
    collect_runtime_campaign_evidence,
)


SHA = "a" * 40
CONFIG = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)
WORKLOAD = "sha256:" + ("d" * 64)
RESOURCE = "sha256:" + ("e" * 64)


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="wp65-nested-text-authority",
        release_sha=SHA,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        strategy_horizon_us=10_000,
        max_p95_financial_latency_us=10_000,
        max_financial_staleness_us=10_000,
        max_research_interference_us=10_000,
        min_financial_samples=1,
        min_research_samples=1,
    )


def _envelope(event_id: str) -> dict[str, object]:
    payload = {"event_id": event_id, "kind": "risk_decision"}
    return {
        "event_id": event_id,
        "event_type": "QualificationEvent",
        "aggregate_type": "risk_decision",
        "aggregate_id": event_id,
        "aggregate_version": "1",
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": "2026-10-03T06:00:00+00:00",
    }


class RuntimeLoadNestedTextAuthorityTests(unittest.TestCase):
    def test_expected_event_identity_rejects_executable_str_subclass_at_construction(self):
        class ForgedExpectedId(str):
            def strip(self):
                raise AssertionError("executable nested text must never be evaluated")

            def __hash__(self):
                raise AssertionError("executable nested text must never be hashed")

            def __eq__(self, other):
                raise AssertionError("executable nested text must never be compared")

        with self.assertRaisesRegex(
            RuntimeBudgetError,
            "expected_financial_event_ids must contain exact strings",
        ):
            RuntimeCampaignPlan.create(
                spec=_spec(),
                workload_profile_hash=WORKLOAD,
                declared_duration_ms=1,
                expected_financial_event_ids=(ForgedExpectedId("fin-expected"),),
                financial_aggregate_types=("risk_decision",),
            )

    def test_issued_plan_rejects_executable_container_before_iteration_or_journal_mutation(self):
        spec = _spec()
        plan = RuntimeCampaignPlan.create(
            spec=spec,
            workload_profile_hash=WORKLOAD,
            declared_duration_ms=1,
            expected_financial_event_ids=("fin-expected",),
            financial_aggregate_types=("risk_decision",),
        )

        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                return_value=1_000_000_000,
            ):
                cut = begin_runtime_campaign(journal=journal, spec=spec, plan=plan)

            class ExecutableExpectedIds(list):
                iterated = False

                def __iter__(self):
                    self.iterated = True
                    journal.append_event(_envelope("fin-expected"))
                    return super().__iter__()

            hostile = ExecutableExpectedIds(["fin-expected"])
            object.__setattr__(plan, "expected_financial_event_ids", hostile)
            before_sequence = JournalStore.current_journal_sequence(journal)

            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                return_value=1_001_000_000,
            ), self.assertRaisesRegex(
                RuntimeBudgetError,
                "expected_financial_event_ids must remain an exact tuple",
            ):
                collect_runtime_campaign_evidence(
                    journal=journal,
                    spec=spec,
                    plan=plan,
                    cut=cut,
                    financial_latency_us=(100,),
                    financial_staleness_us=(100,),
                    research_interference_us=(100,),
                    resource_evidence_hash=RESOURCE,
                    resource_metrics={"cpu_peak_millis": 1},
                )

            self.assertFalse(hostile.iterated)
            self.assertEqual(
                JournalStore.current_journal_sequence(journal),
                before_sequence,
            )
            self.assertIsNone(JournalStore.get_event(journal, "fin-expected"))


if __name__ == "__main__":
    unittest.main()
