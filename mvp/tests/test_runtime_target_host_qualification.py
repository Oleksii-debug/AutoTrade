import base64
from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp.qualification_attestation import (
    AcceptedQualificationAttestation,
    EvidenceArtifactRef,
    QualificationAttestation,
    QualificationTrustError,
    SignedQualificationAttestation,
)
from mvp.autotrade_mvp.runtime_target_host_qualification import (
    BINDING_EVIDENCE_KIND,
    CAMPAIGN_EVIDENCE_KIND,
    DOMAIN,
    GATE,
    HOST_INVENTORY_EVIDENCE_KIND,
    INTERFERENCE_EVIDENCE_KIND,
    JSON_MEDIA_TYPE,
    PACKAGE_ID,
    PROTOCOL_ID,
    PROTOCOL_VERSION,
    REQUIREMENT_ID,
    RESOURCE_EVIDENCE_KIND,
    STALENESS_EVIDENCE_KIND,
    RuntimeTargetHostBinding,
    RuntimeTargetHostQualificationError,
    verify_runtime_target_host_qualification,
)


SOURCE = "a" * 40
SPEC = "sha256:" + "b" * 64
CONFIG = "sha256:" + "c" * 64
HOST = "sha256:" + "d" * 64
WORKLOAD = "sha256:" + "e" * 64
JOURNAL = "sha256:" + "f" * 64
CAMPAIGN = "sha256:" + "1" * 64
STALENESS = "sha256:" + "2" * 64
INTERFERENCE = "sha256:" + "3" * 64
RESOURCE = "sha256:" + "4" * 64
INVENTORY = "sha256:" + "5" * 64
ROOT = "sha256:" + "6" * 64
ATTESTATION_DIGEST = "sha256:" + "7" * 64
POLICY = "sha256:" + "8" * 64


_KIND_IDS = {
    BINDING_EVIDENCE_KIND: "00000000-0000-4000-8000-000000000001",
    CAMPAIGN_EVIDENCE_KIND: "00000000-0000-4000-8000-000000000002",
    STALENESS_EVIDENCE_KIND: "00000000-0000-4000-8000-000000000003",
    INTERFERENCE_EVIDENCE_KIND: "00000000-0000-4000-8000-000000000004",
    RESOURCE_EVIDENCE_KIND: "00000000-0000-4000-8000-000000000005",
    HOST_INVENTORY_EVIDENCE_KIND: "00000000-0000-4000-8000-000000000006",
}


def binding(**overrides) -> RuntimeTargetHostBinding:
    values = {
        "source_sha": SOURCE,
        "scenario_id": "target-host-primary",
        "spec_digest": SPEC,
        "configuration_hash": CONFIG,
        "host_fingerprint": HOST,
        "workload_profile_hash": WORKLOAD,
        "journal_store_identity_digest": JOURNAL,
        "campaign_evidence_sha256": CAMPAIGN,
        "staleness_evidence_sha256": STALENESS,
        "interference_evidence_sha256": INTERFERENCE,
        "resource_evidence_sha256": RESOURCE,
        "host_inventory_evidence_sha256": INVENTORY,
    }
    values.update(overrides)
    return RuntimeTargetHostBinding(**values)


def ref(kind: str, digest: str) -> EvidenceArtifactRef:
    return EvidenceArtifactRef(
        artifact_id=_KIND_IDS[kind],
        sha256=digest,
        media_type=JSON_MEDIA_TYPE,
        evidence_kind=kind,
        source_sha=SOURCE,
    )


def refs_for(raw_binding: bytes, *, resource_digest: str = RESOURCE):
    binding_digest = "sha256:" + sha256(raw_binding).hexdigest()
    return (
        ref(BINDING_EVIDENCE_KIND, binding_digest),
        ref(CAMPAIGN_EVIDENCE_KIND, CAMPAIGN),
        ref(STALENESS_EVIDENCE_KIND, STALENESS),
        ref(INTERFERENCE_EVIDENCE_KIND, INTERFERENCE),
        ref(RESOURCE_EVIDENCE_KIND, resource_digest),
        ref(HOST_INVENTORY_EVIDENCE_KIND, INVENTORY),
    )


