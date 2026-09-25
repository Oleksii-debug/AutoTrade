import unittest

from research.autotrade_research.evaluation.gates import (
    EvaluationEvidence,
    GateDecision,
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
        primary_baseline_id="champion",
        baseline_ids=("cash", "passive", "champion"),
        selection_correction="holm-v1",
        max_trials=20,
        required_regimes=("normal", "stress"),
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
        baseline_advantages={
            "cash": "0.04",
            "passive": "0.035",
            "champion": "0.03",
        },
        selection_correction_applied="holm-v1",
        trials_attempted=12,
        regime_coverage=frozenset({"normal", "stress"}),
    )
    values.update(overrides)
    return EvaluationEvidence.create(**values)


class EvaluationGateTests(unittest.TestCase):
    def test_direct_profile_construction_cannot_bypass_registered_thresholds(self):
        with self.assertRaisesRegex(ValueError, "minimum_net_advantage"):
            GateProfile(
                profile_id="direct-bad",
                minimum_net_advantage="-0.01",
                max_drawdown="0.10",
                max_adverse_cost_loss="0.03",
                min_power="0.80",
                primary_baseline_id="champion",
                baseline_ids=("cash", "champion"),
                selection_correction="holm-v1",
                max_trials=20,
                required_regimes=("normal",),
                require_complete_trials=True,
                require_causal_audit=True,
                require_financial_invariants=True,
            )

    def test_direct_evidence_construction_enforces_exact_types_and_ranges(self):
        base = dict(
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
            baseline_advantages={"champion": "0.03"},
            selection_correction_applied="holm-v1",
            trials_attempted=1,
            regime_coverage=frozenset({"normal"}),
        )
        with self.assertRaisesRegex(TypeError, "reproducible"):
            EvaluationEvidence(**{**base, "reproducible": 1})
        with self.assertRaisesRegex(ValueError, "estimated_power"):
            EvaluationEvidence(**{**base, "estimated_power": "1.01"})
        with self.assertRaisesRegex(TypeError, "regime_coverage"):
            EvaluationEvidence(**{**base, "regime_coverage": frozenset({"normal", 7})})

    def test_complete_registered_evidence_can_pass(self):
        self.assertEqual(evaluate_gates(profile(), evidence()).status, "PASS")

    def test_lower_bound_cannot_exceed_reported_point_estimate(self):
        decision = evaluate_gates(
            profile(),
            evidence(
                dependence_aware_lower_bound="999",
                net_advantage="0.03",
            ),
        )
        self.assertEqual(decision.status, "FAIL")
        self.assertEqual(decision.checks["uncertainty_consistency"], "FAIL")
        self.assertIn(
            "lower bound exceeds",
            " ".join(decision.reasons),
        )

    def test_consistent_lower_bound_and_point_estimate_can_pass(self):
        decision = evaluate_gates(
            profile(),
            evidence(
                dependence_aware_lower_bound="0.02",
                net_advantage="0.03",
            ),
        )
        self.assertEqual(decision.checks["uncertainty_consistency"], "PASS")
        self.assertEqual(decision.status, "PASS")

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

    def test_negative_adverse_cost_loss_cannot_create_a_false_pass(self):
        with self.assertRaisesRegex(ValueError, "adverse_cost_loss must be non-negative"):
            evidence(adverse_cost_loss="-0.01")

    def test_negative_adverse_cost_limit_is_invalid_protocol(self):
        with self.assertRaisesRegex(ValueError, "max_adverse_cost_loss must be non-negative"):
            GateProfile.create(
                profile_id="bad-gate",
                minimum_net_advantage="0.01",
                max_drawdown="0.10",
                max_adverse_cost_loss="-0.03",
                min_power="0.80",
                primary_baseline_id="champion",
                baseline_ids=("cash", "passive", "champion"),
                selection_correction="holm-v1",
                max_trials=20,
                required_regimes=("normal", "stress"),
            )

    def test_negative_minimum_net_advantage_is_invalid_protocol(self):
        with self.assertRaisesRegex(ValueError, "minimum_net_advantage"):
            GateProfile.create(
                profile_id="negative-edge",
                minimum_net_advantage="-0.01",
                max_drawdown="0.10",
                max_adverse_cost_loss="0.03",
                min_power="0.80",
                primary_baseline_id="champion",
                baseline_ids=("cash", "champion"),
                selection_correction="holm-v1",
                max_trials=20,
                required_regimes=("normal",),
            )

    def test_protocol_identity_collections_reject_non_text_values(self):
        with self.assertRaises(TypeError):
            GateProfile.create(
                profile_id="bad-baseline-type",
                minimum_net_advantage="0.01",
                max_drawdown="0.10",
                max_adverse_cost_loss="0.03",
                min_power="0.80",
                primary_baseline_id="champion",
                baseline_ids=("champion", None),
                selection_correction="holm-v1",
                max_trials=20,
                required_regimes=("normal",),
            )
        with self.assertRaises(TypeError):
            GateProfile.create(
                profile_id="bad-regime-type",
                minimum_net_advantage="0.01",
                max_drawdown="0.10",
                max_adverse_cost_loss="0.03",
                min_power="0.80",
                primary_baseline_id="champion",
                baseline_ids=("champion",),
                selection_correction="holm-v1",
                max_trials=20,
                required_regimes=("normal", None),
            )

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


    def test_selection_correction_mismatch_fails(self):
        decision = evaluate_gates(
            profile(),
            evidence(selection_correction_applied="none"),
        )
        self.assertEqual(decision.status, "FAIL")
        self.assertEqual(decision.checks["selection_correction"], "FAIL")

    def test_trial_budget_exhaustion_fails_and_missing_count_is_inconclusive(self):
        self.assertEqual(
            evaluate_gates(profile(), evidence(trials_attempted=21)).status,
            "FAIL",
        )
        self.assertEqual(
            evaluate_gates(profile(), evidence(trials_attempted=None)).status,
            "INCONCLUSIVE",
        )

    def test_missing_registered_regime_fails(self):
        decision = evaluate_gates(
            profile(),
            evidence(regime_coverage=frozenset({"normal"})),
        )
        self.assertEqual(decision.status, "FAIL")
        self.assertEqual(decision.checks["regime_coverage"], "FAIL")

    def test_baseline_set_must_match_registration(self):
        decision = evaluate_gates(
            profile(),
            evidence(
                baseline_advantages={
                    "cash": "0.04",
                    "champion": "0.03",
                }
            ),
        )
        self.assertEqual(decision.status, "FAIL")
        self.assertEqual(decision.checks["baselines"], "FAIL")

    def test_primary_baseline_advantage_is_bound_to_net_advantage(self):
        decision = evaluate_gates(
            profile(),
            evidence(
                baseline_advantages={
                    "cash": "0.04",
                    "passive": "0.035",
                    "champion": "0.031",
                }
            ),
        )
        self.assertEqual(decision.status, "FAIL")
        self.assertEqual(decision.checks["primary_baseline"], "FAIL")

    def test_gate_decision_checks_are_immutable_after_evaluation(self):
        decision = evaluate_gates(profile(), evidence())
        self.assertEqual(decision.status, "PASS")
        with self.assertRaises(TypeError):
            decision.checks["net_advantage"] = "FAIL"
        self.assertEqual(decision.checks["net_advantage"], "PASS")

    def test_gate_decision_defensively_copies_mutable_checks(self):
        checks = {"net_advantage": "PASS"}
        decision = GateDecision(
            status="PASS",
            reasons=("registered gate passed",),
            checks=checks,
        )
        checks["net_advantage"] = "FAIL"
        self.assertEqual(decision.checks["net_advantage"], "PASS")

    def test_forged_gate_decision_statuses_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "status"):
            GateDecision(
                status="APPROVED",
                reasons=("forged",),
                checks={"net_advantage": "PASS"},
            )
        with self.assertRaisesRegex(ValueError, "check status"):
            GateDecision(
                status="PASS",
                reasons=("forged",),
                checks={"net_advantage": "APPROVED"},
            )

    def test_empty_selection_controls_are_invalid_protocol(self):
        with self.assertRaises(ValueError):
            GateProfile.create(
                profile_id="bad",
                minimum_net_advantage="0.01",
                max_drawdown="0.1",
                max_adverse_cost_loss="0.03",
                min_power="0.8",
                primary_baseline_id="champion",
                baseline_ids=(),
                selection_correction="holm-v1",
                max_trials=10,
                required_regimes=("normal",),
            )
        with self.assertRaises(ValueError):
            GateProfile.create(
                profile_id="bad",
                minimum_net_advantage="0.01",
                max_drawdown="0.1",
                max_adverse_cost_loss="0.03",
                min_power="0.8",
                primary_baseline_id="champion",
                baseline_ids=("champion",),
                selection_correction="",
                max_trials=10,
                required_regimes=("normal",),
            )


if __name__ == "__main__":
    unittest.main()
