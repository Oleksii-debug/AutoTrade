from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp import qualification_attestation as qualification_trust

from mvp.autotrade_mvp.qualification_attestation import (
    EvidenceArtifactRef,
    SignedQualificationAttestation,
)
from mvp.autotrade_mvp.science_qualification import (
    QualificationGate,
    ScientificQualificationInput,
    _gate_assertion_requirement,
    _input_assertion_requirement,
    qualify_scientific_learning,
)
from mvp.tests.test_qualification_attestation import (
    EVIDENCE_SHA,
    SOURCE,
    _initialize_exact_source_policy_repo,
    attestation,
    evidence_ref,
    policy,
    publish,
    root,
    sign,
)


H = EVIDENCE_SHA
_POPULATION_BYTES = b"science population coverage"
P = "sha256:" + sha256(_POPULATION_BYTES).hexdigest()
_POPULATION_ID = str(uuid5(NAMESPACE_URL, "science-population-coverage"))


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
        SOURCE,
    )


def _signed_science_receipt(value, *, trust_root=None):
    trust_root = trust_root or root()
    trust_policy = policy(trust_root)
    requirement_ids = [
        "scientific-learning-qualification",
        _input_assertion_requirement(value),
        f"candidate/{value.candidate_hash}",
        f"input/{value.input_snapshot_hash}",
        *(f"gate/{name}" for name in (
            "protocol",
            "leakage",
            "holdout",
            "retention",
            "promotion",
            "ablation",
            "uncertainty",
            "forward_evidence",
        )),
        *(_gate_assertion_requirement(gate) for gate in value.gates),
    ]
    if value.population_coverage_hash is not None:
        requirement_ids.append(
            f"population/{value.population_coverage_hash}"
        )
    signed = attestation(
        trust_root,
        domain="SCIENCE",
        gate="ECONOMIC_EDGE",
        package_id="WP-56",
        protocol_id=value.frozen_protocol_hash,
        protocol_version="1.0.0",
        requirement_ids=tuple(requirement_ids),
        source_sha=value.source_sha,
        evidence_refs=(
            replace(evidence_ref(), source_sha=value.source_sha),
            EvidenceArtifactRef(
                artifact_id=_POPULATION_ID,
                sha256=P,
                media_type="application/vnd.autotrade.qualification-evidence",
                evidence_kind="SCIENCE_POPULATION_COVERAGE",
                source_sha=value.source_sha,
            ),
        ),
        result="PASS",
        release_artifact_id=None,
        release_artifact_sha256=None,
    )
    return (
        SignedQualificationAttestation(signed, sign(signed)),
        trust_policy,
    )


def _qualify_signed(value, receipt, trust_policy, store):
    with patch.object(
        qualification_trust,
        "load_canonical_qualification_trust_policy",
        return_value=trust_policy,
    ):
        return qualify_scientific_learning(
            value,
            qualification_receipt=receipt,
            evidence_store=store,
        )


