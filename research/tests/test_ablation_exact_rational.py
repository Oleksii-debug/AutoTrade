from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext
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
        cases = [_pair("third-a", "0"), _pair("third-b", "0"), _pair("third-c", "1")]
        results = []
        for precision, rounding in (
            (6, ROUND_DOWN),
            (10, ROUND_CEILING),
            (28, ROUND_FLOOR),
            (80, ROUND_HALF_EVEN),
        ):
            with localcontext() as context:
                context.prec = precision
                context.rounding = rounding
                results.append(
                    evaluate_incremental_value(
                        "agent",
                        cases,
                        minimum_pairs=3,
                        required_lower_bound=Decimal("-1"),
                    )
                )
        self.assertTrue(all(result == results[0] for result in results[1:]))
        self.assertEqual(
            (results[0].decision_exact.mean.numerator, results[0].decision_exact.mean.denominator),
            (1, 3),
        )

    def test_locked_bundle_v2_authenticates_exact_decision_material(self):
        cases=[_pair("lock-a","0"),_pair("lock-b","2")]
        locked=build_ablation_evidence_bundle("agent",cases,source_revision="6"*40,protocol_digest=_digest("protocol"),dataset_digest=_digest("dataset"),minimum_pairs=2,required_lower_bound=Decimal("0"),uncertainty_multiplier=Decimal("1"))
        decoded=json.loads(locked.payload)
        self.assertEqual(decoded["schema_version"],"2.0.0")
        self.assertEqual(decoded["evaluation_policy"]["decision_rule"],"exact-rational-d2-sample-variance-v1")
        self.assertEqual(decoded["evaluation"]["decision_exact"]["lhs"],{"denominator":"1","numerator":"1"})
        self.assertTrue(verify_ablation_evidence_bundle(locked,cases))

    def test_out_of_envelope_economics_fail_before_scoring(self):
        with self.assertRaisesRegex(ValueError, "shared exact numeric resource envelope"):
            _pair("oversized", "1E257")

        cases = [_pair("multiplier-a", "1"), _pair("multiplier-b", "1")]
        with self.assertRaisesRegex(ValueError, "shared exact numeric resource envelope"):
            evaluate_incremental_value(
                "agent",
                cases,
                minimum_pairs=2,
                required_lower_bound=Decimal("0"),
                uncertainty_multiplier=Decimal("1E257"),
            )


    def test_reporting_overflow_does_not_suppress_exact_terminal_decision(self):
        cases = [_pair("overflow-a", "0"), _pair("overflow-b", "1E255")]
        result = evaluate_incremental_value(
            "agent",
            cases,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
            uncertainty_multiplier=Decimal("1E255"),
        )
        self.assertEqual(result.status, "FAIL")
        self.assertIsNotNone(result.decision_exact)
        self.assertEqual(result.reporting_status, "UNAVAILABLE")
        self.assertIsNone(result.mean_net_incremental_value)
        self.assertIsNone(result.sample_stddev)
        self.assertIsNone(result.lower_bound)

        locked = build_ablation_evidence_bundle(
            "agent",
            cases,
            source_revision="c" * 40,
            protocol_digest=_digest("overflow-protocol"),
            dataset_digest=_digest("overflow-dataset"),
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
            uncertainty_multiplier=Decimal("1E255"),
        )
        decoded = json.loads(locked.payload)
        self.assertEqual(decoded["evaluation"]["status"], "FAIL")
        self.assertEqual(decoded["evaluation"]["reporting_status"], "UNAVAILABLE")
        self.assertIsNone(decoded["evaluation"]["lower_bound"])
        self.assertTrue(verify_ablation_evidence_bundle(locked, cases))

    def test_exact_decision_tamper_is_rejected_even_with_rehashed_payload(self):
        cases = [_pair("tamper-a", "0"), _pair("tamper-b", "2")]
        locked = build_ablation_evidence_bundle(
            "agent",
            cases,
            source_revision="7" * 40,
            protocol_digest=_digest("tamper-protocol"),
            dataset_digest=_digest("tamper-dataset"),
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
            uncertainty_multiplier=Decimal("1"),
        )
        decoded = json.loads(locked.payload)
        decoded["evaluation"]["decision_exact"]["rhs"]["numerator"] = "2"
        payload = json.dumps(
            decoded,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        digest = "sha256:" + sha256(payload.encode("utf-8")).hexdigest()
        with self.assertRaisesRegex(
            ValueError,
            "payload evaluation does not match bundle evaluation",
        ):
            replace(locked, payload=payload, content_digest=digest)

    def test_coherently_rehashed_reporting_tamper_fails_rebuild_verification(self):
        cases = [_pair("report-a", "0"), _pair("report-b", "2")]
        locked = build_ablation_evidence_bundle(
            "agent",
            cases,
            source_revision="8" * 40,
            protocol_digest=_digest("report-protocol"),
            dataset_digest=_digest("report-dataset"),
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
            uncertainty_multiplier=Decimal("1"),
        )
        tampered_evaluation = replace(
            locked.evaluation,
            lower_bound=Decimal("999"),
            mean_net_incremental_value=Decimal("999"),
        )
        decoded = json.loads(locked.payload)
        decoded["evaluation"]["lower_bound"] = "999"
        decoded["evaluation"]["mean_net_incremental_value"] = "999"
        payload = json.dumps(
            decoded,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        tampered = replace(
            locked,
            evaluation=tampered_evaluation,
            payload=payload,
            content_digest="sha256:" + sha256(payload.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(tampered.evaluation.status, locked.evaluation.status)
        with self.assertRaisesRegex(ValueError, "locked ablation evidence"):
            verify_ablation_evidence_bundle(tampered, cases)

    def test_v1_bundle_metadata_cannot_be_reinterpreted_as_exact_authority(self):
        cases = [_pair("legacy-a", "1"), _pair("legacy-b", "1")]
        locked = build_ablation_evidence_bundle(
            "agent",
            cases,
            source_revision="9" * 40,
            protocol_digest=_digest("legacy-protocol"),
            dataset_digest=_digest("legacy-dataset"),
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
        )
        decoded = json.loads(locked.payload)
        decoded["schema_version"] = "1.0.0"
        payload = json.dumps(
            decoded,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        digest = "sha256:" + sha256(payload.encode("utf-8")).hexdigest()
        with self.assertRaisesRegex(ValueError, "payload metadata"):
            replace(locked, payload=payload, content_digest=digest)



if __name__ == "__main__":
    unittest.main()
