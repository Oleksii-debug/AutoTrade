from decimal import Decimal
import unittest

from research.autotrade_research.learning.retention import (
    RegimeMetric,
    RetentionPolicy,
    evaluate_retention,
)


def metric(regime, champion, candidate, observations=100, label_complete=True):
    return RegimeMetric.create(
        regime=regime,
        champion_net_score=champion,
        candidate_net_score=candidate,
        observations=observations,
        label_complete=label_complete,
    )


def policy(**overrides):
    values = dict(
        protected_regimes=["old"],
        recent_regimes=["new"],
        max_protected_degradation="0.02",
        min_recent_improvement="0.01",
        min_observations_per_regime=50,
        require_complete_labels=True,
        independent_science_gate_passed=True,
        risk_gate_passed=True,
    )
    values.update(overrides)
    return RetentionPolicy.create(**values)


class RetentionTests(unittest.TestCase):
    def test_recent_gain_with_retained_old_regime_can_pass(self):
        result = evaluate_retention(
            {"old": metric("old", "0.10", "0.09"), "new": metric("new", "0.05", "0.08")},
            policy(),
        )
        self.assertTrue(result.promotable)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.recent_improvement, Decimal("0.03"))

    def test_recent_gain_cannot_hide_old_regime_regression(self):
        result = evaluate_retention(
            {"old": metric("old", "0.10", "0.05"), "new": metric("new", "0.05", "0.20")},
            policy(),
        )
        self.assertFalse(result.promotable)
        self.assertEqual(result.status, "FAIL")
        self.assertTrue(any("protected-regime" in row.reason for row in result.regimes))

    def test_recent_average_cannot_hide_another_recent_regime_regression(self):
        result = evaluate_retention(
            {
                "old": metric("old", "0.10", "0.10"),
                "new-a": metric("new-a", "0.10", "0.16"),
                "new-b": metric("new-b", "0.10", "0.09"),
            },
            policy(recent_regimes=["new-a", "new-b"]),
        )
        self.assertEqual(result.recent_improvement, Decimal("0.025"))
        self.assertFalse(result.promotable)
        self.assertEqual(result.status, "FAIL")
        failed = {row.regime: row.reason for row in result.regimes if not row.passed}
        self.assertIn("recent-regime degradation exceeds tolerance", failed["new-b"])

    def test_registered_recent_degradation_tolerance_is_explicit(self):
        result = evaluate_retention(
            {
                "old": metric("old", "0.10", "0.10"),
                "new-a": metric("new-a", "0.10", "0.16"),
                "new-b": metric("new-b", "0.10", "0.09"),
            },
            policy(
                recent_regimes=["new-a", "new-b"],
                max_recent_degradation="0.01",
                min_recent_improvement="0.02",
            ),
        )
        self.assertTrue(result.promotable)
        self.assertEqual(result.status, "PASS")

    def test_negative_recent_degradation_tolerance_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "max_recent_degradation must be non-negative"):
            policy(max_recent_degradation="-0.001")

    def test_delayed_labels_are_inconclusive_not_failure_or_pass(self):
        result = evaluate_retention(
            {"old": metric("old", "0.10", "0.10"), "new": metric("new", "0.05", "0.20", label_complete=False)},
            policy(),
        )
        self.assertFalse(result.promotable)
        self.assertEqual(result.status, "INCONCLUSIVE")

    def test_missing_regime_is_inconclusive(self):
        result = evaluate_retention({"new": metric("new", "0.05", "0.20")}, policy())
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertFalse(result.promotable)

    def test_recent_threshold_prevents_drift_false_alarm_promotion(self):
        result = evaluate_retention(
            {"old": metric("old", "0.10", "0.10"), "new": metric("new", "0.05", "0.055")},
            policy(min_recent_improvement="0.01"),
        )
        self.assertEqual(result.status, "FAIL")
        self.assertFalse(result.promotable)

    def test_science_gate_cannot_be_bypassed(self):
        result = evaluate_retention(
            {"old": metric("old", "0.10", "0.10"), "new": metric("new", "0.05", "0.20")},
            policy(independent_science_gate_passed=False),
        )
        self.assertEqual(result.status, "FAIL")
        self.assertFalse(result.promotable)

    def test_risk_gate_cannot_be_bypassed(self):
        result = evaluate_retention(
            {"old": metric("old", "0.10", "0.10"), "new": metric("new", "0.05", "0.20")},
            policy(risk_gate_passed=False),
        )
        self.assertEqual(result.status, "FAIL")
        self.assertFalse(result.promotable)


if __name__ == "__main__":
    unittest.main()
