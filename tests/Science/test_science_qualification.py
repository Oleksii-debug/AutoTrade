import unittest

from mvp.autotrade_mvp.science_qualification import (
    QualificationGate,
    ScientificQualificationInput,
    qualify_scientific_learning,
)


H = "sha256:" + "a" * 64
P = "sha256:" + "c" * 64


def gate(name, status="PASS", reasons=()):
    hashes = (H, P) if name == "retention" else (H,)
    return QualificationGate(name, status, hashes, H, H, H, tuple(reasons))


def complete_gates(**statuses):
    names = ("protocol", "leakage", "holdout", "retention", "promotion", "ablation", "uncertainty", "forward_evidence")
    return tuple(gate(name, statuses.get(name, "PASS"), (("X." + name.upper()),) if statuses.get(name) == "FAIL" else ()) for name in names)


def evidence(
    gates=None,
    claim="NONE",
    holdout_used=False,
    future_used=False,
    population_coverage_hash=P,
):
    return ScientificQualificationInput(
        H,
        H,
        H,
        complete_gates() if gates is None else tuple(gates),
        claim,
        holdout_used,
        future_used,
        population_coverage_hash,
    )


def _trusted_evidence_verifier(_gate):
    return True


def qualify(value):
    return qualify_scientific_learning(
        value,
        evidence_verifier=_trusted_evidence_verifier,
    )


