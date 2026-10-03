from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_qualification import (
    RuntimeCampaignPlan,
    begin_runtime_campaign,
    collect_runtime_campaign_evidence,
    evaluate_runtime_campaign,
)


SHA = "a" * 40
CONFIG = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)
WORKLOAD = "sha256:" + ("d" * 64)
RESOURCE = "sha256:" + ("e" * 64)


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="wp65-issued-object-nonterminal",
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


def _event(event_id: str) -> dict[str, object]:
    payload = {"event_id": event_id, "kind": "risk_decision"}
    return {
        "event_id": event_id,
        "event_type": "QualificationEvent",
        "aggregate_type": "risk_decision",
        "aggregate_id": event_id,
        "aggregate_version": "1",
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": "2026-10-03T06:45:00+00:00",
    }


def _plan(spec: RuntimeBudgetSpec) -> RuntimeCampaignPlan:
    return RuntimeCampaignPlan.create(
        spec=spec,
        workload_profile_hash=WORKLOAD,
        declared_duration_ms=1,
        expected_financial_event_ids=("fin-1",),
        financial_aggregate_types=("risk_decision",),
    )


class RuntimeLoadIssuedObjectNonterminalTests(unittest.TestCase):
    def test_mutated_cut_cannot_produce_terminal_pass(self):
        spec = _spec()
        plan = _plan(spec)
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                return_value=1_000_000_000,
            ):
                cut = begin_runtime_campaign(journal=journal, spec=spec, plan=plan)
            journal.append_event(_event("fin-1"))
            object.__setattr__(cut, "started_monotonic_ns", 1_000_900_000)
            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                return_value=1_001_000_000,
            ):
                evidence = collect_runtime_campaign_evidence(
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
        self.assertNotEqual(evaluate_runtime_campaign(spec, evidence).status, "PASS")

    def test_mutated_evidence_cannot_turn_fail_into_terminal_pass(self):
        spec = _spec()
        plan = _plan(spec)
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                return_value=1_000_000_000,
            ):
                cut = begin_runtime_campaign(journal=journal, spec=spec, plan=plan)
            journal.append_event(_event("fin-1"))
            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                return_value=1_001_000_000,
            ):
                evidence = collect_runtime_campaign_evidence(
                    journal=journal,
                    spec=spec,
                    plan=plan,
                    cut=cut,
                    financial_latency_us=(900,),
                    financial_staleness_us=(100,),
                    research_interference_us=(100,),
                    resource_evidence_hash=RESOURCE,
                    resource_metrics={"cpu_peak_millis": 1},
                )
        self.assertEqual(evaluate_runtime_campaign(spec, evidence).status, "FAIL")
        object.__setattr__(evidence, "financial_latency_us", (100,))
        self.assertNotEqual(evaluate_runtime_campaign(spec, evidence).status, "PASS")


if __name__ == "__main__":
    unittest.main()