def qualify(value):
    receipt, trust_policy = _signed_science_receipt(value)
    with TemporaryDirectory() as directory:
        store = ArtifactStore(directory)
        publish(store)
        store.publish_bytes(
            artifact_id=_POPULATION_ID,
            data=_POPULATION_BYTES,
            media_type="application/vnd.autotrade.qualification-evidence",
            rights={"storage": True, "export": False},
            source_refs=[f"git:{SOURCE}"],
            metadata={"evidence_kind": "SCIENCE_POPULATION_COVERAGE"},
        )
        return _qualify_signed(value, receipt, trust_policy, store)


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

    def test_caller_lambda_true_cannot_self_approve_science(self):
        result = qualify_scientific_learning(
            evidence(claim="ECONOMIC_EDGE_QUALIFIED"),
            evidence_verifier=lambda _gate: True,
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertFalse(result.economic_claim_accepted)
        self.assertIn(
            "SCIENCE.EVIDENCE_UNVERIFIED:forward_evidence",
            result.reason_codes,
        )

    def test_caller_selected_trust_policy_is_rejected(self):
        value = evidence(claim="ECONOMIC_EDGE_QUALIFIED")
        receipt, trust_policy = _signed_science_receipt(value)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            store.publish_bytes(
                artifact_id=_POPULATION_ID,
                data=_POPULATION_BYTES,
                media_type="application/vnd.autotrade.qualification-evidence",
                rights={"storage": True, "export": False},
                source_refs=[f"git:{SOURCE}"],
                metadata={"evidence_kind": "SCIENCE_POPULATION_COVERAGE"},
            )
            result = qualify_scientific_learning(
                value,
                qualification_receipt=receipt,
                qualification_policy=trust_policy,
                evidence_store=store,
                expected_policy_id=trust_policy.policy_id,
                expected_policy_version=trust_policy.policy_version,
            )
        self.assertEqual(result.status, "FAIL")
        self.assertFalse(result.economic_claim_accepted)
        self.assertIn(
            "SCIENCE.CALLER_SELECTED_TRUST_POLICY_FORBIDDEN",
            result.reason_codes,
        )

    def test_dirty_hostile_trust_policy_cannot_authorize_science_pass(self):
        canonical_root = root()
        hostile_root = replace(
            canonical_root,
            producer_id="candidate.self",
        )
        canonical_policy = policy(canonical_root)
        hostile_policy = policy(hostile_root)
        self.assertNotEqual(canonical_policy.policy_id, hostile_policy.policy_id)

        with TemporaryDirectory() as repository_directory:
            source_sha, policy_path = _initialize_exact_source_policy_repo(
                Path(repository_directory),
                canonical_policy,
            )
            policy_path.write_text(
                json.dumps(
                    qualification_trust.qualification_trust_policy_payload(
                        hostile_policy
                    ),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            value = replace(
                evidence(claim="ECONOMIC_EDGE_QUALIFIED"),
                source_sha=source_sha,
            )
            receipt, _ = _signed_science_receipt(
                value,
                trust_root=hostile_root,
            )

            with TemporaryDirectory() as evidence_directory:
                store = ArtifactStore(evidence_directory)
                publish(store, source=source_sha)
                store.publish_bytes(
                    artifact_id=_POPULATION_ID,
                    data=_POPULATION_BYTES,
                    media_type="application/vnd.autotrade.qualification-evidence",
                    rights={"storage": True, "export": False},
                    source_refs=[f"git:{source_sha}"],
                    metadata={
                        "evidence_kind": "SCIENCE_POPULATION_COVERAGE"
                    },
                )
                with patch.object(
                    qualification_trust,
                    "_QUALIFICATION_TRUST_REPOSITORY_ROOT",
                    Path(repository_directory),
                ):
                    result = qualify_scientific_learning(
                        value,
                        qualification_receipt=receipt,
                        evidence_store=store,
                    )

        self.assertEqual(result.status, "FAIL")
        self.assertFalse(result.economic_claim_accepted)
        self.assertIn(
            "SCIENCE.INDEPENDENT_ATTESTATION_INVALID",
            result.reason_codes,
        )

    def test_self_asserted_research_candidate_is_inconclusive(self):
        result = qualify_scientific_learning(
            evidence(claim="RESEARCH_CANDIDATE")
        )
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertFalse(result.economic_claim_accepted)
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

    def test_signed_receipt_must_cover_population_and_gate_evidence_digests(self):
        value = evidence(
            population_coverage_hash="sha256:" + "f" * 64,
        )
        receipt, trust_policy = _signed_science_receipt(value)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            store.publish_bytes(
                artifact_id=_POPULATION_ID,
                data=_POPULATION_BYTES,
                media_type="application/vnd.autotrade.qualification-evidence",
                rights={"storage": True, "export": False},
                source_refs=[f"git:{SOURCE}"],
                metadata={"evidence_kind": "SCIENCE_POPULATION_COVERAGE"},
            )
            result = _qualify_signed(
                value, receipt, trust_policy, store
            )
        self.assertEqual(result.status, "FAIL")
        self.assertFalse(result.economic_claim_accepted)
        self.assertIn(
            "SCIENCE.INDEPENDENT_ATTESTATION_EVIDENCE_MISMATCH",
            result.reason_codes,
        )

    def test_signed_science_pass_retains_trust_identity(self):
        result = qualify(evidence(claim="ECONOMIC_EDGE_QUALIFIED"))
        self.assertEqual(result.status, "PASS")
        self.assertIsNotNone(result.qualification_attestation_id)
        self.assertTrue(
            result.qualification_attestation_digest.startswith("sha256:")
        )
        self.assertTrue(result.qualification_policy_id.startswith("sha256:"))
        self.assertTrue(result.qualification_trust_root_id.startswith("sha256:"))

    def test_leakage_sentinel_failure_is_hard_fail_even_if_other_gates_pass(self):
        result = qualify(evidence(complete_gates(leakage="FAIL")))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("X.LEAKAGE", result.reason_codes)

    def test_signed_failed_gate_cannot_be_relabelled_pass(self):
        original = evidence(complete_gates(leakage="FAIL"))
        receipt, trust_policy = _signed_science_receipt(original)

        tampered_gates = tuple(
            gate("leakage", "PASS")
            if item.gate_id == "leakage"
            else item
            for item in original.gates
        )
        tampered = ScientificQualificationInput(
            original.candidate_hash,
            original.frozen_protocol_hash,
            original.input_snapshot_hash,
            tampered_gates,
            "ECONOMIC_EDGE_QUALIFIED",
            False,
            False,
            original.population_coverage_hash,
            original.source_sha,
        )

        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            store.publish_bytes(
                artifact_id=_POPULATION_ID,
                data=_POPULATION_BYTES,
                media_type="application/vnd.autotrade.qualification-evidence",
                rights={"storage": True, "export": False},
                source_refs=[f"git:{SOURCE}"],
                metadata={"evidence_kind": "SCIENCE_POPULATION_COVERAGE"},
            )
            result = _qualify_signed(
                tampered, receipt, trust_policy, store
            )

        self.assertEqual(result.status, "FAIL")
        self.assertFalse(result.economic_claim_accepted)
        self.assertIn(
            "SCIENCE.INDEPENDENT_ATTESTATION_BINDING_MISMATCH",
            result.reason_codes,
        )

    def test_signed_receipt_cannot_clean_caller_controlled_contamination(self):
        original = evidence(
            claim="NONE",
            holdout_used=True,
            future_used=True,
        )
        receipt, trust_policy = _signed_science_receipt(original)
        tampered = evidence(
            claim="ECONOMIC_EDGE_QUALIFIED",
            holdout_used=False,
            future_used=False,
        )

        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            store.publish_bytes(
                artifact_id=_POPULATION_ID,
                data=_POPULATION_BYTES,
                media_type="application/vnd.autotrade.qualification-evidence",
                rights={"storage": True, "export": False},
                source_refs=[f"git:{SOURCE}"],
                metadata={"evidence_kind": "SCIENCE_POPULATION_COVERAGE"},
            )
            result = _qualify_signed(
                tampered, receipt, trust_policy, store
            )

        self.assertEqual(result.status, "FAIL")
        self.assertFalse(result.economic_claim_accepted)
        self.assertIn(
            "SCIENCE.INDEPENDENT_ATTESTATION_BINDING_MISMATCH",
            result.reason_codes,
        )

    def test_signed_receipt_cannot_escalate_requested_economic_claim(self):
        original = evidence(claim="NONE")
        receipt, trust_policy = _signed_science_receipt(original)
        tampered = evidence(claim="ECONOMIC_EDGE_QUALIFIED")

        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            store.publish_bytes(
                artifact_id=_POPULATION_ID,
                data=_POPULATION_BYTES,
                media_type="application/vnd.autotrade.qualification-evidence",
                rights={"storage": True, "export": False},
                source_refs=[f"git:{SOURCE}"],
                metadata={"evidence_kind": "SCIENCE_POPULATION_COVERAGE"},
            )
            result = _qualify_signed(
                tampered, receipt, trust_policy, store
            )

        self.assertEqual(result.status, "FAIL")
        self.assertFalse(result.economic_claim_accepted)
        self.assertIn(
            "SCIENCE.INDEPENDENT_ATTESTATION_BINDING_MISMATCH",
            result.reason_codes,
        )

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
        self.assertEqual(baseline.status, "PASS")
        self.assertEqual(revised.status, "FAIL")
        self.assertIn(
            "SCIENCE.INDEPENDENT_ATTESTATION_EVIDENCE_MISMATCH",
            revised.reason_codes,
        )
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
