from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp.qualification_attestation import (
    AcceptedQualificationAttestation,
    EvidenceArtifactRef,
    QualificationAttestation,
    QualificationScope,
    QualificationTrustError,
    QualificationTrustPolicy,
    SignedQualificationAttestation,
    TrustRoot,
)
from mvp.autotrade_mvp.science_qualification import (
    QualificationGate,
    ScientificQualificationInput,
    ScientificQualificationTrustContext,
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


SOURCE = "a" * 40
_PROTOCOL_ID = "science-qualification-v1"
_PROTOCOL_VERSION = "1.0.0"
_TEST_MODULUS = "8" + "0" * 511


def _trust_material(value):
    root = TrustRoot(
        producer_id="science.qualifier",
        verifier_id="science.trust.verifier",
        public_modulus_hex=_TEST_MODULUS,
        public_exponent=65537,
        allowed_scopes=tuple(
            QualificationScope("SCIENCE", gate.gate_id)
            for gate in value.gates
        ),
        valid_from="2026-09-01T00:00:00Z",
    )
    policy = QualificationTrustPolicy(
        policy_version="2026.09",
        roots=(root,),
    )
    receipts = []
    for gate_value in value.gates:
        refs = tuple(
            EvidenceArtifactRef(
                artifact_id=str(
                    uuid5(
                        NAMESPACE_URL,
                        f"science:{gate_value.gate_id}:{index}:{digest}",
                    )
                ),
                sha256=digest,
                media_type="application/vnd.autotrade.science-evidence",
                evidence_kind="SCIENCE_GATE_EVIDENCE",
                source_sha=SOURCE,
            )
            for index, digest in enumerate(gate_value.evidence_hashes)
        )
        attestation = QualificationAttestation(
            attestation_id=str(
                uuid5(NAMESPACE_URL, f"science-attestation:{gate_value.gate_id}")
            ),
            source_sha=SOURCE,
            domain="SCIENCE",
            gate=gate_value.gate_id,
            package_id="WP-56",
            protocol_id=_PROTOCOL_ID,
            protocol_version=_PROTOCOL_VERSION,
            requirement_ids=(f"science.gate.{gate_value.gate_id}",),
            evidence_refs=refs,
            producer_id=root.producer_id,
            verifier_id=root.verifier_id,
            trust_root_id=root.root_id,
            runner_id="science-runner",
            harness_version="1.0.0",
            started_at="2026-09-25T08:00:00Z",
            completed_at="2026-09-25T08:01:00Z",
            signed_at="2026-09-25T08:02:00Z",
            result=gate_value.status,
        )
        receipts.append(
            SignedQualificationAttestation(attestation, "AQ==")
        )
    return root, policy, tuple(receipts)


def _accepted(receipt, policy):
    return AcceptedQualificationAttestation(
        attestation_id=receipt.attestation.attestation_id,
        attestation_digest=receipt.attestation.content_digest,
        policy_id=policy.policy_id,
        trust_root_id=receipt.attestation.trust_root_id,
        result=receipt.attestation.result,
        source_sha=receipt.attestation.source_sha,
        domain=receipt.attestation.domain,
        gate=receipt.attestation.gate,
        package_id=receipt.attestation.package_id,
        protocol_id=receipt.attestation.protocol_id,
        protocol_version=receipt.attestation.protocol_version,
        requirement_id=receipt.attestation.requirement_ids[0],
        release_artifact_id=None,
        release_artifact_sha256=None,
    )


def qualify(value, *, invalid_gate=None):
    _root, policy, receipts = _trust_material(value)
    with TemporaryDirectory() as directory:
        context = ScientificQualificationTrustContext(
            source_sha=SOURCE,
            protocol_id=_PROTOCOL_ID,
            protocol_version=_PROTOCOL_VERSION,
            policy=policy,
            expected_policy_id=policy.policy_id,
            expected_policy_version=policy.policy_version,
            evidence_store=ArtifactStore(directory),
            receipts=receipts,
        )

        def verifier(receipt, **_kwargs):
            if invalid_gate is not None and receipt.attestation.gate == invalid_gate.upper():
                raise QualificationTrustError("injected invalid attestation")
            return _accepted(receipt, policy)

        with patch(
            "mvp.autotrade_mvp.science_qualification.verify_qualification_attestation",
            side_effect=verifier,
        ):
            return qualify_scientific_learning(
                value,
                trust_context=context,
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

    def test_invalid_signed_forward_attestation_is_inconclusive(self):
        result = qualify(
            evidence(claim="ECONOMIC_EDGE_QUALIFIED"),
            invalid_gate="forward_evidence",
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertFalse(result.economic_claim_accepted)
        self.assertIn(
            "SCIENCE.EVIDENCE_UNVERIFIED:forward_evidence",
            result.reason_codes,
        )
        self.assertIn(
            "SCIENCE.ATTESTATION_INVALID:forward_evidence",
            result.reason_codes,
        )

    def test_self_asserted_research_candidate_is_inconclusive(self):
        result = qualify_scientific_learning(
            evidence(claim="RESEARCH_CANDIDATE")
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertFalse(result.economic_claim_accepted)
        self.assertIn("SCIENCE.CLAIM_EXCEEDS_EVIDENCE", result.reason_codes)

    def test_caller_boolean_verifier_is_not_a_supported_terminal_api(self):
        with self.assertRaises(TypeError):
            qualify_scientific_learning(
                evidence(),
                evidence_verifier=lambda _gate: True,
            )

    def test_invalid_signed_gate_never_yields_pass(self):
        result = qualify(evidence(), invalid_gate="holdout")
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertFalse(result.economic_claim_accepted)
        self.assertIn(
            "SCIENCE.EVIDENCE_UNVERIFIED:holdout",
            result.reason_codes,
        )


    def test_all_independent_gates_pass_without_granting_authority(self):
        result = qualify(evidence(claim="ECONOMIC_EDGE_QUALIFIED"))
        self.assertEqual(result.status, "PASS")
        self.assertTrue(result.economic_claim_accepted)
        self.assertFalse(result.release_or_trading_authority)
        self.assertTrue(result.qualification_id.startswith("science-"))
        self.assertIsNotNone(result.qualification_policy_id)
        self.assertEqual(len(result.qualification_attestations), 8)

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

    def test_signed_receipt_must_cover_exact_gate_evidence_digest_set(self):
        value = evidence()
        _root, policy, receipts = _trust_material(value)
        target = next(item for item in receipts if item.attestation.gate == "HOLDOUT")
        wrong_ref = EvidenceArtifactRef(
            artifact_id=str(uuid5(NAMESPACE_URL, "wrong-science-evidence")),
            sha256="sha256:" + "b" * 64,
            media_type="application/vnd.autotrade.science-evidence",
            evidence_kind="SCIENCE_GATE_EVIDENCE",
            source_sha=SOURCE,
        )
        wrong_attestation = QualificationAttestation(
            **{
                **target.attestation.__dict__,
                "evidence_refs": (wrong_ref,),
            }
        )
        receipts = tuple(
            SignedQualificationAttestation(wrong_attestation, "AQ==")
            if item is target
            else item
            for item in receipts
        )
        with TemporaryDirectory() as directory:
            context = ScientificQualificationTrustContext(
                source_sha=SOURCE,
                protocol_id=_PROTOCOL_ID,
                protocol_version=_PROTOCOL_VERSION,
                policy=policy,
                expected_policy_id=policy.policy_id,
                expected_policy_version=policy.policy_version,
                evidence_store=ArtifactStore(directory),
                receipts=receipts,
            )
            result = qualify_scientific_learning(
                value,
                trust_context=context,
            )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertIn("SCIENCE.ATTESTATION_INVALID:holdout", result.reason_codes)

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
