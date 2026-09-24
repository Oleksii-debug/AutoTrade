import unittest

from mvp.autotrade_mvp.science_qualification import (
    QualificationGate,
    ScientificQualificationInput,
    qualify_scientific_learning,
)


H = "sha256:" + "a" * 64


def gate(name, status="PASS", reasons=()):
    return QualificationGate(name, status, (H,), tuple(reasons))


def complete_gates(**statuses):
    names = ("protocol", "leakage", "holdout", "retention", "promotion", "ablation", "uncertainty", "forward_evidence")
    return tuple(
        gate(
            name,
            statuses.get(name, "PASS"),
            (("X." + name.upper()),) if statuses.get(name) == "FAIL" else (),
        )
        for name in names
    )


def evidence(gates=None, claim="NONE", holdout_used=False, future_used=False):
    return ScientificQualificationInput(
        H,
        H,
        H,
        complete_gates() if gates is None else tuple(gates),
        claim,
        holdout_used,
        future_used,
    )


class ScientificQualificationTests(unittest.TestCase):
    def test_all_independent_gates_pass_without_granting_authority(self):
        result = qualify_scientific_learning(evidence(claim="ECONOMIC_EDGE_QUALIFIED"))
        self.assertEqual(result.status, "PASS")
        self.assertTrue(result.economic_claim_accepted)
        self.assertFalse(result.release_or_trading_authority)
        self.assertTrue(result.qualification_id.startswith("science-"))

    def test_leakage_sentinel_failure_is_hard_fail_even_if_other_gates_pass(self):
        result = qualify_scientific_learning(evidence(complete_gates(leakage="FAIL")))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("X.LEAKAGE", result.reason_codes)

    def test_holdout_reuse_for_tuning_is_hard_fail(self):
        result = qualify_scientific_learning(evidence(holdout_used=True))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SCIENCE.HOLDOUT_MISUSE", result.reason_codes)

    def test_retention_regression_is_hard_fail(self):
        result = qualify_scientific_learning(evidence(complete_gates(retention="FAIL")))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("X.RETENTION", result.reason_codes)

    def test_missing_uncertainty_evidence_is_inconclusive_not_pass(self):
        gates = [g for g in complete_gates() if g.gate_id != "uncertainty"]
        result = qualify_scientific_learning(evidence(gates))
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertFalse(result.economic_claim_accepted)
        self.assertIn("SCIENCE.MISSING_GATE:uncertainty", result.reason_codes)

    def test_visually_good_backtest_cannot_override_invalid_protocol(self):
        result = qualify_scientific_learning(
            evidence(complete_gates(protocol="FAIL"), claim="RESEARCH_CANDIDATE")
        )
        self.assertEqual(result.status, "FAIL")
        self.assertFalse(result.economic_claim_accepted)
        self.assertIn("SCIENCE.CLAIM_EXCEEDS_EVIDENCE", result.reason_codes)

    def test_edge_claim_requires_forward_evidence(self):
        result = qualify_scientific_learning(
            evidence(
                complete_gates(forward_evidence="INCONCLUSIVE"),
                claim="ECONOMIC_EDGE_QUALIFIED",
            )
        )
        self.assertEqual(result.status, "FAIL")
        self.assertFalse(result.economic_claim_accepted)
        self.assertIn("SCIENCE.CLAIM_EXCEEDS_EVIDENCE", result.reason_codes)

    def test_future_information_used_for_routing_is_hard_fail(self):
        result = qualify_scientific_learning(evidence(future_used=True))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SCIENCE.FUTURE_LEAKAGE", result.reason_codes)

    def test_exact_hash_identity_required(self):
        with self.assertRaises(ValueError):
            ScientificQualificationInput("candidate", H, H, complete_gates(), "NONE")


if __name__ == "__main__":
    unittest.main()
