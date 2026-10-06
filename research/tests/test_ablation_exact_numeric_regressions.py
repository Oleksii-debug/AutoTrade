from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext
from fractions import Fraction
from hashlib import sha256
import json
import unittest

from autotrade_research.evaluation.ablation import (
    AblationEvidenceBundle,
    AblationOutcome,
    AblationPair,
    CausalInputEvidence,
    build_ablation_evidence_bundle,
    evaluate_incremental_value,
    verify_ablation_evidence_bundle,
)


CUT = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _digest(label: str) -> str:
    return "sha256:" + sha256(label.encode("utf-8")).hexdigest()


def _evidence(case_id: str, component: str) -> CausalInputEvidence:
    return CausalInputEvidence(
        evidence_id=f"{case_id}:{component}",
        content_digest=_digest(f"{case_id}:{component}:content"),
        component_id=component,
        available_utc=CUT,
    )


def _pair(
    case_id: str,
    net_delta,
    *,
    full_utility=None,
    full_cost="0",
    ablated_utility="0",
    ablated_cost="0",
) -> AblationPair:
    if full_utility is None:
        full_utility = net_delta
    base = _evidence(case_id, "base")
    target = _evidence(case_id, "agent")
    common = dict(
        case_id=case_id,
        input_fingerprint=_digest(f"{case_id}:input"),
        elapsed_ms=10,
        deadline_ms=100,
        input_cutoff_utc=CUT,
        decision_utc=CUT,
        outcome_available_utc=CUT + timedelta(hours=1),
        population_unit_id=case_id,
    )
    full = AblationOutcome(
        **common,
        variant="FULL",
        utility=Decimal(str(full_utility)),
        cost=Decimal(str(full_cost)),
        components=("base", "agent"),
        input_evidence=(base, target),
    )
    ablated = AblationOutcome(
        **common,
        variant="ABLATED",
        utility=Decimal(str(ablated_utility)),
        cost=Decimal(str(ablated_cost)),
        components=("base",),
        input_evidence=(base,),
    )
    return AblationPair("agent", full, ablated)


