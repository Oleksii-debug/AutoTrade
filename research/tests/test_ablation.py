from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, localcontext
from hashlib import sha256
import json
import unittest

from autotrade_research.evaluation.ablation import (
    AblationOutcome,
    AblationPair,
    CanonicalAblationOutcomeEvidence,
    CausalInputEvidence,
    RegisteredAblationPopulation,
    build_ablation_evidence_bundle,
    evaluate_incremental_value,
    evaluate_qualified_incremental_value,
    summarize_ablation,
    verify_ablation_evidence_bundle,
)


CUT = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
FINGERPRINT_A = "sha256:" + ("a" * 64)
FINGERPRINT_B = "sha256:" + ("b" * 64)
FINGERPRINT_C = "sha256:" + ("c" * 64)
FINGERPRINT_D = "sha256:" + ("d" * 64)
_DEFAULT_INPUT_EVIDENCE = object()


class _NoOffsetTZ(tzinfo):
    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None


def causal_evidence(
    *,
    evidence_id="base-input",
    digest=FINGERPRINT_B,
    component="base",
    available=CUT,
    syndication_group=None,
):
    return CausalInputEvidence(
        evidence_id=evidence_id,
        content_digest=digest,
        component_id=component,
        available_utc=available,
        syndication_group=syndication_group,
    )


def outcome(
    *,
    variant,
    utility,
    cost,
    elapsed,
    components,
    fingerprint=FINGERPRINT_A,
    case_id="case-1",
    decision=None,
    cutoff=CUT,
    outcome_available=None,
    population_unit=None,
    input_evidence=_DEFAULT_INPUT_EVIDENCE,
):
    evidence_items = (
        (causal_evidence(available=cutoff),)
        if input_evidence is _DEFAULT_INPUT_EVIDENCE
        else input_evidence
    )
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
        population_unit_id=population_unit,
        input_evidence=evidence_items,
    )


def pair(
    case_id,
    full_utility,
    ablated_utility="0",
    *,
    full_cost="0",
    ablated_cost="0",
    population_unit=None,
    fingerprint=None,
    full_decision=None,
    ablated_decision=None,
):
    case_fingerprint = (
        "sha256:" + sha256(case_id.encode("utf-8")).hexdigest()
        if fingerprint is None
        else fingerprint
    )
    return AblationPair(
        "agent",
        outcome(
            case_id=case_id,
            fingerprint=case_fingerprint,
            variant="FULL",
            utility=full_utility,
            cost=full_cost,
            elapsed=50,
            components=("base", "agent"),
            population_unit=population_unit,
            decision=full_decision,
        ),
        outcome(
            case_id=case_id,
            fingerprint=case_fingerprint,
            variant="ABLATED",
            utility=ablated_utility,
            cost=ablated_cost,
            elapsed=50,
            components=("base",),
            population_unit=population_unit,
            decision=ablated_decision,
        ),
    )


def canonical_evidence(matched, *, source_revision="9" * 40, superseded_at=None):
    return tuple(
        CanonicalAblationOutcomeEvidence(
            case_id=item.case_id,
            variant=item.variant,
            population_unit_id=item.population_unit_id,
            utility=item.utility,
            cost=item.cost,
            outcome_available_utc=item.outcome_available_utc,
            source_revision=source_revision,
            utility_evidence_digest=FINGERPRINT_B,
            cost_evidence_digest=FINGERPRINT_C,
            evidence_digest=(
                "sha256:"
                + sha256(
                    f"{item.case_id}:{item.variant}:{item.utility}:{item.cost}".encode()
                ).hexdigest()
            ),
            superseded_at_utc=superseded_at,
        )
        for item in (matched.full, matched.ablated)
    )


