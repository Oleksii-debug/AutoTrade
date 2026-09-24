import unittest
from decimal import Decimal

from mvp.autotrade_mvp.robust_statistics import (
    EdgeEvidence,
    holm_bonferroni,
    paired_block_bootstrap,
    qualify_incremental_edge,
)


class RobustStatisticsTests(unittest.TestCase):
    def test_paired_bootstrap_is_deterministic_with_exact_seed(self):
        candidate = [Decimal("0.03"), Decimal("0.02"), Decimal("0.04"), Decimal("0.01")] * 30
        baseline = [Decimal("0.00"), Decimal("0.00"), Decimal("0.01"), Decimal("0.00")] * 30
        first = paired_block_bootstrap(candidate, baseline, block_length=4, replications=500, seed=7)
        second = paired_block_bootstrap(candidate, baseline, block_length=4, replications=500, seed=7)
        self.assertEqual(first, second)
        self.assertGreater(first.lower_bound, 0)

    def test_pairing_matters_and_equal_length_is_required(self):
        with self.assertRaisesRegex(ValueError, "equal non-empty paired"):
            paired_block_bootstrap(["0.1"], ["0.1", "0.2"], block_length=1, replications=100, seed=1)

    def test_block_length_is_explicit_not_silently_independent(self):
        with self.assertRaisesRegex(ValueError, "invalid block_length"):
            paired_block_bootstrap(["0.1", "0.2"], ["0", "0"], block_length=3, replications=100, seed=1)

    def test_holm_bonferroni_stops_after_first_non_rejection(self):
        result = holm_bonferroni({"a": "0.001", "b": "0.03", "c": "0.04"}, alpha="0.05")
        self.assertEqual(result.ordered_hypotheses, ("a", "b", "c"))
        self.assertEqual(result.rejected, ("a",))
        self.assertEqual(result.adjusted_thresholds["a"], Decimal("0.05") / Decimal("3"))

    def test_multiple_comparisons_prevent_raw_p_value_cherry_pick(self):
        result = holm_bonferroni({f"h{i}": "0.04" for i in range(10)}, alpha="0.05")
        self.assertEqual(result.rejected, ())

    def test_edge_requires_statistics_forward_costs_and_leakage_together(self):
        candidate = ["0.03", "0.02", "0.04", "0.01"] * 30
        baseline = ["0", "0", "0.01", "0"] * 30
        boot = paired_block_bootstrap(candidate, baseline, block_length=4, replications=500, seed=9)
        multi = holm_bonferroni({"candidate": "0.001", "other": "0.9"})
        passed = qualify_incremental_edge(
            EdgeEvidence(boot, "candidate", multi, True, True, True)
        )
        self.assertTrue(passed.qualified)
        failed = qualify_incremental_edge(
            EdgeEvidence(boot, "candidate", multi, False, False, False)
        )
        self.assertFalse(failed.qualified)
        self.assertIn("EDGE.FORWARD_EVIDENCE_MISSING", failed.blockers)
        self.assertIn("EDGE.COSTS_NOT_INCLUDED", failed.blockers)
        self.assertIn("EDGE.LEAKAGE_CHECK_FAILED", failed.blockers)

    def test_positive_mean_is_not_enough_when_interval_crosses_zero(self):
        candidate = ["1", "-0.9"] * 60
        baseline = ["0"] * 120
        boot = paired_block_bootstrap(candidate, baseline, block_length=1, replications=1000, seed=4)
        self.assertGreater(boot.mean_delta, 0)
        self.assertLessEqual(boot.lower_bound, 0)


if __name__ == "__main__":
    unittest.main()