def receipt(evidence_refs) -> SignedQualificationAttestation:
    attestation = QualificationAttestation(
        attestation_id="10000000-0000-4000-8000-000000000001",
        source_sha=SOURCE,
        domain=DOMAIN,
        gate=GATE,
        package_id=PACKAGE_ID,
        protocol_id=PROTOCOL_ID,
        protocol_version=PROTOCOL_VERSION,
        requirement_ids=(REQUIREMENT_ID,),
        evidence_refs=tuple(evidence_refs),
        producer_id="runtime-qualification-producer",
        verifier_id="runtime-qualification-verifier",
        trust_root_id=ROOT,
        runner_id="target-host-runner",
        harness_version="1.0.0",
        started_at="2026-10-03T00:00:00Z",
        completed_at="2026-10-03T00:01:00Z",
        signed_at="2026-10-03T00:01:01Z",
        result="PASS",
    )
    return SignedQualificationAttestation(
        attestation=attestation,
        signature_b64=base64.b64encode(b"not-verified-in-profile-unit-test").decode("ascii"),
    )


def accepted(evidence_refs, *, result="PASS", unresolved_limits=()):
    return AcceptedQualificationAttestation(
        attestation_id="10000000-0000-4000-8000-000000000001",
        attestation_digest=ATTESTATION_DIGEST,
        policy_id=POLICY,
        policy_version="1.0.0",
        trust_root_id=ROOT,
        result=result,
        source_sha=SOURCE,
        domain=DOMAIN,
        gate=GATE,
        package_id=PACKAGE_ID,
        protocol_id=PROTOCOL_ID,
        protocol_version=PROTOCOL_VERSION,
        requirement_id=REQUIREMENT_ID,
        requirement_ids=(REQUIREMENT_ID,),
        evidence_refs=tuple(evidence_refs),
        producer_id="runtime-qualification-producer",
        verifier_id="runtime-qualification-verifier",
        runner_id="target-host-runner",
        harness_version="1.0.0",
        started_at="2026-10-03T00:00:00Z",
        completed_at="2026-10-03T00:01:00Z",
        signed_at="2026-10-03T00:01:01Z",
        unresolved_limits=tuple(unresolved_limits),
        schema_version="1.0.0",
        verification_method="RSA_PKCS1V15_SHA256",
        release_artifact_id=None,
        release_artifact_sha256=None,
        attestation_json="{}",
        signature_b64="AA==",
    )


