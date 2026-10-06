from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import gc
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID
from weakref import ref as weakref_ref

import autotrade_research.evaluation.ablation as ablation_module
from autotrade_research.evaluation.ablation import (
    AblationOutcome,
    AblationPair,
    AblationOutcomeArtifactRef,
    AblationQualificationAuthority,
    CanonicalAblationOutcomeEvidence,
    CausalInputEvidence,
    RegisteredAblationPopulation,
    build_ablation_evidence_bundle,
    evaluate_incremental_value,
    evaluate_qualified_incremental_value,
    summarize_ablation,
    verify_ablation_evidence_bundle,
)
from autotrade_research.artifacts.store import ArtifactStore
from autotrade_research.memory.episodes import ExperienceMemory
from autotrade_research.science.registry import ProtocolViolation, ScientificRegistry


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
    cutoff=CUT,
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
            cutoff=cutoff,
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
            cutoff=cutoff,
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

    def test_hostile_text_subclass_is_rejected_before_identity_callbacks(self):
        calls = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                calls.append("strip")
                raise AssertionError("hostile strip executed")

        hostile = HostileText("agent")
        with self.assertRaisesRegex(ValueError, "target_component"):
            summarize_ablation(hostile, [])
        with self.assertRaisesRegex(ValueError, "case_id"):
            AblationOutcome(
                case_id=HostileText("case-hostile"),
                input_fingerprint=FINGERPRINT_A,
                variant="FULL",
                utility=Decimal("0.1"),
                cost=Decimal("0"),
                elapsed_ms=10,
                deadline_ms=100,
                components=("base",),
                input_cutoff_utc=CUT,
                decision_utc=CUT,
                outcome_available_utc=CUT + timedelta(hours=1),
            )
        with self.assertRaisesRegex(ValueError, "content_digest"):
            CausalInputEvidence(
                evidence_id="input",
                content_digest=HostileText(FINGERPRINT_A),
                component_id="base",
                available_utc=CUT,
            )
        self.assertEqual(calls, [])

    def test_qualification_evidence_and_authority_reject_hostile_subtypes_before_callbacks(self):
        calls = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                calls.append("strip")
                raise AssertionError("hostile strip executed")

            def endswith(self, *args, **kwargs):
                calls.append("endswith")
                raise AssertionError("hostile endswith executed")

        with self.assertRaisesRegex(ValueError, "case_id"):
            CanonicalAblationOutcomeEvidence(
                case_id=HostileText("case"),
                variant="FULL",
                population_unit_id="unit",
                utility=Decimal("1"),
                cost=Decimal("0"),
                outcome_available_utc=CUT + timedelta(hours=1),
                source_revision="9" * 40,
                utility_evidence_digest=FINGERPRINT_B,
                cost_evidence_digest=FINGERPRINT_C,
                evidence_digest=FINGERPRINT_D,
            )
        with self.assertRaisesRegex(ValueError, "population_unit_id"):
            RegisteredAblationPopulation(
                protocol_digest=FINGERPRINT_A,
                population_digest=FINGERPRINT_D,
                stopping_rule_digest=FINGERPRINT_C,
                source_revision="9" * 40,
                registered_at_utc=CUT - timedelta(days=1),
                evaluation_cutoff_utc=CUT + timedelta(hours=2),
                population_unit_ids=(HostileText("unit"),),
            )
        with self.assertRaisesRegex(ValueError, "case_id"):
            CanonicalAblationOutcomeEvidence(
                case_id=" case ",
                variant="FULL",
                population_unit_id="unit",
                utility=Decimal("1"),
                cost=Decimal("0"),
                outcome_available_utc=CUT + timedelta(hours=1),
                source_revision="9" * 40,
                utility_evidence_digest=FINGERPRINT_B,
                cost_evidence_digest=FINGERPRINT_C,
                evidence_digest=FINGERPRINT_D,
            )
        with self.assertRaisesRegex(ValueError, "population_unit_id"):
            RegisteredAblationPopulation(
                protocol_digest=FINGERPRINT_A,
                population_digest=FINGERPRINT_D,
                stopping_rule_digest=FINGERPRINT_C,
                source_revision="9" * 40,
                registered_at_utc=CUT - timedelta(days=1),
                evaluation_cutoff_utc=CUT + timedelta(hours=2),
                population_unit_ids=(" unit ",),
            )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            science = ScientificRegistry(root / "science.sqlite3")
            memory = ExperienceMemory(root / "memory.sqlite3")
            artifacts = ArtifactStore(root / "artifacts")

            class DerivedRegistry(ScientificRegistry):
                pass

            derived_registry = DerivedRegistry(root / "derived-science.sqlite3")
            with self.assertRaisesRegex(TypeError, "exact ScientificRegistry"):
                AblationQualificationAuthority(
                    scientific_registry=derived_registry,
                    experience_memory=memory,
                    artifact_store=artifacts,
                    protocol_id="protocol",
                    protocol_hash=FINGERPRINT_A,
                    source_revision="9" * 40,
                    causal_cutoff=CUT,
                    granted_permissions={"RESEARCH"},
                )
            with self.assertRaisesRegex(ValueError, "protocol_id"):
                AblationQualificationAuthority(
                    scientific_registry=science,
                    experience_memory=memory,
                    artifact_store=artifacts,
                    protocol_id=HostileText("protocol"),
                    protocol_hash=FINGERPRINT_A,
                    source_revision="9" * 40,
                    causal_cutoff=CUT,
                    granted_permissions={"RESEARCH"},
                )
        self.assertEqual(calls, [])

    def test_pair_rejects_noncanonical_outcome_type_before_field_access(self):
        calls = []

        class HostileOutcome:
            def __getattribute__(self, name):
                calls.append(name)
                raise AssertionError("hostile outcome field accessed")

        valid = outcome(
            variant="ABLATED",
            utility=0,
            cost=0,
            elapsed=10,
            components=("base",),
        )
        with self.assertRaisesRegex(TypeError, "exact AblationOutcome"):
            AblationPair("agent", HostileOutcome(), valid)
        self.assertEqual(calls, [])

    def test_input_evidence_requires_exact_causal_evidence_type(self):
        class DerivedEvidence(CausalInputEvidence):
            pass

        derived = DerivedEvidence(
            evidence_id="derived",
            content_digest=FINGERPRINT_B,
            component_id="base",
            available_utc=CUT,
        )
        with self.assertRaisesRegex(TypeError, "exact CausalInputEvidence"):
            outcome(
                variant="FULL",
                utility=1,
                cost=0,
                elapsed=10,
                components=("base",),
                input_evidence=(derived,),
            )

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

    def test_non_utc_offset_is_rejected_instead_of_normalized(self):
        shifted = datetime(
            2026,
            9,
            25,
            2,
            0,
            tzinfo=timezone(timedelta(hours=2)),
        )
        with self.assertRaisesRegex(ValueError, "canonical timezone-aware UTC"):
            outcome(
                variant="FULL",
                utility=1,
                cost=0,
                elapsed=10,
                components=("base",),
                cutoff=shifted,
                decision=CUT,
                outcome_available=CUT + timedelta(hours=1),
                input_evidence=(),
            )

    def test_datetime_subclass_is_rejected_before_virtual_time_callbacks(self):
        calls = []

        class HostileDatetime(datetime):
            def utcoffset(self):
                calls.append("utcoffset")
                raise AssertionError("hostile utcoffset executed")

            def astimezone(self, *args, **kwargs):
                calls.append("astimezone")
                raise AssertionError("hostile astimezone executed")

        hostile = HostileDatetime(
            2026,
            9,
            25,
            0,
            0,
            tzinfo=timezone.utc,
        )
        with self.assertRaisesRegex(TypeError, "exact built-in datetime"):
            outcome(
                variant="FULL",
                utility=1,
                cost=0,
                elapsed=10,
                components=("base",),
                cutoff=hostile,
                decision=CUT,
                outcome_available=CUT + timedelta(hours=1),
                input_evidence=(),
            )
        self.assertEqual(calls, [])

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

    def test_qualified_outcome_after_evaluation_cutoff_is_inconclusive(self):
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
                evaluation_cutoff=CUT + timedelta(minutes=30),
            ),
            canonical_outcomes=evidence,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(
            result.reason,
            "outcome_unavailable_at_evaluation_cutoff",
        )

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
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(
            result.reason,
            "untrusted_caller_authored_qualification_evidence",
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
        self.assertEqual(first.status, "INCONCLUSIVE")
        self.assertEqual(
            first.reason,
            "untrusted_caller_authored_qualification_evidence",
        )

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


    def test_policy_binding_releases_registry_with_dead_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            authority = AblationQualificationAuthority(
                scientific_registry=ScientificRegistry(root / "science.sqlite3"),
                experience_memory=ExperienceMemory(root / "memory.sqlite3"),
                artifact_store=ArtifactStore(root / "artifacts"),
                protocol_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                protocol_hash=FINGERPRINT_A,
                source_revision="9" * 40,
                causal_cutoff=CUT + timedelta(hours=1),
                granted_permissions={"RESEARCH"},
                task="ablation-qualification",
                instrument_family="EQUITY",
            )
            registry = authority.scientific_registry
            authority_reference = weakref_ref(authority)
            registry_reference = weakref_ref(registry)

            del registry
            del authority
            for _ in range(3):
                gc.collect()

            self.assertIsNone(authority_reference())
            self.assertIsNone(registry_reference())

    def test_terminal_interlock_validates_inputs_without_resolving_authority(self):
        authority = object.__new__(AblationQualificationAuthority)

        def forbidden_resolve(*args, **kwargs):
            raise AssertionError("persistent authority graph must not execute")

        authority.resolve = forbidden_resolve
        cases = [
            pair("interlock-a", "2", population_unit="interlock-unit-a"),
            pair("interlock-b", "2", population_unit="interlock-unit-b"),
        ]

        result = evaluate_qualified_incremental_value(
            "agent",
            cases,
            authority=authority,
            minimum_pairs=1,
            required_lower_bound=Decimal("-999"),
            uncertainty_multiplier=Decimal("0"),
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(
            result.reason,
            "registered_ablation_decision_policy_unavailable",
        )
        self.assertEqual(result.required_lower_bound, Decimal("0"))
        self.assertEqual(result.uncertainty_multiplier, Decimal("0"))

        with self.assertRaisesRegex(ValueError, "duplicate matched ablation case_id"):
            evaluate_qualified_incremental_value(
                "agent",
                [cases[0], cases[0]],
                authority=authority,
                minimum_pairs=2,
                required_lower_bound=Decimal("0"),
            )


    def test_terminal_policy_preflight_is_registered_and_never_reads_outcomes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            science = ScientificRegistry(root / "science.sqlite3")
            memory = ExperienceMemory(root / "memory.sqlite3")
            artifacts = ArtifactStore(root / "artifacts")
            cases = [
                pair("policy-a", "2", population_unit="policy-unit-a"),
                pair("policy-b", "2", population_unit="policy-unit-b"),
            ]

            base = {
                "hypothesis": "agent adds after-cost value",
                "strategy": "matched causal ablation",
                "features": ["base", "agent"],
                "search_space": {"agent": ["enabled", "ablated"]},
                "train_period": {"start": "2026-01-01", "end": "2026-01-02"},
                "validation_period": {"start": "2026-01-04", "end": "2026-01-05"},
                "test_period": {"start": "2026-01-07", "end": "2026-01-08"},
                "forward_period": {"start": "2026-01-10", "end": "2026-01-11"},
                "labels": ["net_value"],
                "horizons": ["1d"],
                "purge_embargo": {"purge": "1d", "embargo": "1d"},
                "universe": ["TEST"],
                "cost_fill_model": "canonical-cost-v1",
                "baselines": ["ablated"],
                "primary_metrics": ["net_incremental_value"],
                "secondary_metrics": ["latency"],
                "trial_budget": 2,
                "stopping_rules": {"maximum_trials": 2},
                "statistical_estimator": "matched-lower-bound",
                "multiplicity_treatment": "pre-registered-single-comparison",
                "minimum_practical_effect": "0",
                "risk_constraints": {"authority_expansion": False},
                "retention_tolerances": {"negative_results": "retain"},
                "promotion_rule": "qualified-only",
            }

            def authority_for(registration):
                return AblationQualificationAuthority(
                    scientific_registry=science,
                    experience_memory=memory,
                    artifact_store=artifacts,
                    protocol_id=registration.protocol_id,
                    protocol_hash=registration.protocol_hash,
                    source_revision="9" * 40,
                    causal_cutoff=cases[0].full.decision_utc + timedelta(hours=1),
                    granted_permissions={"RESEARCH"},
                    task="ablation-qualification",
                    instrument_family="EQUITY",
                )

            legacy = science.register_protocol(
                dict(base),
                protocol_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            )
            missing_decision = evaluate_qualified_incremental_value(
                "agent",
                cases,
                authority=authority_for(legacy),
                minimum_pairs=1,
                required_lower_bound=Decimal("-999"),
                uncertainty_multiplier=Decimal("0"),
            )
            self.assertEqual(
                missing_decision.reason,
                "registered_ablation_decision_policy_unavailable",
            )

            decision_only_payload = dict(base)
            decision_only_payload["ablation_decision_policy"] = {
                "schema_version": "1.0.0",
                "minimum_pairs": 2,
                "required_lower_bound": "0",
                "uncertainty_multiplier": "2",
                "decision_rule": "exact-rational-d2-sample-variance-v1",
            }
            decision_only = science.register_protocol(
                decision_only_payload,
                protocol_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            )
            missing_value = evaluate_qualified_incremental_value(
                "agent",
                cases,
                authority=authority_for(decision_only),
                minimum_pairs=999,
                required_lower_bound=Decimal("-999"),
                uncertainty_multiplier=Decimal("0"),
            )
            self.assertEqual(
                missing_value.reason,
                "registered_ablation_value_policy_unavailable",
            )
            self.assertEqual(missing_value.required_lower_bound, Decimal("0"))
            self.assertEqual(missing_value.uncertainty_multiplier, Decimal("2"))

            population_payload = dict(base)
            population_payload["ablation_decision_policy"] = dict(
                decision_only_payload["ablation_decision_policy"]
            )
            population_payload["ablation_value_policy"] = {
                "schema_version": "1.0.0",
                "value_unit": "USD",
                "utility_projection_ref": (
                    "artifact:11111111-1111-4111-8111-111111111111@sha256:"
                    + "1" * 64
                ),
                "cost_projection_ref": (
                    "artifact:22222222-2222-4222-8222-222222222222@sha256:"
                    + "2" * 64
                ),
                "fx_valuation_ref": None,
            }
            population_registration = science.register_protocol(
                population_payload,
                protocol_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            )
            shadowed_authority = authority_for(population_registration)
            shadow_calls: list[str] = []

            def hostile_population_resolver(*_args, **_kwargs):
                shadow_calls.append("resolve_population")
                raise AssertionError("instance population resolver executed")

            shadowed_authority.resolve_population = hostile_population_resolver
            shadow_result = evaluate_qualified_incremental_value(
                "agent",
                cases,
                authority=shadowed_authority,
                minimum_pairs=999,
                required_lower_bound=Decimal("-999"),
            )
            self.assertEqual(
                shadow_result.reason,
                "canonical_population_evidence_unavailable",
            )
            self.assertEqual(shadow_calls, [])

            fx_required_payload = dict(base)
            fx_required_payload["ablation_decision_policy"] = dict(
                decision_only_payload["ablation_decision_policy"]
            )
            fx_required_payload["ablation_value_policy"] = {
                "schema_version": "1.0.0",
                "value_unit": "USD",
                "utility_projection_ref": (
                    "artifact:11111111-1111-4111-8111-111111111111@sha256:"
                    + "1" * 64
                ),
                "cost_projection_ref": (
                    "artifact:22222222-2222-4222-8222-222222222222@sha256:"
                    + "2" * 64
                ),
                "fx_valuation_ref": (
                    "artifact:55555555-5555-4555-8555-555555555555@sha256:"
                    + "5" * 64
                ),
            }
            fx_required = science.register_protocol(
                fx_required_payload,
                protocol_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            )
            fx_result = evaluate_qualified_incremental_value(
                "agent",
                cases,
                authority=authority_for(fx_required),
                minimum_pairs=999,
                required_lower_bound=Decimal("-999"),
            )
            self.assertEqual(
                fx_result.reason,
                "registered_ablation_fx_valuation_evidence_unavailable",
            )
            self.assertEqual(fx_result.required_lower_bound, Decimal("0"))
            self.assertEqual(fx_result.uncertainty_multiplier, Decimal("2"))

            unsupported_payload = dict(base)
            unsupported_payload["ablation_decision_policy"] = {
                **decision_only_payload["ablation_decision_policy"],
                "decision_rule": "unsupported-rule-v1",
            }
            unsupported_payload["ablation_value_policy"] = {
                "schema_version": "1.0.0",
                "value_unit": "USD",
                "utility_projection_ref": (
                    "artifact:11111111-1111-4111-8111-111111111111@sha256:"
                    + "1" * 64
                ),
                "cost_projection_ref": (
                    "artifact:22222222-2222-4222-8222-222222222222@sha256:"
                    + "2" * 64
                ),
                "fx_valuation_ref": None,
            }
            unsupported = science.register_protocol(
                unsupported_payload,
                protocol_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            )
            unsupported_result = evaluate_qualified_incremental_value(
                "agent",
                cases,
                authority=authority_for(unsupported),
                minimum_pairs=999,
                required_lower_bound=Decimal("-999"),
            )
            self.assertEqual(
                unsupported_result.reason,
                "registered_ablation_decision_rule_unsupported",
            )


    def test_outcome_artifact_ref_rejects_text_subclass_before_uuid_callbacks(self):
        calls: list[str] = []

        class HostileText(str):
            def replace(self, *args, **kwargs):
                calls.append("replace")
                return super().replace(*args, **kwargs)

            def strip(self, *args, **kwargs):
                calls.append("strip")
                return super().strip(*args, **kwargs)

        with self.assertRaisesRegex(TypeError, "exact canonical UUID text"):
            AblationOutcomeArtifactRef(
                artifact_id=HostileText(
                    "77777777-7777-4777-8777-777777777770"
                ),
                sha256="sha256:" + "7" * 64,
            )
        self.assertEqual(calls, [])

    def test_registered_projection_rule_authentication_is_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")

            def publish(
                artifact_id: str,
                payload: dict,
                *,
                media_type: str = (
                    "application/vnd.autotrade.ablation-value-projection+json"
                ),
                canonical: bool = True,
            ) -> str:
                raw_text = json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":") if canonical else None,
                    ensure_ascii=False,
                )
                manifest = store.publish_bytes(
                    artifact_id=artifact_id,
                    data=raw_text.encode("utf-8"),
                    media_type=media_type,
                    rights={"storage": True, "export": False},
                    source_refs=["wp63:test-projection-rule"],
                )
                return f"artifact:{artifact_id}@{manifest['sha256']}"

            base = {
                "schema_version": 1,
                "projection_kind": "UTILITY",
                "value_unit": "USD",
                "owner_authority": "CANONICAL_RECONCILED_OUTCOME",
                "rule_id": "reconciled-outcome-net-value-v1",
                "cost_components": [],
            }
            reference = publish(
                "77777777-7777-4777-8777-777777777771",
                base,
            )
            descriptor = ablation_module._load_registered_projection_descriptor(
                store,
                reference,
                projection_kind="UTILITY",
                value_unit="USD",
            )
            self.assertEqual(descriptor.projection_kind, "UTILITY")
            self.assertEqual(descriptor.value_unit, "USD")
            self.assertEqual(
                descriptor.owner_authority,
                "CANONICAL_RECONCILED_OUTCOME",
            )
            with (
                patch.object(
                    ArtifactStore,
                    "load_manifest",
                    side_effect=AssertionError("split manifest read executed"),
                ),
                patch.object(
                    ArtifactStore,
                    "read_bytes",
                    side_effect=AssertionError("split object read executed"),
                ),
            ):
                snapshot_descriptor = (
                    ablation_module._load_registered_projection_descriptor(
                        store,
                        reference,
                        projection_kind="UTILITY",
                        value_unit="USD",
                    )
                )
            self.assertEqual(snapshot_descriptor, descriptor)

            wrong_owner = publish(
                "77777777-7777-4777-8777-777777777772",
                {**base, "owner_authority": "CALLER_ASSERTED"},
            )
            with self.assertRaisesRegex(ValueError, "owner authority mismatch"):
                ablation_module._load_registered_projection_descriptor(
                    store,
                    wrong_owner,
                    projection_kind="UTILITY",
                    value_unit="USD",
                )

            wrong_kind = publish(
                "77777777-7777-4777-8777-777777777773",
                {**base, "projection_kind": "COST"},
            )
            with self.assertRaisesRegex(ValueError, "kind mismatch"):
                ablation_module._load_registered_projection_descriptor(
                    store,
                    wrong_kind,
                    projection_kind="UTILITY",
                    value_unit="USD",
                )

            wrong_rule = publish(
                "77777777-7777-4777-8777-777777777778",
                {**base, "rule_id": "caller-selected-rule-v9"},
            )
            with self.assertRaisesRegex(ValueError, "rule is unsupported"):
                ablation_module._load_registered_projection_descriptor(
                    store,
                    wrong_rule,
                    projection_kind="UTILITY",
                    value_unit="USD",
                )

            wrong_unit = publish(
                "77777777-7777-4777-8777-777777777774",
                {**base, "value_unit": "EUR"},
            )
            with self.assertRaisesRegex(ValueError, "value unit mismatch"):
                ablation_module._load_registered_projection_descriptor(
                    store,
                    wrong_unit,
                    projection_kind="UTILITY",
                    value_unit="USD",
                )

            noncanonical = publish(
                "77777777-7777-4777-8777-777777777775",
                base,
                canonical=False,
            )
            with self.assertRaisesRegex(ValueError, "JSON must be canonical"):
                ablation_module._load_registered_projection_descriptor(
                    store,
                    noncanonical,
                    projection_kind="UTILITY",
                    value_unit="USD",
                )

            incomplete_cost = publish(
                "77777777-7777-4777-8777-777777777777",
                {
                    **base,
                    "projection_kind": "COST",
                    "owner_authority": "CANONICAL_ABLATION_COST_COMPOSITE",
                    "rule_id": "complete-after-cost-attribution-v1",
                    "cost_components": ["commission", "spread", "slippage"],
                },
            )
            with self.assertRaisesRegex(ValueError, "component coverage is incomplete"):
                ablation_module._load_registered_projection_descriptor(
                    store,
                    incomplete_cost,
                    projection_kind="COST",
                    value_unit="USD",
                )

            wrong_media = publish(
                "77777777-7777-4777-8777-777777777776",
                base,
                media_type="application/json",
            )
            with self.assertRaisesRegex(ValueError, "media type"):
                ablation_module._load_registered_projection_descriptor(
                    store,
                    wrong_media,
                    projection_kind="UTILITY",
                    value_unit="USD",
                )

            artifact_id = reference.split(":", 1)[1].split("@", 1)[0]
            forged_ref = f"artifact:{artifact_id}@sha256:{'0' * 64}"
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                ablation_module._load_registered_projection_descriptor(
                    store,
                    forged_ref,
                    projection_kind="UTILITY",
                    value_unit="USD",
                )

    def test_terminal_qualification_requires_persistent_protocol_population_and_artifacts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            science = ScientificRegistry(root / "science.sqlite3")
            memory = ExperienceMemory(root / "memory.sqlite3")
            artifacts = ArtifactStore(root / "artifacts")

            def projection_ref(
                artifact_id: str,
                *,
                projection_kind: str,
                owner_authority: str,
                rule_id: str,
                cost_components: list[str],
            ) -> str:
                payload = {
                    "schema_version": 1,
                    "projection_kind": projection_kind,
                    "value_unit": "USD",
                    "owner_authority": owner_authority,
                    "rule_id": rule_id,
                    "cost_components": cost_components,
                }
                raw = json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
                manifest = artifacts.publish_bytes(
                    artifact_id=artifact_id,
                    data=raw,
                    media_type=(
                        "application/vnd.autotrade.ablation-value-projection+json"
                    ),
                    rights={"storage": True, "export": False},
                    source_refs=["wp63:preregistered-projection-rule"],
                )
                return f"artifact:{artifact_id}@{manifest['sha256']}"

            utility_projection_ref = projection_ref(
                "33333333-3333-4333-8333-333333333333",
                projection_kind="UTILITY",
                owner_authority="CANONICAL_RECONCILED_OUTCOME",
                rule_id="reconciled-outcome-net-value-v1",
                cost_components=[],
            )
            cost_projection_ref = projection_ref(
                "44444444-4444-4444-8444-444444444444",
                projection_kind="COST",
                owner_authority="CANONICAL_ABLATION_COST_COMPOSITE",
                rule_id="complete-after-cost-attribution-v1",
                cost_components=[
                    "commission",
                    "spread",
                    "slippage",
                    "financing",
                    "funding",
                    "borrow",
                    "market_data",
                    "model_compute",
                    "infrastructure",
                    "tax_estimate",
                ],
            )
            protocol_payload = {
                "hypothesis": "agent adds after-cost value",
                "strategy": "matched causal ablation",
                "features": ["base", "agent"],
                "search_space": {"agent": ["enabled", "ablated"]},
                "train_period": {"start": "2026-01-01", "end": "2026-01-02"},
                "validation_period": {"start": "2026-01-04", "end": "2026-01-05"},
                "test_period": {"start": "2026-01-07", "end": "2026-01-08"},
                "forward_period": {"start": "2026-01-10", "end": "2026-01-11"},
                "labels": ["net_value"],
                "horizons": ["1d"],
                "purge_embargo": {"purge": "1d", "embargo": "1d"},
                "universe": ["TEST"],
                "cost_fill_model": "canonical-cost-v1",
                "baselines": ["ablated"],
                "primary_metrics": ["net_incremental_value"],
                "secondary_metrics": ["latency"],
                "trial_budget": 2,
                "stopping_rules": {"maximum_trials": 2},
                "ablation_decision_policy": {
                    "schema_version": "1.0.0",
                    "minimum_pairs": 2,
                    "required_lower_bound": "0",
                    "uncertainty_multiplier": "2",
                    "decision_rule": "exact-rational-d2-sample-variance-v1",
                },
                "ablation_value_policy": {
                    "schema_version": "1.0.0",
                    "value_unit": "USD",
                    "utility_projection_ref": utility_projection_ref,
                    "cost_projection_ref": cost_projection_ref,
                    "fx_valuation_ref": None,
                },
                "statistical_estimator": "matched-lower-bound",
                "multiplicity_treatment": "pre-registered-single-comparison",
                "minimum_practical_effect": "0",
                "risk_constraints": {"authority_expansion": False},
                "retention_tolerances": {"negative_results": "retain"},
                "promotion_rule": "qualified-only",
            }
            registration = science.register_protocol(
                protocol_payload,
                protocol_id="11111111-1111-4111-8111-111111111111",
            )
            attacker_science = ScientificRegistry(root / "attacker-science.sqlite3")
            attacker_payload = {
                **protocol_payload,
                "minimum_practical_effect": "-999",
                "ablation_decision_policy": {
                    "schema_version": "1.0.0",
                    "minimum_pairs": 2,
                    "required_lower_bound": "-999",
                    "uncertainty_multiplier": "0",
                    "decision_rule": "exact-rational-d2-sample-variance-v1",
                },
            }
            attacker_registration = attacker_science.register_protocol(
                attacker_payload,
                protocol_id="55555555-5555-4555-8555-555555555555",
            )
            registered_at = datetime.fromisoformat(registration.created_at)
            self.assertIsNotNone(registered_at.tzinfo)
            registered_at = registered_at.astimezone(timezone.utc)
            cutoff = registered_at + timedelta(hours=1)
            evaluation_cutoff = cutoff + timedelta(hours=2)
            units = (
                "22222222-2222-4222-8222-222222222222",
                "33333333-3333-4333-8333-333333333333",
            )
            cases = [
                pair(
                    "qualified-authority-a",
                    "2",
                    population_unit=units[0],
                    cutoff=cutoff,
                ),
                pair(
                    "qualified-authority-b",
                    "2",
                    population_unit=units[1],
                    cutoff=cutoff,
                ),
            ]
            for unit, matched in zip(units, cases):
                memory.append_episode(
                    episode_id=unit,
                    decision_time=matched.full.decision_utc,
                    information_cutoff=matched.full.input_cutoff_utc,
                    task="ablation-qualification",
                    regime="test",
                    instrument_family="EQUITY",
                    permission_class="RESEARCH",
                    payload={
                        "evidence_refs": ["artifact:source"],
                        "intended_action": {
                            "case_id": matched.full.case_id,
                            "side": "BUY",
                        },
                        "actual_execution": {"fills": []},
                        "outcome": {
                            "class": "POSITIVE",
                            "label": "observed",
                            "label_mature": True,
                            "reconciliation_state": "RECONCILED",
                        },
                        "costs": {"USD": "0"},
                    },
                )
            population = memory.coverage_population_snapshot(
                causal_cutoff=evaluation_cutoff,
                granted_permissions={"RESEARCH"},
                task="ablation-qualification",
                instrument_family="EQUITY",
            )
            refs = []
            source_revision = "9" * 40
            artifact_index = 0
            for matched in cases:
                for item in (matched.full, matched.ablated):
                    artifact_index += 1
                    artifact_id = str(
                        UUID(int=0x44444444444440008000000000000000 + artifact_index)
                    )
                    payload = {
                        "schema_version": 1,
                        "case_id": item.case_id,
                        "variant": item.variant,
                        "population_unit_id": item.population_unit_id,
                        "utility": str(item.utility),
                        "cost": str(item.cost),
                        "outcome_available_utc": item.outcome_available_utc.isoformat().replace("+00:00", "Z"),
                        "source_revision": source_revision,
                        "protocol_id": registration.protocol_id,
                        "protocol_hash": registration.protocol_hash,
                        "population_root": population.root_hash,
                        "utility_evidence_digest": FINGERPRINT_B,
                        "cost_evidence_digest": FINGERPRINT_C,
                        "superseded_at_utc": None,
                    }
                    raw = json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                    manifest = artifacts.publish_bytes(
                        artifact_id=artifact_id,
                        data=raw,
                        media_type="application/vnd.autotrade.ablation-outcome+json",
                        rights={"storage": True, "export": False},
                        source_refs=[f"protocol:{registration.protocol_id}"],
                    )
                    refs.append(
                        AblationOutcomeArtifactRef(
                            artifact_id=artifact_id,
                            sha256=manifest["sha256"],
                        )
                    )
            authority = AblationQualificationAuthority(
                scientific_registry=science,
                experience_memory=memory,
                artifact_store=artifacts,
                protocol_id=registration.protocol_id,
                protocol_hash=registration.protocol_hash,
                source_revision=source_revision,
                causal_cutoff=evaluation_cutoff,
                granted_permissions={"RESEARCH"},
                task="ablation-qualification",
                instrument_family="EQUITY",
            )
            population_preflight = authority.resolve_population(cases)
            self.assertTrue(population_preflight.complete)
            self.assertEqual(
                population_preflight.population_unit_ids,
                tuple(sorted(units)),
            )
            self.assertIsNotNone(population_preflight.coverage_digest)
            with (
                patch.object(
                    ArtifactStore,
                    "load_manifest",
                    side_effect=AssertionError("split manifest read executed"),
                ),
                patch.object(
                    ArtifactStore,
                    "read_bytes",
                    side_effect=AssertionError("split object read executed"),
                ),
            ):
                one_snapshot_outcome = authority._load_outcome(
                    refs[0],
                    population_root=population.root_hash,
                )
            self.assertEqual(one_snapshot_outcome.evidence_digest, refs[0].sha256)

            for field, forged_value in (
                ("scientific_registry", attacker_science),
                ("experience_memory", ExperienceMemory(root / "attacker-memory.sqlite3")),
                ("artifact_store", ArtifactStore(root / "attacker-artifacts")),
                ("protocol_id", attacker_registration.protocol_id),
                ("protocol_hash", attacker_registration.protocol_hash),
                ("source_revision", "8" * 40),
                ("causal_cutoff", evaluation_cutoff - timedelta(minutes=1)),
                ("granted_permissions", {"ATTACKER"}),
                ("task", "attacker-task"),
                ("instrument_family", "CRYPTO"),
            ):
                tampered_authority = AblationQualificationAuthority(
                    scientific_registry=science,
                    experience_memory=memory,
                    artifact_store=artifacts,
                    protocol_id=registration.protocol_id,
                    protocol_hash=registration.protocol_hash,
                    source_revision=source_revision,
                    causal_cutoff=evaluation_cutoff,
                    granted_permissions={"RESEARCH"},
                    task="ablation-qualification",
                    instrument_family="EQUITY",
                )
                object.__setattr__(
                    tampered_authority,
                    field,
                    forged_value,
                )
                tampered = evaluate_qualified_incremental_value(
                    "agent",
                    cases,
                    authority=tampered_authority,
                    minimum_pairs=999,
                    required_lower_bound=Decimal("-999"),
                    uncertainty_multiplier=Decimal("0"),
                )
                self.assertEqual(
                    tampered.reason,
                    "registered_ablation_decision_policy_unavailable",
                    field,
                )
                self.assertEqual(tampered.required_lower_bound, Decimal("0"), field)
                self.assertEqual(tampered.uncertainty_multiplier, Decimal("0"), field)

            in_place_tamper = AblationQualificationAuthority(
                scientific_registry=science,
                experience_memory=memory,
                artifact_store=artifacts,
                protocol_id=registration.protocol_id,
                protocol_hash=registration.protocol_hash,
                source_revision=source_revision,
                causal_cutoff=evaluation_cutoff,
                granted_permissions={"RESEARCH"},
                task="ablation-qualification",
                instrument_family="EQUITY",
            )
            in_place_tamper.granted_permissions.add("ATTACKER")
            in_place_result = evaluate_qualified_incremental_value(
                "agent",
                cases,
                authority=in_place_tamper,
                minimum_pairs=999,
                required_lower_bound=Decimal("-999"),
                uncertainty_multiplier=Decimal("0"),
            )
            self.assertEqual(
                in_place_result.reason,
                "registered_ablation_decision_policy_unavailable",
            )

            self.assertFalse(
                hasattr(
                    ablation_module,
                    "_issued_ablation_authority_policy_binding",
                )
            )
            ablation_module._issued_ablation_authority_policy_binding = (
                lambda _authority: (
                    attacker_science,
                    attacker_registration.protocol_id,
                    attacker_registration.protocol_hash,
                )
            )
            try:
                result = evaluate_qualified_incremental_value(
                    "agent",
                    cases,
                    authority=authority,
                    outcome_refs=refs,
                    minimum_pairs=999,
                    required_lower_bound=Decimal("-999"),
                    uncertainty_multiplier=Decimal("0"),
                )
            finally:
                del ablation_module._issued_ablation_authority_policy_binding
            self.assertEqual(result.status, "INCONCLUSIVE")
            self.assertEqual(
                result.reason,
                "canonical_utility_cost_owner_evidence_unavailable",
            )
            self.assertEqual(result.required_lower_bound, Decimal("0"))
            self.assertEqual(result.uncertainty_multiplier, Decimal("2"))

            pre_outcome_authority = AblationQualificationAuthority(
                scientific_registry=science,
                experience_memory=memory,
                artifact_store=artifacts,
                protocol_id=registration.protocol_id,
                protocol_hash=registration.protocol_hash,
                source_revision=source_revision,
                causal_cutoff=cutoff + timedelta(minutes=30),
                granted_permissions={"RESEARCH"},
                task="ablation-qualification",
                instrument_family="EQUITY",
            )
            with self.assertRaisesRegex(ValueError, "not available by causal cutoff"):
                pre_outcome_authority.resolve(cases, outcome_refs=())
            with self.assertRaisesRegex(ValueError, "not available by causal cutoff"):
                pre_outcome_authority.resolve(cases, outcome_refs=refs)

            with self.assertRaisesRegex(ValueError, "exactly match selected pairs"):
                authority.resolve(cases, outcome_refs=refs[:-1])
            with self.assertRaisesRegex(ValueError, "duplicate canonical"):
                authority.resolve(cases, outcome_refs=refs + refs[:1])

            forged = canonical_evidence(cases[0]) + canonical_evidence(cases[1])
            diagnostic = evaluate_qualified_incremental_value(
                "agent",
                cases,
                population=registered_population(
                    cases,
                    evaluation_cutoff=evaluation_cutoff,
                ),
                canonical_outcomes=forged,
                minimum_pairs=2,
                required_lower_bound=Decimal("0"),
            )
            self.assertEqual(diagnostic.status, "INCONCLUSIVE")
            self.assertEqual(
                diagnostic.reason,
                "untrusted_caller_authored_qualification_evidence",
            )

            extra_unit = "66666666-6666-4666-8666-666666666666"
            extra_case = pair(
                "qualified-authority-c",
                "2",
                population_unit=extra_unit,
                cutoff=cutoff,
            )
            memory.append_episode(
                episode_id=extra_unit,
                decision_time=extra_case.full.decision_utc,
                information_cutoff=extra_case.full.input_cutoff_utc,
                task="ablation-qualification",
                regime="test",
                instrument_family="EQUITY",
                permission_class="RESEARCH",
                payload={
                    "evidence_refs": ["artifact:pending-label"],
                    "intended_action": {
                        "case_id": extra_case.full.case_id,
                        "side": "BUY",
                    },
                    "actual_execution": {"fills": []},
                    "outcome": {
                        "class": "PENDING",
                        "label": "pending",
                        "label_mature": False,
                        "reconciliation_state": "PENDING",
                    },
                    "costs": {"USD": "0"},
                },
            )
            incomplete_authority = AblationQualificationAuthority(
                scientific_registry=science,
                experience_memory=memory,
                artifact_store=artifacts,
                protocol_id=registration.protocol_id,
                protocol_hash=registration.protocol_hash,
                source_revision=source_revision,
                causal_cutoff=evaluation_cutoff,
                granted_permissions={"RESEARCH"},
                task="ablation-qualification",
                instrument_family="EQUITY",
            )
            omitted = evaluate_qualified_incremental_value(
                "agent",
                cases,
                authority=incomplete_authority,
                outcome_refs=refs,
                minimum_pairs=999,
                required_lower_bound=Decimal("-999"),
                uncertainty_multiplier=Decimal("0"),
            )
            self.assertEqual(omitted.status, "INCONCLUSIVE")
            self.assertEqual(omitted.reason, "incomplete_registered_population")

            immature = evaluate_qualified_incremental_value(
                "agent",
                cases + [extra_case],
                authority=incomplete_authority,
                outcome_refs=refs,
                minimum_pairs=999,
                required_lower_bound=Decimal("-999"),
                uncertainty_multiplier=Decimal("0"),
            )
            self.assertEqual(immature.status, "INCONCLUSIVE")
            self.assertEqual(immature.reason, "incomplete_registered_population")


    def test_trusted_iterable_ingress_fails_before_callbacks(self):
        calls: list[str] = []

        class HostileList(list):
            def __iter__(self):
                calls.append("iter")
                raise AssertionError("hostile iterable callback executed")

        authority = object.__new__(AblationQualificationAuthority)
        cases = [
            pair("trusted-ingress-a", "2", population_unit="trusted-ingress-unit-a"),
            pair("trusted-ingress-b", "2", population_unit="trusted-ingress-unit-b"),
        ]

        with self.assertRaisesRegex(
            TypeError,
            "pairs must be an exact list or tuple",
        ):
            evaluate_qualified_incremental_value(
                "agent",
                HostileList(cases),
                authority=authority,
                minimum_pairs=2,
                required_lower_bound=Decimal("0"),
            )
        self.assertEqual(calls, [])

        for field, kwargs, message in (
            (
                "canonical_outcomes",
                {"canonical_outcomes": HostileList()},
                "canonical_outcomes must be an exact list or tuple",
            ),
            (
                "outcome_refs",
                {"outcome_refs": HostileList()},
                "outcome_refs must be an exact list or tuple",
            ),
        ):
            with self.subTest(field=field):
                calls.clear()
                with self.assertRaisesRegex(TypeError, message):
                    evaluate_qualified_incremental_value(
                        "agent",
                        cases,
                        authority=authority,
                        minimum_pairs=2,
                        required_lower_bound=Decimal("0"),
                        **kwargs,
                    )
                self.assertEqual(calls, [])


    def test_direct_trusted_resolvers_reject_outer_subclasses_before_authority_reads(self):
        calls: list[str] = []

        class HostileList(list):
            def __iter__(self):
                calls.append("iter")
                raise AssertionError("hostile iterable callback executed")

        authority = object.__new__(AblationQualificationAuthority)
        cases = [
            pair("direct-ingress-a", "2", population_unit="direct-ingress-unit-a"),
            pair("direct-ingress-b", "2", population_unit="direct-ingress-unit-b"),
        ]

        with self.assertRaisesRegex(
            TypeError,
            "pairs must be an exact list or tuple",
        ):
            authority.resolve_population(HostileList(cases))
        self.assertEqual(calls, [])

        calls.clear()
        with self.assertRaisesRegex(
            TypeError,
            "pairs must be an exact list or tuple",
        ):
            authority.resolve(HostileList(cases), outcome_refs=[])
        self.assertEqual(calls, [])

        calls.clear()
        with self.assertRaisesRegex(
            TypeError,
            "outcome_refs must be an exact list or tuple",
        ):
            authority.resolve(cases, outcome_refs=HostileList())
        self.assertEqual(calls, [])


    def test_descriptive_summary_reporting_is_all_or_none_on_late_failure(self):
        cases = [
            pair("report-atomic-a", "2", population_unit="report-atomic-unit-a"),
            pair("report-atomic-b", "3", population_unit="report-atomic-unit-b"),
        ]
        original = ablation_module._report_fraction
        calls = 0

        def fail_third_projection(value):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise InvalidOperation
            return original(value)

        with patch.object(
            ablation_module,
            "_report_fraction",
            side_effect=fail_third_projection,
        ):
            summary = summarize_ablation("agent", cases)

        self.assertEqual(calls, 3)
        self.assertEqual(summary.reporting_status, "UNAVAILABLE")
        self.assertIsNone(summary.mean_utility_delta)
        self.assertIsNone(summary.mean_cost_delta)
        self.assertIsNone(summary.mean_latency_delta_ms)


    def test_authority_permissions_reject_text_subclass_before_sort_or_strip(self):
        calls: list[str] = []

        class HostileText(str):
            def __lt__(self, other):
                calls.append("lt")
                raise AssertionError("permission comparison callback executed")

            def strip(self, *args, **kwargs):
                calls.append("strip")
                raise AssertionError("permission strip callback executed")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            science = ScientificRegistry(root / "science.sqlite3")
            memory = ExperienceMemory(root / "memory.sqlite3")
            artifacts = ArtifactStore(root / "artifacts")
            permissions = {"RESEARCH", HostileText("ATTACKER")}

            with self.assertRaisesRegex(ValueError, "exact canonical text"):
                AblationQualificationAuthority(
                    scientific_registry=science,
                    experience_memory=memory,
                    artifact_store=artifacts,
                    protocol_id="permission-preflight",
                    protocol_hash=FINGERPRINT_A,
                    source_revision="1" * 40,
                    causal_cutoff=CUT,
                    granted_permissions=permissions,
                )
            self.assertEqual(calls, [])

            authority = AblationQualificationAuthority(
                scientific_registry=science,
                experience_memory=memory,
                artifact_store=artifacts,
                protocol_id="permission-revalidation",
                protocol_hash=FINGERPRINT_A,
                source_revision="1" * 40,
                causal_cutoff=CUT,
                granted_permissions={"RESEARCH"},
            )
            authority.granted_permissions.add(HostileText("ATTACKER"))
            result = evaluate_qualified_incremental_value(
                "agent",
                [
                    pair(
                        "permission-revalidation-a",
                        "2",
                        population_unit="permission-revalidation-unit-a",
                    ),
                    pair(
                        "permission-revalidation-b",
                        "2",
                        population_unit="permission-revalidation-unit-b",
                    ),
                ],
                authority=authority,
                minimum_pairs=2,
                required_lower_bound=Decimal("0"),
            )
            self.assertEqual(
                result.reason,
                "registered_ablation_decision_policy_unavailable",
            )
            self.assertEqual(calls, [])


    def test_bound_database_path_retarget_fails_before_replacement_touch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            science = ScientificRegistry(root / "science.sqlite3")
            memory = ExperienceMemory(root / "memory.sqlite3")
            artifacts = ArtifactStore(root / "artifacts")
            authority = AblationQualificationAuthority(
                scientific_registry=science,
                experience_memory=memory,
                artifact_store=artifacts,
                protocol_id="database-path-binding",
                protocol_hash=FINGERPRINT_A,
                source_revision="1" * 40,
                causal_cutoff=CUT,
                granted_permissions={"RESEARCH"},
            )
            cases = [
                pair(
                    "database-path-a",
                    "2",
                    population_unit="database-path-unit-a",
                ),
                pair(
                    "database-path-b",
                    "2",
                    population_unit="database-path-unit-b",
                ),
            ]

            canonical_memory_path = memory.path
            attacker_memory_path = root / "attacker-memory.sqlite3"
            memory.path = attacker_memory_path
            with self.assertRaisesRegex(
                ProtocolViolation,
                "memory database path changed after issuance",
            ):
                authority.resolve_population(cases)
            self.assertFalse(attacker_memory_path.exists())
            memory.path = canonical_memory_path

            attacker_registry_path = root / "attacker-science.sqlite3"
            science.path = attacker_registry_path
            with self.assertRaisesRegex(
                ProtocolViolation,
                "registry database path changed after issuance",
            ):
                authority.resolve_population(cases)
            self.assertFalse(attacker_registry_path.exists())

    def test_bound_store_method_shadows_fail_before_callbacks(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            science = ScientificRegistry(root / "science.sqlite3")
            memory = ExperienceMemory(root / "memory.sqlite3")
            artifacts = ArtifactStore(root / "artifacts")
            authority = AblationQualificationAuthority(
                scientific_registry=science,
                experience_memory=memory,
                artifact_store=artifacts,
                protocol_id="method-shadow-preflight",
                protocol_hash=FINGERPRINT_A,
                source_revision="1" * 40,
                causal_cutoff=CUT,
                granted_permissions={"RESEARCH"},
            )
            cases = [
                pair(
                    "method-shadow-a",
                    "2",
                    population_unit="method-shadow-unit-a",
                ),
                pair(
                    "method-shadow-b",
                    "2",
                    population_unit="method-shadow-unit-b",
                ),
            ]

            for label, target, method_name in (
                ("registry", science, "protocol_registration"),
                ("memory", memory, "coverage_population_snapshot"),
                ("artifact store", artifacts, "read_authenticated_snapshot"),
            ):
                calls: list[str] = []

                def forbidden(*args, **kwargs):
                    calls.append(method_name)
                    raise AssertionError("shadowed canonical method executed")

                setattr(target, method_name, forbidden)
                try:
                    result = evaluate_qualified_incremental_value(
                        "agent",
                        cases,
                        authority=authority,
                        minimum_pairs=2,
                        required_lower_bound=Decimal("0"),
                    )
                finally:
                    delattr(target, method_name)

                self.assertEqual(
                    result.reason,
                    "registered_ablation_decision_policy_unavailable",
                    label,
                )
                self.assertEqual(calls, [], label)


    def test_bound_store_state_key_subclass_fails_before_hash_callback(self):
        calls: list[str] = []

        class HostileKey(str):
            def __hash__(self):
                calls.append("hash")
                return str.__hash__(self)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            science = ScientificRegistry(root / "science.sqlite3")
            memory = ExperienceMemory(root / "memory.sqlite3")
            artifacts = ArtifactStore(root / "artifacts")
            authority = AblationQualificationAuthority(
                scientific_registry=science,
                experience_memory=memory,
                artifact_store=artifacts,
                protocol_id="state-key-preflight",
                protocol_hash=FINGERPRINT_A,
                source_revision="1" * 40,
                causal_cutoff=CUT,
                granted_permissions={"RESEARCH"},
            )
            hostile_key = HostileKey("protocol_registration")
            science.__dict__[hostile_key] = lambda *_args, **_kwargs: None
            calls.clear()

            result = evaluate_qualified_incremental_value(
                "agent",
                [
                    pair(
                        "state-key-a",
                        "2",
                        population_unit="state-key-unit-a",
                    ),
                    pair(
                        "state-key-b",
                        "2",
                        population_unit="state-key-unit-b",
                    ),
                ],
                authority=authority,
                minimum_pairs=2,
                required_lower_bound=Decimal("0"),
            )

            self.assertEqual(
                result.reason,
                "registered_ablation_decision_policy_unavailable",
            )
            self.assertEqual(calls, [])


    def test_caller_correction_evidence_resolver_is_not_terminal_authority(self):
        calls: list[str] = []

        def hostile_resolver(_reference):
            calls.append("resolver")
            raise AssertionError("caller correction evidence resolver executed")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            science = ScientificRegistry(root / "science.sqlite3")
            memory = ExperienceMemory(
                root / "memory.sqlite3",
                correction_evidence_resolver=hostile_resolver,
            )
            artifacts = ArtifactStore(root / "artifacts")
            authority = AblationQualificationAuthority(
                scientific_registry=science,
                experience_memory=memory,
                artifact_store=artifacts,
                protocol_id="caller-correction-resolver",
                protocol_hash=FINGERPRINT_A,
                source_revision="1" * 40,
                causal_cutoff=CUT,
                granted_permissions={"RESEARCH"},
            )

            result = evaluate_qualified_incremental_value(
                "agent",
                [
                    pair(
                        "caller-resolver-a",
                        "2",
                        population_unit="caller-resolver-unit-a",
                    ),
                    pair(
                        "caller-resolver-b",
                        "2",
                        population_unit="caller-resolver-unit-b",
                    ),
                ],
                authority=authority,
                minimum_pairs=2,
                required_lower_bound=Decimal("0"),
            )

            self.assertEqual(
                result.reason,
                "registered_ablation_decision_policy_unavailable",
            )
            self.assertEqual(calls, [])

    def test_caller_correction_resolver_cannot_be_erased_after_issuance(self):
        calls: list[str] = []

        def hostile_resolver(_reference):
            calls.append("resolver")
            raise AssertionError("caller correction evidence resolver executed")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            science = ScientificRegistry(root / "science.sqlite3")
            memory = ExperienceMemory(
                root / "memory.sqlite3",
                correction_evidence_resolver=hostile_resolver,
            )
            artifacts = ArtifactStore(root / "artifacts")
            authority = AblationQualificationAuthority(
                scientific_registry=science,
                experience_memory=memory,
                artifact_store=artifacts,
                protocol_id="caller-correction-resolver-erasure",
                protocol_hash=FINGERPRINT_A,
                source_revision="1" * 40,
                causal_cutoff=CUT,
                granted_permissions={"RESEARCH"},
            )
            memory._correction_evidence_resolver = None

            with self.assertRaisesRegex(
                ProtocolViolation,
                "memory correction authority changed after issuance",
            ):
                authority.resolve_population(
                    [
                        pair(
                            "caller-resolver-erasure-a",
                            "2",
                            population_unit="caller-resolver-erasure-unit-a",
                        ),
                        pair(
                            "caller-resolver-erasure-b",
                            "2",
                            population_unit="caller-resolver-erasure-unit-b",
                        ),
                    ]
                )
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
