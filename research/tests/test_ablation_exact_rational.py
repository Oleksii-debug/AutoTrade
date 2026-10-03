from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from hashlib import sha256

from autotrade_research.evaluation.ablation import (
    AblationOutcome,
    AblationPair,
    CausalInputEvidence,
    build_ablation_evidence_bundle,
    evaluate_incremental_value,
    verify_ablation_evidence_bundle,
)

CUT = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc)


def _digest(text: str) -> str:
    return "sha256:" + sha256(text.encode("utf-8")).hexdigest()


def _pair(case_id: str, full_utility: str, ablated_utility: str = "0", *, full_cost: str = "0", ablated_cost: str = "0") -> AblationPair:
    evidence = CausalInputEvidence(
        evidence_id=f"{case_id}-agent",
        content_digest=_digest(f"{case_id}-agent-content"),
        component_id="agent",
        available_utc=CUT,
    )
    common = dict(
        case_id=case_id,
        input_fingerprint=_digest(f"{case_id}-inputs"),
        elapsed_ms=10,
        deadline_ms=100,
        input_cutoff_utc=CUT,
        decision_utc=CUT + timedelta(seconds=1),
        outcome_available_utc=CUT + timedelta(hours=1),
        population_unit_id=case_id,
    )
    return AblationPair(
        "agent",
        AblationOutcome(**common, variant="FULL", utility=Decimal(full_utility), cost=Decimal(full_cost), components=("base", "agent"), input_evidence=(evidence,)),
        AblationOutcome(**common, variant="ABLATED", utility=Decimal(ablated_utility), cost=Decimal(ablated_cost), components=("base",), input_evidence=()),
    )


class ExactRationalAblationTests(unittest.TestCase):
    def test_huge_negative_delta_cannot_round_into_pass(self):
        cases=[_pair("neg-a","1E100","1E100",full_cost="2",ablated_cost="1"),_pair("neg-b","1E100","1E100",full_cost="2",ablated_cost="1")]
        result=evaluate_incremental_value("agent",cases,minimum_pairs=2,required_lower_bound=Decimal("0"))
        self.assertEqual(result.status,"FAIL")
        self.assertEqual(result.mean_net_incremental_value,Decimal("-1"))
        self.assertEqual((result.decision_exact.mean.numerator,result.decision_exact.mean.denominator),(-1,1))

    def test_huge_positive_delta_remains_positive(self):
        cases=[_pair("pos-a","1E100","1E100",full_cost="1",ablated_cost="2"),_pair("pos-b","1E100","1E100",full_cost="1",ablated_cost="2")]
        result=evaluate_incremental_value("agent",cases,minimum_pairs=2,required_lower_bound=Decimal("0"))
        self.assertEqual(result.status,"PASS")
        self.assertEqual(result.decision_exact.mean.numerator,1)

    def test_exact_uncertainty_boundary_is_inclusive(self):
        cases=[_pair("bound-a","0"),_pair("bound-b","2")]
        boundary=evaluate_incremental_value("agent",cases,minimum_pairs=2,required_lower_bound=Decimal("0"),uncertainty_multiplier=Decimal("1"))
        below=evaluate_incremental_value("agent",cases,minimum_pairs=2,required_lower_bound=Decimal("1e-50"),uncertainty_multiplier=Decimal("1"))
        self.assertEqual(boundary.status,"PASS")
        self.assertEqual(boundary.decision_exact.lhs,boundary.decision_exact.rhs)
        self.assertEqual(below.status,"FAIL")
        self.assertLess(below.decision_exact.lhs,below.decision_exact.rhs)

    def test_verdict_is_independent_of_ambient_decimal_context(self):
        cases=[_pair("third-a","0"),_pair("third-b","0"),_pair("third-c","1")]
        with localcontext() as context:
            context.prec=6
            low=evaluate_incremental_value("agent",cases,minimum_pairs=3,required_lower_bound=Decimal("-1"))
        with localcontext() as context:
            context.prec=80
            high=evaluate_incremental_value("agent",cases,minimum_pairs=3,required_lower_bound=Decimal("-1"))
        self.assertEqual(low,high)
        self.assertEqual((low.decision_exact.mean.numerator,low.decision_exact.mean.denominator),(1,3))

    def test_locked_bundle_v2_authenticates_exact_decision_material(self):
        cases=[_pair("lock-a","0"),_pair("lock-b","2")]
        locked=build_ablation_evidence_bundle("agent",cases,source_revision="6"*40,protocol_digest=_digest("protocol"),dataset_digest=_digest("dataset"),minimum_pairs=2,required_lower_bound=Decimal("0"),uncertainty_multiplier=Decimal("1"))
        decoded=json.loads(locked.payload)
        self.assertEqual(decoded["schema_version"],"2.0.0")
        self.assertEqual(decoded["evaluation_policy"]["decision_rule"],"exact-rational-d2-sample-variance-v1")
        self.assertEqual(decoded["evaluation"]["decision_exact"]["lhs"],{"denominator":"1","numerator":"1"})
        self.assertTrue(verify_ablation_evidence_bundle(locked,cases))

    def test_out_of_envelope_economics_fail_before_scoring(self):
        with self.assertRaisesRegex(ValueError,"shared exact numeric resource envelope"):
            _pair("oversized","1E257")


if __name__ == "__main__":
    unittest.main()
