import unittest

from mvp.autotrade_mvp.performance_qualification import (
    RuntimeBudgetError,
    RuntimeBudgetSpec,
    RuntimeLoadObservation,
    evaluate_runtime_budget,
)


SHA = "a" * 40
HASH = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)


def spec():
    return RuntimeBudgetSpec(
        scenario_id="declared-load",
        release_sha=SHA,
        configuration_hash=HASH,
        host_fingerprint=HOST,
        strategy_horizon_us=1000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=2,
        min_research_samples=1,
    )


class RuntimePerformanceQualificationTests(unittest.TestCase):
    def test_direct_observation_cannot_hide_negative_latency_or_staleness(self):
        current = spec()
        with self.assertRaisesRegex(RuntimeBudgetError, "financial_staleness_us"):
            RuntimeLoadObservation(
                scenario_id=current.scenario_id,
                spec_digest=current.digest,
                release_sha=SHA,
                configuration_hash=HASH,
                host_fingerprint=HOST,
                expected_financial_events=2,
                recovered_financial_events=2,
                financial_latency_us=(10, 20),
                financial_staleness_us=(-1, 10),
                research_interference_us=(10,),
                reconnect_backlog_remaining=0,
            )

    def test_direct_observation_cannot_use_boolean_event_counts(self):
        current = spec()
        with self.assertRaisesRegex(RuntimeBudgetError, "expected_financial_events"):
            RuntimeLoadObservation(
                scenario_id=current.scenario_id,
                spec_digest=current.digest,
                release_sha=SHA,
                configuration_hash=HASH,
                host_fingerprint=HOST,
                expected_financial_events=True,
                recovered_financial_events=1,
                financial_latency_us=(10, 20),
                financial_staleness_us=(10, 20),
                research_interference_us=(10,),
                reconnect_backlog_remaining=0,
            )

    def test_valid_factory_evidence_still_passes_declared_budget(self):
        current = spec()
        observation = RuntimeLoadObservation.create(
            scenario_id=current.scenario_id,
            spec_digest=current.digest,
            release_sha=SHA,
            configuration_hash=HASH,
            host_fingerprint=HOST,
            expected_financial_events=2,
            recovered_financial_events=2,
            financial_latency_us=(100, 200),
            financial_staleness_us=(100, 200),
            research_interference_us=(50,),
            reconnect_backlog_remaining=0,
        )
        decision = evaluate_runtime_budget(current, observation)
        self.assertEqual(decision.status, "PASS")
        self.assertEqual(decision.metrics["recovered_financial_events"], 2)


if __name__ == "__main__":
    unittest.main()