class ScientificQualificationTests(unittest.TestCase):
    def test_self_asserted_gate_hashes_are_inconclusive_without_verifier(self):
        result = qualify_scientific_learning(
            evidence(claim="ECONOMIC_EDGE_QUALIFIED")
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertFalse(result.economic_claim_accepted)
        self.assertIn(
            "SCIENCE.EVIDENCE_UNVERIFIED:forward_evidence",
            result.reason_codes,
        )
        self.assertIn("SCIENCE.CLAIM_EXCEEDS_EVIDENCE", result.reason_codes)

    def test_failed_or_raising_evidence_verifier_never_yields_pass(self):
        value = evidence()
        selective = qualify_scientific_learning(
            value,
            evidence_verifier=lambda gate: gate.gate_id != "holdout",
        )
        self.assertEqual(selective.status, "INCONCLUSIVE")
        self.assertIn(
            "SCIENCE.EVIDENCE_UNVERIFIED:holdout",
            selective.reason_codes,
        )

        def broken(_gate):
            raise RuntimeError("artifact store unavailable")

        broken_result = qualify_scientific_learning(
            value,
            evidence_verifier=broken,
        )
        self.assertEqual(broken_result.status, "INCONCLUSIVE")
        self.assertFalse(broken_result.economic_claim_accepted)

    def test_all_independent_gates_pass_without_granting_authority(self):
        result = qualify(evidence(claim="ECONOMIC_EDGE_QUALIFIED"))
        self.assertEqual(result.status, "PASS")
        self.assertTrue(result.economic_claim_accepted)
        self.assertFalse(result.release_or_trading_authority)
        self.assertTrue(result.qualification_id.startswith("science-"))

    def test_missing_population_coverage_is_inconclusive(self):
        result = qualify(evidence(population_coverage_hash=None))
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertFalse(result.economic_claim_accepted)
        self.assertIn("SCIENCE.POPULATION_COVERAGE_MISSING", result.reason_codes)

    def test_population_coverage_must_be_bound_to_retention_gate(self):
        gates = list(complete_gates())
        gates[gates.index(next(g for g in gates if g.gate_id == "retention"))] = (
            QualificationGate("retention", "PASS", (H,), H, H, H)
        )
        result = qualify(evidence(gates))
        self.assertEqual(result.status, "FAIL")
        self.assertFalse(result.economic_claim_accepted)
        self.assertIn(
            "SCIENCE.POPULATION_COVERAGE_NOT_BOUND_TO_RETENTION",
            result.reason_codes,
        )

    def test_leakage_sentinel_failure_is_hard_fail_even_if_other_gates_pass(self):
        result = qualify(evidence(complete_gates(leakage="FAIL")))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("X.LEAKAGE", result.reason_codes)

    def test_holdout_reuse_for_tuning_is_hard_fail(self):
        result = qualify(evidence(holdout_used=True))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SCIENCE.HOLDOUT_MISUSE", result.reason_codes)

    def test_retention_regression_is_hard_fail(self):
        result = qualify(evidence(complete_gates(retention="FAIL")))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("X.RETENTION", result.reason_codes)

    def test_missing_uncertainty_evidence_is_inconclusive_not_pass(self):
        gates = [g for g in complete_gates() if g.gate_id != "uncertainty"]
        result = qualify(evidence(gates))
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertFalse(result.economic_claim_accepted)
        self.assertIn("SCIENCE.MISSING_GATE:uncertainty", result.reason_codes)

    def test_visually_good_backtest_cannot_override_invalid_protocol(self):
        result = qualify(evidence(complete_gates(protocol="FAIL"), claim="RESEARCH_CANDIDATE"))
        self.assertEqual(result.status, "FAIL")
        self.assertFalse(result.economic_claim_accepted)
        self.assertIn("SCIENCE.CLAIM_EXCEEDS_EVIDENCE", result.reason_codes)

    def test_edge_claim_requires_forward_evidence(self):
        result = qualify(evidence(complete_gates(forward_evidence="INCONCLUSIVE"), claim="ECONOMIC_EDGE_QUALIFIED"))
        self.assertEqual(result.status, "FAIL")
        self.assertFalse(result.economic_claim_accepted)
        self.assertIn("SCIENCE.CLAIM_EXCEEDS_EVIDENCE", result.reason_codes)

    def test_future_information_used_for_routing_is_hard_fail(self):
        result = qualify(evidence(future_used=True))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SCIENCE.FUTURE_LEAKAGE", result.reason_codes)

    def test_gate_evidence_must_bind_same_candidate_protocol_and_snapshot(self):
        bad = QualificationGate("leakage", "PASS", (H,), "sha256:" + "b" * 64, H, H)
        gates = [g for g in complete_gates() if g.gate_id != "leakage"] + [bad]
        result = qualify(evidence(gates))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("SCIENCE.EVIDENCE_BINDING_MISMATCH:leakage", result.reason_codes)

    def test_exact_hash_identity_required(self):
        with self.assertRaises(ValueError):
            ScientificQualificationInput("candidate", H, H, complete_gates(), "NONE")
        with self.assertRaises(ValueError):
            ScientificQualificationInput("sha256:" + "A" * 64, H, H, complete_gates(), "NONE")

    def test_control_flags_require_actual_booleans(self):
        with self.assertRaises(TypeError):
            ScientificQualificationInput(H, H, H, complete_gates(), "NONE", 1, False)
        with self.assertRaises(TypeError):
            ScientificQualificationInput(H, H, H, complete_gates(), "NONE", False, "false")

    def test_qualification_identity_binds_exact_gate_evidence(self):
        baseline = qualify(evidence())
        changed = list(complete_gates())
        changed[0] = QualificationGate(
            "protocol",
            "PASS",
            ("sha256:" + "b" * 64,),
            H,
            H,
            H,
        )
        revised = qualify(evidence(changed))
        self.assertEqual(baseline.status, revised.status)
        self.assertNotEqual(baseline.qualification_id, revised.qualification_id)

    def test_unknown_gate_cannot_be_silently_ignored(self):
        with self.assertRaises(ValueError):
            QualificationGate("future_magic_gate", "PASS", (H,), H, H, H)

    def test_gate_collections_are_strictly_typed(self):
        with self.assertRaises(ValueError):
            QualificationGate("protocol", "PASS", [H], H, H, H)
        with self.assertRaises(ValueError):
            QualificationGate("protocol", "FAIL", (H,), H, H, H, ("",))
        with self.assertRaises(TypeError):
            ScientificQualificationInput(H, H, H, list(complete_gates()), "NONE")


if __name__ == "__main__":
    unittest.main()
