"""Plan-7 independent consumer contract for the existing Plan-2 ablation authority.

A descriptive PASS is never independently qualified economic proof. No provider,
financial sender, learner, or additional ablation engine is introduced here.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
import unittest

from autotrade_research.evaluation.ablation import (
    AblationOutcome,
    AblationPair,
    CausalInputEvidence,
    RegisteredAblationPopulation,
    build_ablation_evidence_bundle,
    evaluate_qualified_incremental_value,
    verify_ablation_evidence_bundle,
)
from mvp.autotrade_mvp.science_qualification import (
    QualificationGate,
    ScientificQualificationInput,
    qualify_scientific_learning,
)


CUT = datetime(2026, 9, 25, tzinfo=timezone.utc)
SOURCE = "a" * 40
H = "sha256:" + "b" * 64
PROTOCOL = "sha256:" + "c" * 64
DATASET = "sha256:" + "d" * 64


def pair(case_id: str, *, full_utility: str = "2") -> AblationPair:
    fingerprint = "sha256:" + sha256(("input:" + case_id).encode()).hexdigest()
    common = CausalInputEvidence(
        evidence_id="base-" + case_id,
        content_digest="sha256:" + sha256(("evidence:" + case_id).encode()).hexdigest(),
        component_id="base",
        available_utc=CUT,
    )
    shared = dict(
        case_id=case_id,
        input_fingerprint=fingerprint,
        elapsed_ms=20,
        deadline_ms=100,
        input_cutoff_utc=CUT,
        decision_utc=CUT,
        outcome_available_utc=CUT + timedelta(hours=1),
        input_evidence=(common,),
    )
    return AblationPair(
        "agent",
        AblationOutcome(
            variant="FULL", utility=Decimal(full_utility),
            cost=Decimal("0.2"), components=("base", "agent"), **shared
        ),
        AblationOutcome(
            variant="ABLATED", utility=Decimal("0"),
            cost=Decimal("0.1"), components=("base",), **shared
        ),
    )


def bundle(pairs):
    return build_ablation_evidence_bundle(
        "agent", pairs,
        source_revision=SOURCE,
        protocol_digest=PROTOCOL,
        dataset_digest=DATASET,
        minimum_pairs=2,
        required_lower_bound=Decimal("0"),
        uncertainty_multiplier=Decimal("2"),
    )


def registered(pairs, *, complete=True, extra_units=()):
    return RegisteredAblationPopulation(
        protocol_digest=PROTOCOL,
        population_digest=DATASET,
        stopping_rule_digest=H,
        source_revision=SOURCE,
        registered_at_utc=CUT - timedelta(days=1),
        evaluation_cutoff_utc=CUT + timedelta(hours=2),
        population_unit_ids=tuple(sorted(
            {p.full.population_unit_id for p in pairs} | set(extra_units)
        )),
        complete=complete,
    )


class Plan7IndependentAblationConsumerTests(unittest.TestCase):
    def setUp(self):
        self.pairs = (pair("case-1"), pair("case-2"))

    def test_matched_exact_rational_result_is_only_descriptive(self):
        sealed = bundle(self.pairs)
        self.assertTrue(verify_ablation_evidence_bundle(sealed, self.pairs))
        self.assertEqual(sealed.evaluation.status, "PASS")
        self.assertEqual(sealed.evaluation.decision_exact.mean, Fraction(19, 10))
        self.assertEqual(sealed.evaluation.decision_exact.sample_variance, Fraction(0, 1))
        self.assertEqual(sealed.pair_count, 2)
        qualified = evaluate_qualified_incremental_value(
            "agent", self.pairs,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
            population=registered(self.pairs),
        )
        self.assertEqual(qualified.status, "INCONCLUSIVE")
        self.assertEqual(qualified.reason, "missing_canonical_outcome_evidence")

    def test_complete_population_and_canonical_owner_not_forgeable_by_omission(self):
        for population in (
            registered(self.pairs, complete=False),
            registered(self.pairs, extra_units=("withheld-losing-unit",)),
        ):
            with self.subTest(population=population):
                result = evaluate_qualified_incremental_value(
                    "agent", self.pairs,
                    minimum_pairs=2,
                    required_lower_bound=Decimal("0"),
                    population=population,
                )
                self.assertEqual(result.status, "INCONCLUSIVE")
                self.assertEqual(result.reason, "incomplete_registered_population")

    def test_replay_is_deterministic_and_matched_population_is_exact(self):
        sealed = bundle(self.pairs)
        reversed_bundle = bundle(tuple(reversed(self.pairs)))
        self.assertEqual(sealed.payload, reversed_bundle.payload)
        self.assertEqual(sealed.content_digest, reversed_bundle.content_digest)
        self.assertTrue(verify_ablation_evidence_bundle(sealed, tuple(reversed(self.pairs))))
        with self.assertRaisesRegex(ValueError, "does not match"):
            verify_ablation_evidence_bundle(sealed, self.pairs[:1])

    def test_favorable_utility_edit_and_cost_edit_invalidate_locked_evidence(self):
        sealed = bundle(self.pairs)
        for field, value in (("utility", Decimal("200")), ("cost", Decimal("0"))):
            with self.subTest(field=field):
                changed = replace(self.pairs[0].full, **{field: value})
                edited_pair = AblationPair("agent", changed, self.pairs[0].ablated)
                with self.assertRaisesRegex(ValueError, "does not match"):
                    verify_ablation_evidence_bundle(sealed, (edited_pair, self.pairs[1]))

    def test_matching_fingerprint_and_independent_population_are_required(self):
        first = self.pairs[0]
        mismatched = replace(first.ablated, input_fingerprint=H)
        with self.assertRaisesRegex(ValueError, "input_fingerprint"):
            AblationPair("agent", first.full, mismatched)
        with self.assertRaisesRegex(ValueError, "duplicate matched"):
            bundle((first, first))

    def test_policy_multiplicity_and_source_are_locked_not_relabelled(self):
        sealed = bundle(self.pairs)
        with self.assertRaises(ValueError):
            replace(sealed, uncertainty_multiplier=Decimal("0"))
        with self.assertRaises(ValueError):
            replace(sealed, source_revision="f" * 40)
        self.assertNotEqual(
            sealed.content_digest,
            bundle((pair("case-1", full_utility="1"), self.pairs[1])).content_digest,
        )

    def test_signed_science_gate_cannot_be_self_issued_from_diagnostic_ablation(self):
        sealed = bundle(self.pairs)
        input_value = ScientificQualificationInput(
            candidate_hash=H,
            frozen_protocol_hash=PROTOCOL,
            input_snapshot_hash=DATASET,
            gates=(QualificationGate(
                gate_id="ablation",
                status="PASS",
                evidence_hashes=(sealed.content_digest,),
                candidate_hash=H,
                frozen_protocol_hash=PROTOCOL,
                input_snapshot_hash=DATASET,
            ),),
            economic_claim="ECONOMIC_EDGE_QUALIFIED",
            source_sha=SOURCE,
        )
        result = qualify_scientific_learning(input_value)
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertFalse(result.economic_claim_accepted)
        self.assertFalse(result.release_or_trading_authority)
        self.assertIn("SCIENCE.INDEPENDENT_ATTESTATION_MISSING", result.reason_codes)


if __name__ == "__main__":
    unittest.main()
