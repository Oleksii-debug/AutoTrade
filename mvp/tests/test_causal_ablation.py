from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.causal_ablation import ShadowPair, evaluate_incremental_value


CUT = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)


def pair(pair_id, value, *, baseline_cost="0", candidate_cost="0"):
    return ShadowPair(
        pair_id=pair_id,
        task_id="task",
        input_cutoff_utc=CUT,
        baseline_decision_utc=CUT,
        candidate_decision_utc=CUT,
        outcome_available_utc=CUT + timedelta(hours=1),
        baseline_utility=Decimal("0"),
        candidate_utility=Decimal(value),
        baseline_cost=Decimal(baseline_cost),
        candidate_cost=Decimal(candidate_cost),
    )


class CausalAblationTests(unittest.TestCase):
    def test_future_leakage_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "candidate decision"):
            ShadowPair(
                pair_id="p",
                task_id="t",
                input_cutoff_utc=CUT,
                baseline_decision_utc=CUT,
                candidate_decision_utc=CUT + timedelta(seconds=1),
                outcome_available_utc=CUT + timedelta(hours=1),
                baseline_utility=Decimal("0"),
                candidate_utility=Decimal("1"),
                baseline_cost=Decimal("0"),
                candidate_cost=Decimal("0"),
            )

    def test_outcome_available_before_cutoff_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "strictly after"):
            ShadowPair(
                pair_id="p",
                task_id="t",
                input_cutoff_utc=CUT,
                baseline_decision_utc=CUT,
                candidate_decision_utc=CUT,
                outcome_available_utc=CUT,
                baseline_utility=Decimal("0"),
                candidate_utility=Decimal("1"),
                baseline_cost=Decimal("0"),
                candidate_cost=Decimal("0"),
            )

    def test_insufficient_pairs_are_inconclusive(self):
        result = evaluate_incremental_value(
            [pair("p1", "1")],
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertIsNone(result.lower_bound)

    def test_incremental_value_is_net_of_candidate_cost(self):
        result = evaluate_incremental_value(
            [
                pair("p1", "2", candidate_cost="0.5"),
                pair("p2", "2", candidate_cost="0.5"),
            ],
            minimum_pairs=2,
            required_lower_bound=Decimal("1.5"),
        )
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.mean_incremental_value, Decimal("1.5"))
        self.assertEqual(result.lower_bound, Decimal("1.5"))

    def test_uncertain_mixed_result_fails_lower_bound(self):
        result = evaluate_incremental_value(
            [pair("p1", "3"), pair("p2", "-1"), pair("p3", "2")],
            minimum_pairs=3,
            required_lower_bound=Decimal("0.5"),
        )
        self.assertEqual(result.status, "FAIL")
        self.assertLess(result.lower_bound, Decimal("0.5"))

    def test_duplicate_pair_cannot_be_double_counted(self):
        with self.assertRaisesRegex(ValueError, "duplicate shadow pair"):
            evaluate_incremental_value(
                [pair("p1", "1"), pair("p1", "1")],
                minimum_pairs=2,
                required_lower_bound=Decimal("0"),
            )


if __name__ == "__main__":
    unittest.main()
