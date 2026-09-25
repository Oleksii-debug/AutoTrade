from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, localcontext
import unittest

from autotrade_research.evaluation.ablation import (
    AblationOutcome,
    AblationPair,
    evaluate_incremental_value,
    summarize_ablation,
)


CUT = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)


class _NoOffsetTZ(tzinfo):
    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None


def outcome(
    *,
    variant,
    utility,
    cost,
    elapsed,
    components,
    fingerprint="same",
    case_id="case-1",
    decision=None,
    cutoff=CUT,
    outcome_available=None,
):
    return AblationOutcome(
        case_id=case_id,
        input_fingerprint=fingerprint,
        variant=variant,
        utility=Decimal(str(utility)),
        cost=Decimal(str(cost)),
        elapsed_ms=elapsed,
        deadline_ms=100,
        components=tuple(components),
        input_cutoff_utc=cutoff,
        decision_utc=cutoff if decision is None else decision,
        outcome_available_utc=(
            cutoff + timedelta(hours=1)
            if outcome_available is None
            else outcome_available
        ),
    )


def pair(case_id, full_utility, ablated_utility="0", *, full_cost="0", ablated_cost="0"):
    return AblationPair(
        "agent",
        outcome(
            case_id=case_id,
            variant="FULL",
            utility=full_utility,
            cost=full_cost,
            elapsed=50,
            components=("base", "agent"),
        ),
        outcome(
            case_id=case_id,
            variant="ABLATED",
            utility=ablated_utility,
            cost=ablated_cost,
            elapsed=50,
            components=("base",),
        ),
    )


