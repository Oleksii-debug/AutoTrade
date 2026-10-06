from __future__ import annotations

import unittest
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp.qualification_attestation import (
    EvidenceArtifactRef,
    QualificationAttestation,
    SignedQualificationAttestation,
)
from mvp.autotrade_mvp.supply_chain_qualification import (
    ComponentEvidence,
    ModelDataRightsEvidence,
    SupplyChainEvidence,
    canonical_supply_chain_proof_bytes,
    parse_supply_chain_proof_bytes,
    supply_chain_subject_requirement,
)


SOURCE_SHA = "a" * 40
DIGEST_A = "sha256:" + "1" * 64
DIGEST_B = "sha256:" + "2" * 64
RELEASE_ID = str(uuid5(NAMESPACE_URL, "wp64-proof-release"))


def _id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, "wp64-proof:" + label))


def _component(label: str) -> ComponentEvidence:
    return ComponentEvidence(
        component_id=f"pkg:pypi/{label}@1.0",
        artifact_id=_id("component:" + label),
        version="1.0",
        declared_artifact_hash=DIGEST_A,
        observed_artifact_hash=DIGEST_A,
        source_revision="tag:v1.0",
        license_status="APPROVED",
        distribution_rights="APPROVED",
        advisory_status="CLEAR",
        notice_required=False,
        notice_present=False,
        reviewed_for_release_sha=SOURCE_SHA,
    )


def _rights(label: str) -> ModelDataRightsEvidence:
    return ModelDataRightsEvidence(
        artifact_id=_id("rights:" + label),
        artifact_hash=DIGEST_B,
        use_scope="redistribute",
        rights_status="APPROVED",
        reviewed_for_release_sha=SOURCE_SHA,
    )


def _evidence(*, reverse: bool) -> SupplyChainEvidence:
    components = (_component("a"), _component("b"))
    rights = (_rights("a"), _rights("b"))
    if reverse:
        components = tuple(reversed(components))
        rights = tuple(reversed(rights))
    ids = tuple(item.component_id for item in components)
    return SupplyChainEvidence(
        release_commit_sha=SOURCE_SHA,
        built_from_commit_sha=SOURCE_SHA,
        sbom_artifact_id=_id("sbom"),
        sbom_hash=DIGEST_A,
        provenance_artifact_id=_id("provenance"),
        provenance_hash=DIGEST_A,
        dependency_lock_artifact_id=_id("lock"),
        dependency_lock_hash=DIGEST_A,
        sbom_reviewed_for_release_sha=SOURCE_SHA,
        provenance_reviewed_for_release_sha=SOURCE_SHA,
        dependency_lock_reviewed_for_release_sha=SOURCE_SHA,
        distributed_component_ids=ids,
        sbom_component_ids=ids,
        components=components,
        model_data_rights=rights,
        release_artifact_id=RELEASE_ID,
        release_artifact_sha256=DIGEST_B,
    )


def _receipt() -> SignedQualificationAttestation:
    ref = EvidenceArtifactRef(
        artifact_id=_id("review-evidence"),
        sha256=DIGEST_A,
        media_type="application/json",
        evidence_kind="SUPPLY_CHAIN_REVIEW",
        source_sha=SOURCE_SHA,
    )
    attestation = QualificationAttestation(
        attestation_id=_id("attestation"),
        source_sha=SOURCE_SHA,
        domain="SUPPLY_CHAIN",
        gate="RELEASE",
        package_id="WP-64",
        protocol_id="supply-chain-review-v1",
        protocol_version="1.0.0",
        requirement_ids=("independent-supply-chain-review",),
        evidence_refs=(ref,),
        producer_id="independent.qualifier",
        verifier_id="autotrade.qualifier",
        trust_root_id=DIGEST_A,
        runner_id="runner-1",
        harness_version="1.0.0",
        started_at="2026-10-03T10:00:00Z",
        completed_at="2026-10-03T10:01:00Z",
        signed_at="2026-10-03T10:01:01Z",
        result="PASS",
        release_artifact_id=RELEASE_ID,
        release_artifact_sha256=DIGEST_B,
    )
    return SignedQualificationAttestation(attestation=attestation, signature_b64="AA==")


class SupplyChainProofCanonicalTests(unittest.TestCase):
    def test_semantically_equivalent_order_has_one_subject_and_one_proof_identity(self):
        left = _evidence(reverse=False)
        right = _evidence(reverse=True)
        receipt = _receipt()

        self.assertEqual(
            supply_chain_subject_requirement(left),
            supply_chain_subject_requirement(right),
        )
        left_bytes = canonical_supply_chain_proof_bytes(left, receipt)
        right_bytes = canonical_supply_chain_proof_bytes(right, receipt)
        self.assertEqual(left_bytes, right_bytes)

        parsed_evidence, parsed_receipt = parse_supply_chain_proof_bytes(left_bytes)
        self.assertEqual(
            canonical_supply_chain_proof_bytes(parsed_evidence, parsed_receipt),
            left_bytes,
        )

    def test_noncanonical_equivalent_json_bytes_fail_closed(self):
        raw = canonical_supply_chain_proof_bytes(_evidence(reverse=False), _receipt())
        with self.assertRaisesRegex(ValueError, "not canonical"):
            parse_supply_chain_proof_bytes(raw + b"\n")


if __name__ == "__main__":
    unittest.main()