class ExactAblationNumericRegressions(unittest.TestCase):
    def test_large_equal_utilities_preserve_true_negative_net_delta(self):
        pairs = [
            _pair(
                "negative-large-1",
                "-1",
                full_utility="1E100",
                full_cost="2",
                ablated_utility="1E100",
                ablated_cost="1",
            ),
            _pair(
                "negative-large-2",
                "-1",
                full_utility="1E100",
                full_cost="2",
                ablated_utility="1E100",
                ablated_cost="1",
            ),
        ]

        self.assertEqual(
            [item.net_value_delta for item in pairs],
            [Decimal("-1"), Decimal("-1")],
        )
        result = evaluate_incremental_value(
            "agent",
            pairs,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
            uncertainty_multiplier=Decimal("0"),
        )

        self.assertEqual(result.status, "FAIL")
        self.assertIsNotNone(result.decision_exact)
        self.assertEqual(result.decision_exact.mean, Fraction(-1, 1))
        self.assertEqual(result.decision_exact.threshold_delta, Fraction(-1, 1))
        self.assertEqual(result.decision_exact.status, "FAIL")

    def test_large_equal_utilities_preserve_true_positive_net_delta(self):
        pairs = [
            _pair(
                "positive-large-1",
                "1",
                full_utility="1E100",
                full_cost="1",
                ablated_utility="1E100",
                ablated_cost="2",
            ),
            _pair(
                "positive-large-2",
                "1",
                full_utility="1E100",
                full_cost="1",
                ablated_utility="1E100",
                ablated_cost="2",
            ),
        ]

        result = evaluate_incremental_value(
            "agent",
            pairs,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
            uncertainty_multiplier=Decimal("0"),
        )

        self.assertEqual(result.status, "PASS")
        self.assertIsNotNone(result.decision_exact)
        self.assertEqual(result.decision_exact.mean, Fraction(1, 1))
        self.assertEqual(result.decision_exact.threshold_delta, Fraction(1, 1))

    def test_exact_uncertainty_boundary_is_inclusive_and_one_quantum_below_fails(self):
        pairs = [
            _pair("boundary-0", "0"),
            _pair("boundary-2", "2"),
        ]

        boundary = evaluate_incremental_value(
            "agent",
            pairs,
            minimum_pairs=2,
            required_lower_bound=Decimal("0"),
            uncertainty_multiplier=Decimal("1"),
        )
        self.assertEqual(boundary.status, "PASS")
        self.assertIsNotNone(boundary.decision_exact)
        self.assertEqual(boundary.decision_exact.mean, Fraction(1, 1))
        self.assertEqual(boundary.decision_exact.sample_variance, Fraction(2, 1))
        self.assertEqual(boundary.decision_exact.lhs, Fraction(1, 1))
        self.assertEqual(boundary.decision_exact.rhs, Fraction(1, 1))

        below = evaluate_incremental_value(
            "agent",
            pairs,
            minimum_pairs=2,
            required_lower_bound=Decimal("0.000000000000000001"),
            uncertainty_multiplier=Decimal("1"),
        )
        self.assertEqual(below.status, "FAIL")
        self.assertIsNotNone(below.decision_exact)
        self.assertLess(below.decision_exact.lhs, below.decision_exact.rhs)

    def test_exact_verdict_is_independent_of_ambient_decimal_context(self):
        pairs = [
            _pair("context-a", "0.333333333333333333"),
            _pair("context-b", "0.666666666666666667"),
            _pair("context-c", "-0.125"),
        ]

        results = []
        for precision, rounding in (
            (6, ROUND_DOWN),
            (10, ROUND_UP),
            (28, ROUND_DOWN),
            (80, ROUND_UP),
        ):
            with localcontext() as context:
                context.prec = precision
                context.rounding = rounding
                results.append(
                    evaluate_incremental_value(
                        "agent",
                        pairs,
                        minimum_pairs=3,
                        required_lower_bound=Decimal("0"),
                        uncertainty_multiplier=Decimal("1.25"),
                    )
                )

        first = results[0]
        for result in results[1:]:
            self.assertEqual(result.status, first.status)
            self.assertEqual(result.decision_exact, first.decision_exact)
            self.assertEqual(result.reporting_status, first.reporting_status)
            self.assertEqual(
                result.mean_net_incremental_value,
                first.mean_net_incremental_value,
            )
            self.assertEqual(result.sample_stddev, first.sample_stddev)
            self.assertEqual(result.lower_bound, first.lower_bound)

    def test_nonterminating_mean_keeps_exact_one_third_decision(self):
        pairs = [
            _pair("third-a", "1"),
            _pair("third-b", "0"),
            _pair("third-c", "0"),
        ]

        result = evaluate_incremental_value(
            "agent",
            pairs,
            minimum_pairs=3,
            required_lower_bound=Decimal("0"),
            uncertainty_multiplier=Decimal("0"),
        )

        self.assertEqual(result.status, "PASS")
        self.assertIsNotNone(result.decision_exact)
        self.assertEqual(result.decision_exact.mean, Fraction(1, 3))
        self.assertEqual(result.decision_exact.status, "PASS")
        self.assertEqual(result.reporting_status, "AVAILABLE")
        self.assertIsNotNone(result.mean_net_incremental_value)

    def test_over_envelope_utility_fails_before_evaluation(self):
        with self.assertRaisesRegex(ValueError, "resource envelope"):
            _pair(
                "oversized-utility",
                "0",
                full_utility="1E10000",
                full_cost="0",
                ablated_utility="0",
                ablated_cost="0",
            )

    def test_locked_bundle_binds_exact_decision_not_rehashed_reporting_text(self):
        pairs = [
            _pair("bundle-a", "1"),
            _pair("bundle-b", "0"),
            _pair("bundle-c", "0"),
        ]
        bundle = build_ablation_evidence_bundle(
            "agent",
            pairs,
            source_revision="1" * 40,
            protocol_digest="sha256:" + "2" * 64,
            dataset_digest="sha256:" + "3" * 64,
            minimum_pairs=3,
            required_lower_bound=Decimal("0"),
            uncertainty_multiplier=Decimal("0"),
        )
        self.assertTrue(verify_ablation_evidence_bundle(bundle, pairs))
        self.assertEqual(bundle.evaluation.decision_exact.mean, Fraction(1, 3))

        decoded = json.loads(bundle.payload)
        decoded["evaluation"]["mean_net_incremental_value"] = "999"
        tampered_payload = json.dumps(
            decoded,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        tampered_digest = (
            "sha256:" + sha256(tampered_payload.encode("utf-8")).hexdigest()
        )

        with self.assertRaisesRegex(
            ValueError,
            "payload evaluation does not match bundle evaluation",
        ):
            replace(
                bundle,
                payload=tampered_payload,
                content_digest=tampered_digest,
            )


if __name__ == "__main__":
    unittest.main()
