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


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="nested-scalar-authority",
        release_sha=SHA,
        configuration_hash=HASH,
        host_fingerprint=HOST,
        strategy_horizon_us=1_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=2,
        min_research_samples=1,
    )


def _observation(current: RuntimeBudgetSpec) -> RuntimeLoadObservation:
    return RuntimeLoadObservation.create(
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
        declared_duration_us=1_000_000,
        observed_duration_us=900_000,
    )


class RuntimeBudgetNestedScalarAuthorityTests(unittest.TestCase):
    def test_evaluator_rejects_hostile_nested_integer_in_budget_spec(self):
        current = _spec()

        class FavorableBudgetInt(int):
            def __lt__(self, other):
                return False

            def __gt__(self, other):
                return False

        object.__setattr__(
            current,
            "max_p95_financial_latency_us",
            FavorableBudgetInt(1),
        )
        observation = _observation(current)

        with self.assertRaisesRegex(RuntimeBudgetError, "max_p95_financial_latency_us"):
            evaluate_runtime_budget(current, observation)

    def test_evaluator_rejects_hostile_nested_integer_in_observation(self):
        current = _spec()
        observation = _observation(current)

        class HiddenBacklogInt(int):
            def __lt__(self, other):
                return False

            def __ne__(self, other):
                return False

        object.__setattr__(
            observation,
            "reconnect_backlog_remaining",
            HiddenBacklogInt(1),
        )

        with self.assertRaisesRegex(RuntimeBudgetError, "reconnect_backlog_remaining"):
            evaluate_runtime_budget(current, observation)


if __name__ == "__main__":
    unittest.main()