class RuntimeTargetHostQualificationTests(unittest.TestCase):
    def _verify(self, raw_binding, accepted_receipt, signed_receipt):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(f"{directory}/store")

            def reader(artifact_id):
                self.assertEqual(
                    artifact_id,
                    _KIND_IDS[BINDING_EVIDENCE_KIND],
                )
                return {}, raw_binding

            with patch(
                "mvp.autotrade_mvp.runtime_target_host_qualification."
                "verify_canonical_qualification_attestation",
                return_value=accepted_receipt,
            ) as canonical, patch(
                "mvp.autotrade_mvp.runtime_target_host_qualification."
                "trusted_authenticated_reader",
                return_value=reader,
            ):
                result = verify_runtime_target_host_qualification(
                    signed_receipt,
                    evidence_store=store,
                    evidence_root=directory,
                    expected_source_sha=SOURCE,
                    expected_scenario_id="target-host-primary",
                    expected_spec_digest=SPEC,
                    expected_configuration_hash=CONFIG,
                    expected_host_fingerprint=HOST,
                    expected_workload_profile_hash=WORKLOAD,
                    expected_journal_store_identity_digest=JOURNAL,
                )
            canonical.assert_called_once()
            kwargs = canonical.call_args.kwargs
            self.assertEqual(kwargs["expected_domain"], DOMAIN)
            self.assertEqual(kwargs["expected_gate"], GATE)
            self.assertEqual(kwargs["expected_package_id"], PACKAGE_ID)
            self.assertEqual(kwargs["expected_protocol_id"], PROTOCOL_ID)
            self.assertEqual(kwargs["expected_protocol_version"], PROTOCOL_VERSION)
            self.assertEqual(kwargs["expected_requirement_id"], REQUIREMENT_ID)
            return result

    def test_binding_round_trip_is_canonical(self):
        current = binding()
        self.assertEqual(
            RuntimeTargetHostBinding.parse(current.canonical_bytes()),
            current,
        )

    def test_binding_rejects_noncanonical_or_duplicate_json(self):
        current = binding()
        raw = current.canonical_bytes()
        self.assertRaises(
            RuntimeTargetHostQualificationError,
            RuntimeTargetHostBinding.parse,
            b'{"source_sha":"' + SOURCE.encode() + b'","source_sha":"' + SOURCE.encode() + b'"}',
        )
        self.assertRaises(
            RuntimeTargetHostQualificationError,
            RuntimeTargetHostBinding.parse,
            raw + b"\n",
        )

    def test_terminal_profile_cross_binds_all_required_artifacts(self):
        raw = binding().canonical_bytes()
        evidence_refs = refs_for(raw)
        result = self._verify(raw, accepted(evidence_refs), receipt(evidence_refs))
        self.assertEqual(result.source_sha, SOURCE)
        self.assertEqual(result.host_fingerprint, HOST)
        self.assertEqual(result.spec_digest, SPEC)
        self.assertEqual(
            result.evidence_sha256_by_kind[RESOURCE_EVIDENCE_KIND],
            RESOURCE,
        )

    def test_resource_digest_substitution_is_rejected_after_signature_acceptance(self):
        raw = binding().canonical_bytes()
        evidence_refs = refs_for(raw, resource_digest="sha256:" + "9" * 64)
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            RESOURCE_EVIDENCE_KIND,
        ):
            self._verify(raw, accepted(evidence_refs), receipt(evidence_refs))

    def test_missing_evidence_family_is_rejected(self):
        raw = binding().canonical_bytes()
        evidence_refs = tuple(
            value for value in refs_for(raw)
            if value.evidence_kind != STALENESS_EVIDENCE_KIND
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "evidence set mismatch",
        ):
            self._verify(raw, accepted(evidence_refs), receipt(evidence_refs))

    def test_wrong_target_host_identity_is_rejected(self):
        raw = binding(host_fingerprint="sha256:" + "9" * 64).canonical_bytes()
        evidence_refs = refs_for(raw)
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "identity does not match",
        ):
            self._verify(raw, accepted(evidence_refs), receipt(evidence_refs))

    def test_canonical_trust_failure_propagates_before_profile_reader(self):
        raw = binding().canonical_bytes()
        evidence_refs = refs_for(raw)
        signed = receipt(evidence_refs)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(f"{directory}/store")
            with patch(
                "mvp.autotrade_mvp.runtime_target_host_qualification."
                "verify_canonical_qualification_attestation",
                side_effect=QualificationTrustError("scope unavailable"),
            ), patch(
                "mvp.autotrade_mvp.runtime_target_host_qualification."
                "trusted_authenticated_reader",
            ) as reader:
                with self.assertRaisesRegex(QualificationTrustError, "scope unavailable"):
                    verify_runtime_target_host_qualification(
                        signed,
                        evidence_store=store,
                        evidence_root=directory,
                        expected_source_sha=SOURCE,
                        expected_scenario_id="target-host-primary",
                        expected_spec_digest=SPEC,
                        expected_configuration_hash=CONFIG,
                        expected_host_fingerprint=HOST,
                        expected_workload_profile_hash=WORKLOAD,
                        expected_journal_store_identity_digest=JOURNAL,
                    )
            reader.assert_not_called()

    def test_nonpass_cannot_be_promoted_to_terminal_target_host_qualification(self):
        raw = binding().canonical_bytes()
        evidence_refs = refs_for(raw)
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "requires signed PASS",
        ):
            self._verify(
                raw,
                accepted(evidence_refs, result="INCONCLUSIVE", unresolved_limits=("pressure-run-not-complete",)),
                receipt(evidence_refs),
            )


if __name__ == "__main__":
    unittest.main()
