import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetError, RuntimeBudgetSpec
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
        "committed_at": "2026-10-03T04:34:00+00:00",
    }


class RuntimeCampaignStartBracketTests(unittest.TestCase):
    def test_expected_event_committed_before_start_clock_cannot_boost_timed_throughput(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = RuntimeBudgetSpec(
                scenario_id="wp65-start-bracket",
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
            plan = RuntimeCampaignPlan.create(
                spec=spec,
                workload_profile_hash=WORKLOAD,
                declared_duration_ms=1,
                expected_financial_event_ids=("fin-raced",),
                financial_aggregate_types=("risk_decision",),
            )

            def starting_clock() -> int:
                # Current ordering freezes the journal cursor first. This expected
                # event then commits before the sampled start time, yet remains
                # inside the recovered cut and can inflate timed throughput.
                journal.append_event(_envelope("fin-raced"))
                return 1_000_000_000

            try:
                with patch(
                    "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                    side_effect=starting_clock,
                ):
                    cut = begin_runtime_campaign(
                        journal=journal,
                        spec=spec,
                        plan=plan,
                    )
                with patch(
                    "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                    return_value=1_000_001_000,
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
            except RuntimeBudgetError:
                # A repaired begin cut may fail closed if the journal changes
                # while the start clock is sampled.
                return

            decision = evaluate_runtime_campaign(spec, evidence)
            self.assertNotEqual(
                decision.status,
                "PASS",
                "an expected financial event committed before the sampled start "
                "clock was credited to the measured campaign throughput",
            )


if __name__ == "__main__":
    unittest.main()
