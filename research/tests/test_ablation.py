from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from autotrade_research.evaluation.ablation import (
    AblationOutcome,
    AblationPair,
    summarize_ablation,
)


CUTOFF = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)


def outcome(
    *,
    variant,
    utility,
    cost,
    elapsed,
    components,
    fingerprint="same",
    cutoff=CUTOFF,
):
    return AblationOutcome(
        case_id="case-1",
        input_fingerprint=fingerprint,
        variant=variant,
        information_cutoff=cutoff,
        utility=utility if isinstance(utility, Decimal) else Decimal(str(utility)),
        cost=cost if isinstance(cost, Decimal) else Decimal(str(cost)),
        elapsed_ms=elapsed,
        deadline_ms=100,
        components=tuple(components),
    )


class AblationTests(unittest.TestCase):
    def test_matched_pair_reports_descriptive_value_cost_and_latency(self):
        pair = AblationPair(
            "news-source",
            outcome(variant="FULL", utility="0.8", cost="0.12", elapsed=80,
                    components=("base", "news-source")),
            outcome(variant="ABLATED", utility="0.5", cost="0.02", elapsed=50,
                    components=("base",)),
        )
        summary = summarize_ablation("news-source", [pair])
        self.assertEqual(summary.status, "DESCRIPTIVE_ONLY")
        self.assertEqual(summary.mean_utility_delta, Decimal("0.3"))
        self.assertEqual(summary.mean_cost_delta, Decimal("0.10"))
        self.assertEqual(summary.mean_latency_delta_ms, Decimal("30"))

    def test_input_fingerprint_must_match(self):
        with self.assertRaisesRegex(ValueError, "input_fingerprint"):
            AblationPair(
                "agent",
                outcome(variant="FULL", utility=1, cost=1, elapsed=10,
                        components=("base", "agent"), fingerprint="a"),
                outcome(variant="ABLATED", utility=1, cost=0, elapsed=10,
                        components=("base",), fingerprint="b"),
            )

    def test_matched_pair_must_share_information_cutoff(self):
        with self.assertRaisesRegex(ValueError, "information_cutoff"):
            AblationPair(
                "agent",
                outcome(
                    variant="FULL",
                    utility=Decimal("1"),
                    cost=Decimal("1"),
                    elapsed=10,
                    components=("base", "agent"),
                    cutoff=CUTOFF,
                ),
                outcome(
                    variant="ABLATED",
                    utility=Decimal("1"),
                    cost=Decimal("0"),
                    elapsed=10,
                    components=("base",),
                    cutoff=CUTOFF + timedelta(seconds=1),
                ),
            )

    def test_information_cutoff_must_be_timezone_aware(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            outcome(
                variant="FULL",
                utility=Decimal("1"),
                cost=Decimal("1"),
                elapsed=10,
                components=("base",),
                cutoff=datetime(2026, 9, 24, 16, 0),
            )

    def test_binary_float_metrics_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "exact decimal"):
            AblationOutcome(
                case_id="case-1",
                input_fingerprint="same",
                variant="FULL",
                information_cutoff=CUTOFF,
                utility=0.1,
                cost=Decimal("0"),
                elapsed_ms=10,
                deadline_ms=100,
                components=("base",),
            )
        with self.assertRaisesRegex(ValueError, "exact decimal"):
            AblationOutcome(
                case_id="case-1",
                input_fingerprint="same",
                variant="FULL",
                information_cutoff=CUTOFF,
                utility=Decimal("1"),
                cost=0.1,
                elapsed_ms=10,
                deadline_ms=100,
                components=("base",),
            )

    def test_pair_may_only_remove_target_component(self):
        with self.assertRaisesRegex(ValueError, "differ only"):
            AblationPair(
                "agent",
                outcome(variant="FULL", utility=1, cost=1, elapsed=10,
                        components=("base", "agent")),
                outcome(variant="ABLATED", utility=1, cost=0, elapsed=10,
                        components=("other",)),
            )

    def test_deadline_asymmetry_is_not_scored_as_comparable_utility(self):
        pair = AblationPair(
            "model",
            outcome(variant="FULL", utility=1, cost="0.20", elapsed=120,
                    components=("base", "model")),
            outcome(variant="ABLATED", utility="0.4", cost="0", elapsed=50,
                    components=("base",)),
        )
        summary = summarize_ablation("model", [pair])
        self.assertEqual(summary.status, "INCONCLUSIVE")
        self.assertIsNone(summary.mean_utility_delta)
        self.assertEqual(summary.deadline_mismatch_pairs, 1)
        self.assertEqual(summary.full_deadline_misses, 1)

    def test_both_deadline_misses_are_not_scored_as_useful_contribution(self):
        pair = AblationPair(
            "model",
            outcome(variant="FULL", utility="1", cost="0.20", elapsed=130,
                    components=("base", "model")),
            outcome(variant="ABLATED", utility="0.4", cost="0", elapsed=120,
                    components=("base",)),
        )
        summary = summarize_ablation("model", [pair])
        self.assertEqual(summary.status, "INCONCLUSIVE")
        self.assertEqual(summary.comparable_pairs, 0)
        self.assertEqual(summary.deadline_mismatch_pairs, 0)
        self.assertEqual(summary.both_deadline_miss_pairs, 1)
        self.assertIsNone(summary.mean_utility_delta)

    def test_duplicate_matched_case_cannot_be_double_counted(self):
        pair = AblationPair(
            "agent",
            outcome(variant="FULL", utility="0.8", cost="0.1", elapsed=50,
                    components=("base", "agent")),
            outcome(variant="ABLATED", utility="0.5", cost="0", elapsed=40,
                    components=("base",)),
        )
        with self.assertRaisesRegex(ValueError, "duplicate matched ablation case"):
            summarize_ablation("agent", [pair, pair])

    def test_syndicated_duplicates_require_canonical_deduplication(self):
        with self.assertRaisesRegex(ValueError, "deduplicated"):
            outcome(
                variant="FULL",
                utility=1,
                cost=1,
                elapsed=10,
                components=("wire-story-group-7", "wire-story-group-7"),
            )

    def test_negative_cost_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-negative"):
            outcome(
                variant="FULL",
                utility=1,
                cost="-0.01",
                elapsed=10,
                components=("base",),
            )


if __name__ == "__main__":
    unittest.main()