class AblationTests(unittest.TestCase):
    def test_matched_pair_reports_descriptive_value_cost_and_latency(self):
        matched = AblationPair(
            "news-source",
            outcome(variant="FULL", utility="0.8", cost="0.12", elapsed=80,
                    components=("base", "news-source")),
            outcome(variant="ABLATED", utility="0.5", cost="0.02", elapsed=50,
                    components=("base",)),
        )
        summary = summarize_ablation("news-source", [matched])
        self.assertEqual(summary.status, "DESCRIPTIVE_ONLY")
        self.assertEqual(summary.mean_utility_delta, Decimal("0.3"))
        self.assertEqual(summary.mean_cost_delta, Decimal("0.10"))
        self.assertEqual(summary.mean_latency_delta_ms, Decimal("30"))
        self.assertEqual(matched.net_value_delta, Decimal("0.20"))

    def test_input_fingerprint_must_match(self):
        with self.assertRaisesRegex(ValueError, "input_fingerprint"):
            AblationPair(
                "agent",
                outcome(variant="FULL", utility=1, cost=1, elapsed=10,
                        components=("base", "agent"), fingerprint="a"),
                outcome(variant="ABLATED", utility=1, cost=0, elapsed=10,
                        components=("base",), fingerprint="b"),
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
        matched = AblationPair(
            "model",
            outcome(variant="FULL", utility=1, cost="0.20", elapsed=120,
                    components=("base", "model")),
            outcome(variant="ABLATED", utility="0.4", cost="0", elapsed=50,
                    components=("base",)),
        )
        summary = summarize_ablation("model", [matched])
        self.assertEqual(summary.status, "INCONCLUSIVE")
        self.assertIsNone(summary.mean_utility_delta)
        self.assertEqual(summary.deadline_mismatch_pairs, 1)
        self.assertEqual(summary.full_deadline_misses, 1)

    def test_both_deadline_misses_are_not_scored_as_useful_contribution(self):
        matched = AblationPair(
            "model",
            outcome(variant="FULL", utility="1", cost="0.20", elapsed=130,
                    components=("base", "model")),
            outcome(variant="ABLATED", utility="0.4", cost="0", elapsed=120,
                    components=("base",)),
        )
        summary = summarize_ablation("model", [matched])
        self.assertEqual(summary.status, "INCONCLUSIVE")
        self.assertEqual(summary.comparable_pairs, 0)
        self.assertEqual(summary.deadline_mismatch_pairs, 0)
        self.assertEqual(summary.both_deadline_miss_pairs, 1)
        self.assertIsNone(summary.mean_utility_delta)

    def test_duplicate_matched_case_cannot_be_double_counted(self):
        matched = pair("case-1", "0.8", "0.5", full_cost="0.1")
        with self.assertRaisesRegex(ValueError, "duplicate matched ablation case"):
            summarize_ablation("agent", [matched, matched])

    def test_syndicated_duplicates_require_canonical_deduplication(self):
        with self.assertRaisesRegex(ValueError, "deduplicated"):
            outcome(
                variant="FULL",
                utility=1,
                cost=1,
                elapsed=10,
                components=("wire-story-group-7", "wire-story-group-7"),
            )

    def test_component_identity_whitespace_cannot_bypass_deduplication(self):
        with self.assertRaisesRegex(ValueError, "deduplicated canonical identities"):
            outcome(
                variant="FULL",
                utility=1,
                cost=1,
                elapsed=10,
                components=("base", "agent", " agent "),
            )

    def test_target_and_case_identity_are_canonicalized_before_matching(self):
        matched = AblationPair(
            " agent ",
            outcome(
                case_id=" case-1 ",
                fingerprint=" same-input ",
                variant="FULL",
                utility="0.2",
                cost="0.01",
                elapsed=10,
                components=("base", " agent "),
            ),
            outcome(
                case_id="case-1",
                fingerprint="same-input",
                variant="ABLATED",
                utility="0.1",
                cost="0",
                elapsed=10,
                components=("base",),
            ),
        )
        summary = summarize_ablation("agent", [matched])
        self.assertEqual(matched.target_component, "agent")
        self.assertEqual(matched.full.case_id, "case-1")
        self.assertEqual(matched.full.input_fingerprint, "same-input")
        self.assertEqual(matched.full.components, ("base", "agent"))
        self.assertEqual(summary.total_pairs, 1)

    def test_components_must_be_immutable_tuple(self):
        mutable = ["base", "agent"]
        with self.assertRaisesRegex(TypeError, "immutable tuple"):
            AblationOutcome(
                case_id="case-mutable-components",
                input_fingerprint="same",
                variant="FULL",
                utility=Decimal("0.1"),
                cost=Decimal("0.01"),
                elapsed_ms=10,
                deadline_ms=100,
                components=mutable,
                input_cutoff_utc=CUT,
                decision_utc=CUT,
                outcome_available_utc=CUT + timedelta(hours=1),
            )

    def test_elapsed_and_deadline_reject_boolean_pseudo_integers(self):
        with self.assertRaisesRegex(TypeError, "must be integers"):
            AblationOutcome(
                case_id="case-bool-elapsed",
                input_fingerprint="same",
                variant="FULL",
                utility=Decimal("0.1"),
                cost=Decimal("0.01"),
                elapsed_ms=True,
                deadline_ms=100,
                components=("base",),
                input_cutoff_utc=CUT,
                decision_utc=CUT,
                outcome_available_utc=CUT + timedelta(hours=1),
            )
        with self.assertRaisesRegex(TypeError, "must be integers"):
            AblationOutcome(
                case_id="case-bool-deadline",
                input_fingerprint="same",
                variant="FULL",
                utility=Decimal("0.1"),
                cost=Decimal("0.01"),
                elapsed_ms=10,
                deadline_ms=True,
                components=("base",),
                input_cutoff_utc=CUT,
                decision_utc=CUT,
                outcome_available_utc=CUT + timedelta(hours=1),
            )

    def test_case_identity_must_be_explicit_string(self):
        with self.assertRaisesRegex(ValueError, "case_id"):
            AblationOutcome(
                case_id=1,
                input_fingerprint="same",
                variant="FULL",
                utility=Decimal("0.1"),
                cost=Decimal("0.01"),
                elapsed_ms=10,
                deadline_ms=100,
                components=("base",),
                input_cutoff_utc=CUT,
                decision_utc=CUT,
                outcome_available_utc=CUT + timedelta(hours=1),
            )

    def test_binary_float_utility_and_cost_are_rejected(self):
        with self.assertRaises(TypeError):
            AblationOutcome(
                case_id="case-float-utility",
                input_fingerprint="same",
                variant="FULL",
                utility=0.1,
                cost=Decimal("0.01"),
                elapsed_ms=10,
                deadline_ms=100,
                components=("base",),
                input_cutoff_utc=CUT,
                decision_utc=CUT,
                outcome_available_utc=CUT + timedelta(hours=1),
            )
        with self.assertRaises(TypeError):
            AblationOutcome(
                case_id="case-float-cost",
                input_fingerprint="same",
                variant="FULL",
                utility=Decimal("0.1"),
                cost=0.01,
                elapsed_ms=10,
                deadline_ms=100,
                components=("base",),
                input_cutoff_utc=CUT,
                decision_utc=CUT,
                outcome_available_utc=CUT + timedelta(hours=1),
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

    def test_causal_timestamp_requires_real_utc_offset(self):
        invalid = datetime(2026, 9, 25, 0, 0, tzinfo=_NoOffsetTZ())
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            outcome(
                variant="FULL",
                utility=1,
                cost=0,
                elapsed=10,
                components=("base",),
                cutoff=invalid,
            )

    def test_future_leakage_is_rejected_when_cutoff_is_after_decision(self):
        with self.assertRaisesRegex(ValueError, "cannot precede"):
            outcome(
                variant="FULL",
                utility=1,
                cost=0,
                elapsed=10,
                components=("base",),
                decision=CUT - timedelta(seconds=1),
            )

    def test_decision_after_frozen_input_cutoff_is_causally_valid(self):
        item = outcome(
            variant="FULL",
            utility=1,
            cost=0,
            elapsed=10,
            components=("base",),
            decision=CUT + timedelta(seconds=1),
        )
        self.assertGreater(item.decision_utc, item.input_cutoff_utc)

    def test_outcome_must_be_after_input_cutoff(self):
        with self.assertRaisesRegex(ValueError, "strictly after"):
            outcome(
                variant="FULL",
                utility=1,
                cost=0,
                elapsed=10,
                components=("base",),
                outcome_available=CUT,
            )

    def test_decision_cannot_happen_after_outcome_is_available(self):
        with self.assertRaisesRegex(
            ValueError,
            "strictly before outcome availability",
        ):
            outcome(
                variant="FULL",
                utility=1,
                cost=0,
                elapsed=10,
                components=("base",),
                decision=CUT + timedelta(hours=2),
                outcome_available=CUT + timedelta(hours=1),
            )

    def test_matched_pair_requires_same_causal_cutoff(self):
        with self.assertRaisesRegex(ValueError, "causal input cutoff"):
            AblationPair(
                "agent",
                outcome(
                    variant="FULL", utility=1, cost=0, elapsed=10,
                    components=("base", "agent"),
                ),
                outcome(
                    variant="ABLATED", utility=0, cost=0, elapsed=10,
                    components=("base",),
                    cutoff=CUT - timedelta(seconds=1),
                ),
            )

    def test_insufficient_comparable_pairs_are_inconclusive(self):
        result = evaluate_incremental_value(
            "agent",
            [pair("p1", "1")],
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertIsNone(result.lower_bound)

    def test_incremental_value_is_net_of_cost_with_lower_bound(self):
        result = evaluate_incremental_value(
            "agent",
            [
                pair("p1", "2", full_cost="0.5"),
                pair("p2", "2", full_cost="0.5"),
            ],
            minimum_pairs=2,
            required_lower_bound=Decimal("1.5"),
        )
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.mean_net_incremental_value, Decimal("1.5"))
        self.assertEqual(result.lower_bound, Decimal("1.5"))

    def test_scientific_aggregation_is_independent_of_ambient_decimal_context(self):
        pairs = [
            pair("ctx-1", "1.234567890123456789", full_cost="0.111111111111111111"),
            pair("ctx-2", "2.345678901234567891", full_cost="0.222222222222222222"),
            pair("ctx-3", "0.987654321987654321", full_cost="0.033333333333333333"),
        ]
        with localcontext() as context:
            context.prec = 7
            low_precision = evaluate_incremental_value(
                "agent",
                pairs,
                minimum_pairs=3,
                required_lower_bound=Decimal("0"),
            )
            low_summary = summarize_ablation("agent", pairs)
        with localcontext() as context:
            context.prec = 34
            high_precision = evaluate_incremental_value(
                "agent",
                pairs,
                minimum_pairs=3,
                required_lower_bound=Decimal("0"),
            )
            high_summary = summarize_ablation("agent", pairs)

        self.assertEqual(low_precision, high_precision)
        self.assertEqual(low_summary, high_summary)

    def test_uncertain_mixed_result_fails_lower_bound(self):
        result = evaluate_incremental_value(
            "agent",
            [
                pair("p1", "3"),
                pair("p2", "-1"),
                pair("p3", "2"),
            ],
            minimum_pairs=3,
            required_lower_bound=Decimal("0.5"),
        )
        self.assertEqual(result.status, "FAIL")
        self.assertLess(result.lower_bound, Decimal("0.5"))


if __name__ == "__main__":
    unittest.main()