def registered_population(pairs, *, source_revision="9" * 40, registered_at=None, evaluation_cutoff=None, complete=True, extra_units=()):
    units = tuple(
        sorted(
            {pair.full.population_unit_id for pair in pairs}
            | set(extra_units)
        )
    )
    return RegisteredAblationPopulation(
        protocol_digest=FINGERPRINT_A,
        population_digest=FINGERPRINT_D,
        stopping_rule_digest=FINGERPRINT_C,
        source_revision=source_revision,
        registered_at_utc=(
            CUT - timedelta(days=1) if registered_at is None else registered_at
        ),
        evaluation_cutoff_utc=(
            CUT + timedelta(hours=2)
            if evaluation_cutoff is None
            else evaluation_cutoff
        ),
        population_unit_ids=units,
        complete=complete,
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
                        components=("base", "agent"), fingerprint=FINGERPRINT_A),
                outcome(variant="ABLATED", utility=1, cost=0, elapsed=10,
                        components=("base",), fingerprint=FINGERPRINT_B),
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

    def test_case_id_cannot_be_reused_with_changed_input_fingerprint(self):
        first = pair("case-reused", "0.8", fingerprint=FINGERPRINT_A)
        second = pair("case-reused", "0.9", fingerprint=FINGERPRINT_B)
        with self.assertRaisesRegex(ValueError, "duplicate matched ablation case_id"):
            summarize_ablation("agent", [first, second])

    def test_identical_input_cannot_be_recounted_under_case_alias(self):
        first = pair("case-a", "0.8", fingerprint=FINGERPRINT_A)
        second = pair("case-b", "0.9", fingerprint=FINGERPRINT_A)
        with self.assertRaisesRegex(
            ValueError,
            "duplicate matched ablation input_fingerprint",
        ):
            summarize_ablation("agent", [first, second])

    def test_matched_pair_requires_same_decision_time(self):
        with self.assertRaisesRegex(ValueError, "exact decision time"):
            pair(
                "decision-mismatch",
                "0.8",
                full_decision=CUT,
                ablated_decision=CUT + timedelta(milliseconds=1),
            )

    def test_syndicated_duplicates_require_canonical_deduplication(self):
        with self.assertRaisesRegex(ValueError, "deduplicated"):
            outcome(
                variant="FULL",
                utility=1,
                cost=1,
                elapsed=10,
                components=("wire-story-group-7", "wire-story-group-7"),
            )

    def test_causal_input_after_cutoff_is_rejected(self):
        late = causal_evidence(
            evidence_id="late-story",
            digest=FINGERPRINT_C,
            component="news-source",
            available=CUT + timedelta(microseconds=1),
        )
        with self.assertRaisesRegex(ValueError, "after the causal cutoff"):
            outcome(
                variant="FULL",
                utility=1,
                cost=0,
                elapsed=10,
                components=("base", "news-source"),
                input_evidence=(late,),
            )

    def test_source_ablation_may_remove_only_target_owned_input_evidence(self):
        base = causal_evidence()
        story = causal_evidence(
            evidence_id="wire-story-7",
            digest=FINGERPRINT_C,
            component="news-source",
            syndication_group="wire-story-group-7",
        )
        matched = AblationPair(
            "news-source",
            outcome(
                variant="FULL",
                utility="0.8",
                cost="0.1",
                elapsed=40,
                components=("base", "news-source"),
                input_evidence=(base, story),
            ),
            outcome(
                variant="ABLATED",
                utility="0.4",
                cost="0",
                elapsed=35,
                components=("base",),
                input_evidence=(base,),
            ),
        )
        self.assertEqual(
            tuple(item.evidence_id for item in matched.full.input_evidence),
            ("base-input", "wire-story-7"),
        )

    def test_pair_rejects_removal_of_non_target_input_evidence(self):
        base = causal_evidence()
        unrelated = causal_evidence(
            evidence_id="macro-release",
            digest=FINGERPRINT_C,
            component="macro-source",
        )
        with self.assertRaisesRegex(ValueError, "target-component evidence"):
            AblationPair(
                "agent",
                outcome(
                    variant="FULL",
                    utility=1,
                    cost=0,
                    elapsed=10,
                    components=("base", "agent"),
                    input_evidence=(base, unrelated),
                ),
                outcome(
                    variant="ABLATED",
                    utility=0,
                    cost=0,
                    elapsed=10,
                    components=("base",),
                    input_evidence=(base,),
                ),
            )

    def test_syndicated_input_groups_are_deduplicated_within_case(self):
        first = causal_evidence(
            evidence_id="wire-a",
            digest=FINGERPRINT_C,
            component="news-source",
            syndication_group="wire-story-group-7",
        )
        second = causal_evidence(
            evidence_id="wire-b",
            digest=FINGERPRINT_D,
            component="news-source",
            syndication_group="wire-story-group-7",
        )
        with self.assertRaisesRegex(ValueError, "syndicated input groups"):
            outcome(
                variant="FULL",
                utility=1,
                cost=0,
                elapsed=10,
                components=("base", "news-source"),
                input_evidence=(first, second),
            )

    def test_syndicated_cases_cannot_be_double_counted_as_independent_samples(self):
        first = pair(
            "case-wire-a",
            "1",
            population_unit="canonical-wire-story-7",
        )
        second = pair(
            "case-wire-b",
            "1",
            population_unit="canonical-wire-story-7",
        )
        with self.assertRaisesRegex(ValueError, "independent population unit"):
            summarize_ablation("agent", [first, second])

    def test_target_content_cannot_be_recounted_across_distinct_cases(self):
        target = causal_evidence(
            evidence_id="wire-story-a",
            digest=FINGERPRINT_C,
            component="agent",
        )
        first = pair("target-dup-a", "1")
        second = pair("target-dup-b", "1")
        first = AblationPair(
            "agent",
            replace(
                first.full,
                input_evidence=(causal_evidence(), target),
            ),
            first.ablated,
        )
        second_base = causal_evidence(
            evidence_id="base-b",
            digest="sha256:" + ("e" * 64),
        )
        second = AblationPair(
            "agent",
            replace(
                second.full,
                input_evidence=(
                    second_base,
                    replace(target, evidence_id="wire-story-b"),
                ),
            ),
            replace(second.ablated, input_evidence=(second_base,)),
        )
        with self.assertRaisesRegex(ValueError, "duplicate target evidence content"):
            evaluate_incremental_value(
                "agent",
                [first, second],
                minimum_pairs=2,
                required_lower_bound=Decimal("0"),
            )

    def test_target_syndication_group_cannot_be_recounted_across_cases(self):
        first_target = causal_evidence(
            evidence_id="wire-story-a",
            digest=FINGERPRINT_C,
            component="agent",
            syndication_group="wire-group-9",
        )
        second_target = causal_evidence(
            evidence_id="wire-story-b",
            digest=FINGERPRINT_D,
            component="agent",
            syndication_group="wire-group-9",
        )
        first = pair("syndicated-a", "1")
        second = pair("syndicated-b", "1")
        first = AblationPair(
            "agent",
            replace(first.full, input_evidence=(causal_evidence(), first_target)),
            first.ablated,
        )
        second_base = causal_evidence(
            evidence_id="base-b",
            digest="sha256:" + ("e" * 64),
        )
        second = AblationPair(
            "agent",
            replace(second.full, input_evidence=(second_base, second_target)),
            replace(second.ablated, input_evidence=(second_base,)),
        )
        with self.assertRaisesRegex(ValueError, "duplicate target syndication group"):
            summarize_ablation("agent", [first, second])

    def test_inferential_value_requires_causal_input_evidence(self):
        unbound = AblationPair(
            "agent",
            outcome(
                variant="FULL",
                utility=1,
                cost=0,
                elapsed=10,
                components=("base", "agent"),
                input_evidence=(),
            ),
            outcome(
                variant="ABLATED",
                utility=0,
                cost=0,
                elapsed=10,
                components=("base",),
                input_evidence=(),
            ),
        )
        result = evaluate_incremental_value(
            "agent",
            [unbound],
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(result.reason, "missing_causal_input_evidence")

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
                fingerprint=FINGERPRINT_A,
                variant="FULL",
                utility="0.2",
                cost="0.01",
                elapsed=10,
                components=("base", " agent "),
            ),
            outcome(
                case_id="case-1",
                fingerprint=FINGERPRINT_A,
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
        self.assertEqual(matched.full.input_fingerprint, FINGERPRINT_A)
        self.assertEqual(matched.full.components, ("base", "agent"))
        self.assertEqual(summary.total_pairs, 1)

    def test_components_must_be_immutable_tuple(self):
        mutable = ["base", "agent"]
        with self.assertRaisesRegex(TypeError, "immutable tuple"):
            AblationOutcome(
                case_id="case-mutable-components",
                input_fingerprint=FINGERPRINT_A,
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
                input_fingerprint=FINGERPRINT_A,
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
                input_fingerprint=FINGERPRINT_A,
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
                input_fingerprint=FINGERPRINT_A,
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
                input_fingerprint=FINGERPRINT_A,
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
                input_fingerprint=FINGERPRINT_A,
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


    def test_locked_evidence_bundle_is_order_stable_and_self_verifying(self):
        cases = [
            pair("locked-a", "2", full_cost="0.25"),
            pair("locked-b", "1.5", full_cost="0.10"),
        ]
        first = build_ablation_evidence_bundle(
            "agent",
            cases,
            source_revision="1" * 40,
            protocol_digest=FINGERPRINT_C,
            dataset_digest=FINGERPRINT_D,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        second = build_ablation_evidence_bundle(
            "agent",
            list(reversed(cases)),
            source_revision="1" * 40,
            protocol_digest=FINGERPRINT_C,
            dataset_digest=FINGERPRINT_D,
            minimum_pairs=2,
            required_lower_bound=Decimal("0.000"),
        )
        self.assertEqual(first.payload, second.payload)
        self.assertEqual(first.content_digest, second.content_digest)
        self.assertTrue(verify_ablation_evidence_bundle(first, cases))

    def test_locked_evidence_digest_changes_when_causal_result_changes(self):
        baseline = [
            pair("locked-a", "2", full_cost="0.25"),
            pair("locked-b", "1.5", full_cost="0.10"),
        ]
        changed = [
            pair("locked-a", "2.1", full_cost="0.25"),
            pair("locked-b", "1.5", full_cost="0.10"),
        ]
        first = build_ablation_evidence_bundle(
            "agent",
            baseline,
            source_revision="2" * 40,
            protocol_digest=FINGERPRINT_C,
            dataset_digest=FINGERPRINT_D,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        second = build_ablation_evidence_bundle(
            "agent",
            changed,
            source_revision="2" * 40,
            protocol_digest=FINGERPRINT_C,
            dataset_digest=FINGERPRINT_D,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        self.assertNotEqual(first.content_digest, second.content_digest)
        with self.assertRaisesRegex(ValueError, "locked ablation evidence"):
            verify_ablation_evidence_bundle(first, changed)

    def test_locked_evidence_binds_source_protocol_and_dataset_identity(self):
        cases = [
            pair("identity-a", "1"),
            pair("identity-b", "1"),
        ]
        base = build_ablation_evidence_bundle(
            "agent",
            cases,
            source_revision="3" * 40,
            protocol_digest=FINGERPRINT_C,
            dataset_digest=FINGERPRINT_D,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        changed_source = build_ablation_evidence_bundle(
            "agent",
            cases,
            source_revision="4" * 40,
            protocol_digest=FINGERPRINT_C,
            dataset_digest=FINGERPRINT_D,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        changed_protocol = build_ablation_evidence_bundle(
            "agent",
            cases,
            source_revision="3" * 40,
            protocol_digest=FINGERPRINT_B,
            dataset_digest=FINGERPRINT_D,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        self.assertNotEqual(base.content_digest, changed_source.content_digest)
        self.assertNotEqual(base.content_digest, changed_protocol.content_digest)

    def test_locked_evidence_preserves_inconclusive_negative_result(self):
        locked = build_ablation_evidence_bundle(
            "agent",
            [pair("only-one", "1")],
            source_revision="5" * 40,
            protocol_digest=FINGERPRINT_C,
            dataset_digest=FINGERPRINT_D,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        self.assertEqual(locked.pair_count, 1)
        self.assertEqual(locked.evaluation.status, "INCONCLUSIVE")
        self.assertEqual(
            locked.evaluation.reason,
            "insufficient_comparable_matched_pairs",
        )
        self.assertIn('"status":"INCONCLUSIVE"', locked.payload)

    def test_locked_evidence_rejects_non_exact_source_revision(self):
        with self.assertRaisesRegex(ValueError, "exact 40-character lowercase git SHA"):
            build_ablation_evidence_bundle(
                "agent",
                [pair("bad-source-a", "1"), pair("bad-source-b", "1")],
                source_revision="main",
                protocol_digest=FINGERPRINT_C,
                dataset_digest=FINGERPRINT_D,
                minimum_pairs=2,
                required_lower_bound=Decimal("0"),
            )


    def test_locked_evidence_constructor_rejects_payload_evaluation_mismatch(self):
        cases = [
            pair("constructor-a", "2", full_cost="0.25"),
            pair("constructor-b", "1.5", full_cost="0.10"),
        ]
        locked = build_ablation_evidence_bundle(
            "agent",
            cases,
            source_revision="7" * 40,
            protocol_digest=FINGERPRINT_C,
            dataset_digest=FINGERPRINT_D,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        decoded = json.loads(locked.payload)
        decoded["evaluation"]["reason"] = "tampered_reason"
        tampered_payload = json.dumps(
            decoded,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        tampered_digest = "sha256:" + sha256(
            tampered_payload.encode("utf-8")
        ).hexdigest()

        with self.assertRaisesRegex(
            ValueError,
            "payload evaluation does not match bundle evaluation",
        ):
            replace(
                locked,
                payload=tampered_payload,
                content_digest=tampered_digest,
            )

    def test_locked_evidence_constructor_rejects_noncanonical_json(self):
        cases = [
            pair("canonical-a", "2", full_cost="0.25"),
            pair("canonical-b", "1.5", full_cost="0.10"),
        ]
        locked = build_ablation_evidence_bundle(
            "agent",
            cases,
            source_revision="8" * 40,
            protocol_digest=FINGERPRINT_C,
            dataset_digest=FINGERPRINT_D,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        noncanonical_payload = json.dumps(
            json.loads(locked.payload),
            indent=2,
            ensure_ascii=False,
        )
        noncanonical_digest = "sha256:" + sha256(
            noncanonical_payload.encode("utf-8")
        ).hexdigest()

        with self.assertRaisesRegex(
            ValueError,
            "payload must use canonical JSON serialization",
        ):
            replace(
                locked,
                payload=noncanonical_payload,
                content_digest=noncanonical_digest,
            )


    def test_qualified_pass_requires_canonical_outcome_evidence(self):
        cases = [
            pair("qualified-a", "2", population_unit="unit-a"),
            pair("qualified-b", "2", population_unit="unit-b"),
        ]
        result = evaluate_qualified_incremental_value(
            "agent",
            cases,
            population=registered_population(cases),
            canonical_outcomes=(),
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(result.reason, "missing_canonical_outcome_evidence")

    def test_qualified_population_must_include_every_registered_unit(self):
        cases = [
            pair("qualified-a", "2", population_unit="unit-a"),
            pair("qualified-b", "2", population_unit="unit-b"),
        ]
        evidence = tuple(
            item
            for matched in cases
            for item in canonical_evidence(matched)
        )
        result = evaluate_qualified_incremental_value(
            "agent",
            cases,
            population=registered_population(cases, extra_units=("unit-negative-null",)),
            canonical_outcomes=evidence,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(result.reason, "incomplete_registered_population")

    def test_qualified_stale_pre_cutoff_revision_is_inconclusive(self):
        cases = [
            pair("qualified-a", "2", population_unit="unit-a"),
            pair("qualified-b", "2", population_unit="unit-b"),
        ]
        stale = canonical_evidence(
            cases[0],
            superseded_at=CUT + timedelta(hours=1, minutes=30),
        )
        evidence = stale + canonical_evidence(cases[1])
        result = evaluate_qualified_incremental_value(
            "agent",
            cases,
            population=registered_population(cases),
            canonical_outcomes=evidence,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(result.reason, "stale_canonical_outcome_revision")

    def test_qualified_post_cutoff_correction_does_not_rewrite_frozen_result(self):
        cases = [
            pair("qualified-a", "2", population_unit="unit-a"),
            pair("qualified-b", "2", population_unit="unit-b"),
        ]
        evidence = (
            canonical_evidence(
                cases[0],
                superseded_at=CUT + timedelta(hours=3),
            )
            + canonical_evidence(cases[1])
        )
        result = evaluate_qualified_incremental_value(
            "agent",
            cases,
            population=registered_population(cases),
            canonical_outcomes=evidence,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            result.reason,
            "qualified_registered_canonical_ablation_net_of_cost",
        )

    def test_qualified_post_hoc_registration_cannot_pass(self):
        cases = [
            pair("qualified-a", "2", population_unit="unit-a"),
            pair("qualified-b", "2", population_unit="unit-b"),
        ]
        evidence = tuple(
            item
            for matched in cases
            for item in canonical_evidence(matched)
        )
        result = evaluate_qualified_incremental_value(
            "agent",
            cases,
            population=registered_population(
                cases,
                registered_at=CUT + timedelta(seconds=1),
            ),
            canonical_outcomes=evidence,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(
            result.reason,
            "post_hoc_population_or_protocol_registration",
        )

    def test_qualified_evaluation_is_deterministic_for_exact_registered_population(self):
        cases = [
            pair("qualified-a", "2", full_cost="0.25", population_unit="unit-a"),
            pair("qualified-b", "1.5", full_cost="0.10", population_unit="unit-b"),
        ]
        evidence = tuple(
            item
            for matched in cases
            for item in canonical_evidence(matched)
        )
        population = registered_population(cases)
        first = evaluate_qualified_incremental_value(
            "agent",
            cases,
            population=population,
            canonical_outcomes=evidence,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        second = evaluate_qualified_incremental_value(
            "agent",
            list(reversed(cases)),
            population=population,
            canonical_outcomes=tuple(reversed(evidence)),
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        self.assertEqual(first, second)
        self.assertEqual(first.status, "PASS")

    def test_qualified_canonical_economics_must_match_scored_numbers(self):
        cases = [
            pair("qualified-a", "2", population_unit="unit-a"),
            pair("qualified-b", "2", population_unit="unit-b"),
        ]
        evidence = list(
            item
            for matched in cases
            for item in canonical_evidence(matched)
        )
        evidence[0] = replace(evidence[0], utility=Decimal("999"))
        result = evaluate_qualified_incremental_value(
            "agent",
            cases,
            population=registered_population(cases),
            canonical_outcomes=evidence,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(result.reason, "canonical_outcome_economic_mismatch")


if __name__ == "__main__":
    unittest.main()
