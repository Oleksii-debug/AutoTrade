import unittest

from research.autotrade_research.evaluation.gates import (
    EvaluationEvidence,
    GateProfile,
    evaluate_gates,
)


def profile():
    return GateProfile.create(
        profile_id="gate-v1",
        minimum_net_advantage="0.01",
        max_drawdown="0.10",
        max_adverse_cost_loss="0.03",
        min_power="0.80",
    )


def evidence(**overrides):
    values = dict(
        registered_profile_id="gate-v1",
        profile_unchanged_after_results=True,
        reproducible=True,
        causal_audit_passed=True,
        financial_invariants_passed=True,
        trial_log_complete=True,
        dependence_aware_lower_bound="0.02",
        estimated_power="0.85",
        net_advantage="0.03",
        drawdown="0.05",
        adverse_cost_loss="0.01",
        retention_passed=True,
    )
    values.update(overrides)
    return EvaluationEvidence.create(**values)


class EvaluationGateTests(unittest.TestCase):
    def test_complete_registered_evidence_can_pass(self):
        self.assertEqual(evaluate_gates(profile(), evidence()).status, "PASS")

    def test_missing_uncertainty_is_inconclusive_not_pass(self):
        self.assertEqual(
            evaluate_gates(profile(), evidence(dependence_aware_lower_bound=None)).status,
            "INCONCLUSIVE",
        )

    def test_insufficient_power_is_fail(self):
        self.assertEqual(evaluate_gates(profile(), evidence(estimated_power="0.50")).status, "FAIL")

    def test_hidden_failed_trials_fail(self):
        self.assertEqual(evaluate_gates(profile(), evidence(trial_log_complete=False)).status, "FAIL")

    def test_adverse_cost_stress_can_fail_good_base_result(self):
        self.assertEqual(evaluate_gates(profile(), evidence(adverse_cost_loss="0.10")).status, "FAIL")

    def test_profile_changed_after_result_fails(self):
        self.assertEqual(
            evaluate_gates(profile(), evidence(profile_unchanged_after_results=False)).status,
            "FAIL",
        )

    def test_retention_regression_fails(self):
        self.assertEqual(evaluate_gates(profile(), evidence(retention_passed=False)).status, "FAIL")

    def test_missing_causal_audit_is_inconclusive(self):
        self.assertEqual(
            evaluate_gates(profile(), evidence(causal_audit_passed=None)).status,
            "INCONCLUSIVE",
        )


if __name__ == "__main__":
    unittest.main()
