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
    RuntimeTargetHostProvenance,
    RuntimeTargetHostQualificationError,
    verify_runtime_target_host_qualification,
)


SOURCE = "a" * 40
SPEC = "sha256:" + "b" * 64
CONFIG = "sha256:" + "c" * 64
HOST = "sha256:" + "d" * 64
WORKLOAD = "sha256:" + "e" * 64
JOURNAL = "sha256:" + "f" * 64
RELEASE_ID = "20000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + "5" * 64
ALT_RELEASE_ID = "20000000-0000-4000-8000-000000000002"
ALT_RELEASE_SHA = "sha256:" + "4" * 64
ROOT = "sha256:" + "6" * 64
ATTESTATION_DIGEST = "sha256:" + "7" * 64
POLICY = "sha256:" + "8" * 64

KINDS = (
    CAMPAIGN_EVIDENCE_KIND,
    STALENESS_EVIDENCE_KIND,
    INTERFERENCE_EVIDENCE_KIND,
    RESOURCE_EVIDENCE_KIND,
    HOST_INVENTORY_EVIDENCE_KIND,
)
_KIND_IDS = {
    BINDING_EVIDENCE_KIND: "00000000-0000-4000-8000-000000000001",
    CAMPAIGN_EVIDENCE_KIND: "00000000-0000-4000-8000-000000000002",
    STALENESS_EVIDENCE_KIND: "00000000-0000-4000-8000-000000000003",
    INTERFERENCE_EVIDENCE_KIND: "00000000-0000-4000-8000-000000000004",
    RESOURCE_EVIDENCE_KIND: "00000000-0000-4000-8000-000000000005",
    HOST_INVENTORY_EVIDENCE_KIND: "00000000-0000-4000-8000-000000000006",
}
_PAYLOAD_DIGEST = {
    kind: "sha256:" + hex(index + 1)[2:] * 64
    for index, kind in enumerate(KINDS)
}


def _sha(raw: bytes) -> str:
    return "sha256:" + sha256(raw).hexdigest()


def provenance(kind: str, **overrides) -> RuntimeTargetHostProvenance:
    values = {
        "evidence_kind": kind,
        "source_sha": SOURCE,
        "scenario_id": "target-host-primary",
        "spec_digest": SPEC,
        "configuration_hash": CONFIG,
        "host_fingerprint": HOST,
        "workload_profile_hash": WORKLOAD,
        "journal_store_identity_digest": JOURNAL,
        "release_artifact_id": RELEASE_ID,
        "release_artifact_sha256": RELEASE_SHA,
        "collector_id": f"collector-{kind.lower()}",
        "collector_version": "1.0.0",
        "payload_sha256": _PAYLOAD_DIGEST[kind],
    }
    values.update(overrides)
    return RuntimeTargetHostProvenance(**values)


def ref(kind: str, digest: str) -> EvidenceArtifactRef:
    return EvidenceArtifactRef(
        artifact_id=_KIND_IDS[kind],
        sha256=digest,
        media_type=JSON_MEDIA_TYPE,
        evidence_kind=kind,
        source_sha=SOURCE,
    )


def material(*, provenance_overrides=None, binding_overrides=None):
    provenance_overrides = provenance_overrides or {}
    raw_by_id = {}
    evidence_refs = []
    digest_by_kind = {}
    for kind in KINDS:
        raw = provenance(kind, **provenance_overrides.get(kind, {})).canonical_bytes()
        digest = _sha(raw)
        digest_by_kind[kind] = digest
        raw_by_id[_KIND_IDS[kind]] = raw
        evidence_refs.append(ref(kind, digest))
    binding_values = {
        "source_sha": SOURCE,
        "scenario_id": "target-host-primary",
        "spec_digest": SPEC,
        "configuration_hash": CONFIG,
        "host_fingerprint": HOST,
        "workload_profile_hash": WORKLOAD,
        "journal_store_identity_digest": JOURNAL,
        "release_artifact_id": RELEASE_ID,
        "release_artifact_sha256": RELEASE_SHA,
        "campaign_evidence_sha256": digest_by_kind[CAMPAIGN_EVIDENCE_KIND],
        "staleness_evidence_sha256": digest_by_kind[STALENESS_EVIDENCE_KIND],
        "interference_evidence_sha256": digest_by_kind[INTERFERENCE_EVIDENCE_KIND],
        "resource_evidence_sha256": digest_by_kind[RESOURCE_EVIDENCE_KIND],
        "host_inventory_evidence_sha256": digest_by_kind[HOST_INVENTORY_EVIDENCE_KIND],
    }
    binding_values.update(binding_overrides or {})
    binding_value = RuntimeTargetHostBinding(**binding_values)
    raw_binding = binding_value.canonical_bytes()
    raw_by_id[_KIND_IDS[BINDING_EVIDENCE_KIND]] = raw_binding
    evidence_refs.append(ref(BINDING_EVIDENCE_KIND, _sha(raw_binding)))
    return raw_by_id, tuple(evidence_refs), binding_value


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
        release_artifact_id=RELEASE_ID,
        release_artifact_sha256=RELEASE_SHA,
    )
    return SignedQualificationAttestation(
        attestation=attestation,
        signature_b64=base64.b64encode(b"profile-unit-test-signature").decode("ascii"),
    )


def accepted(
    evidence_refs,
    *,
    result="PASS",
    unresolved_limits=(),
    release_artifact_id=RELEASE_ID,
    release_artifact_sha256=RELEASE_SHA,
):
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
        release_artifact_id=release_artifact_id,
        release_artifact_sha256=release_artifact_sha256,
        attestation_json="{}",
        signature_b64="AA==",
    )


class RuntimeTargetHostQualificationTests(unittest.TestCase):
    def _verify(
        self,
        raw_by_id,
        evidence_refs,
        *,
        accepted_value=None,
        expected_release_artifact_id=RELEASE_ID,
        expected_release_artifact_sha256=RELEASE_SHA,
    ):
        signed = receipt(evidence_refs)
        accepted_value = accepted_value or accepted(evidence_refs)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(f"{directory}/store")

            def reader(artifact_id):
                return {}, raw_by_id[artifact_id]

            with patch(
                "mvp.autotrade_mvp.runtime_target_host_qualification."
                "verify_canonical_qualification_attestation",
                return_value=accepted_value,
            ) as canonical, patch(
                "mvp.autotrade_mvp.runtime_target_host_qualification."
                "trusted_authenticated_reader",
                return_value=reader,
            ):
                result = verify_runtime_target_host_qualification(
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
                    expected_release_artifact_id=expected_release_artifact_id,
                    expected_release_artifact_sha256=expected_release_artifact_sha256,
                )
            kwargs = canonical.call_args.kwargs
            self.assertEqual(kwargs["expected_domain"], DOMAIN)
            self.assertEqual(kwargs["expected_gate"], GATE)
            self.assertEqual(kwargs["expected_package_id"], PACKAGE_ID)
            self.assertEqual(kwargs["expected_protocol_id"], PROTOCOL_ID)
            self.assertEqual(kwargs["expected_requirement_id"], REQUIREMENT_ID)
            self.assertEqual(kwargs["expected_release_artifact_id"], RELEASE_ID)
            self.assertEqual(kwargs["expected_release_artifact_sha256"], RELEASE_SHA)
            return result

    def test_binding_and_provenance_round_trip_are_canonical(self):
        raw_by_id, _refs, binding_value = material()
        self.assertEqual(
            RuntimeTargetHostBinding.parse(binding_value.canonical_bytes()),
            binding_value,
        )
        raw = raw_by_id[_KIND_IDS[RESOURCE_EVIDENCE_KIND]]
        self.assertEqual(
            RuntimeTargetHostProvenance.parse(raw).evidence_kind,
            RESOURCE_EVIDENCE_KIND,
        )

    def test_duplicate_or_noncanonical_json_is_rejected(self):
        raw = (
            b'{"source_sha":"' + SOURCE.encode() +
            b'","source_sha":"' + SOURCE.encode() + b'"}'
        )
        with self.assertRaises(RuntimeTargetHostQualificationError):
            RuntimeTargetHostBinding.parse(raw)
        raw_by_id, _refs, binding_value = material()
        with self.assertRaises(RuntimeTargetHostQualificationError):
            RuntimeTargetHostBinding.parse(binding_value.canonical_bytes() + b"\n")
        with self.assertRaises(RuntimeTargetHostQualificationError):
            RuntimeTargetHostProvenance.parse(
                raw_by_id[_KIND_IDS[CAMPAIGN_EVIDENCE_KIND]] + b"\n"
            )

    def test_terminal_profile_cross_binds_all_authenticated_artifacts_and_release(self):
        raw_by_id, evidence_refs, _binding = material()
        result = self._verify(raw_by_id, evidence_refs)
        self.assertEqual(result.source_sha, SOURCE)
        self.assertEqual(result.host_fingerprint, HOST)
        self.assertEqual(result.spec_digest, SPEC)
        self.assertEqual(result.release_artifact_id, RELEASE_ID)
        self.assertEqual(result.release_artifact_sha256, RELEASE_SHA)
        self.assertIn(RESOURCE_EVIDENCE_KIND, result.collector_by_kind)

    def test_signed_binding_cannot_hide_resource_artifact_from_another_host(self):
        raw_by_id, evidence_refs, _binding = material(
            provenance_overrides={
                RESOURCE_EVIDENCE_KIND: {
                    "host_fingerprint": "sha256:" + "9" * 64,
                }
            }
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "provenance identity conflicts.*RESOURCES",
        ):
            self._verify(raw_by_id, evidence_refs)

    def test_binding_digest_substitution_is_rejected(self):
        raw_by_id, evidence_refs, _binding = material(
            binding_overrides={"resource_evidence_sha256": "sha256:" + "9" * 64}
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "does not match.*RESOURCES",
        ):
            self._verify(raw_by_id, evidence_refs)

    def test_missing_staleness_family_is_rejected(self):
        raw_by_id, evidence_refs, _binding = material()
        evidence_refs = tuple(
            ref_value for ref_value in evidence_refs
            if ref_value.evidence_kind != STALENESS_EVIDENCE_KIND
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "evidence set mismatch",
        ):
            self._verify(raw_by_id, evidence_refs)

    def test_release_identity_is_required_before_canonical_trust_dispatch(self):
        raw_by_id, evidence_refs, _binding = material()
        signed = receipt(evidence_refs)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(f"{directory}/store")
            with patch(
                "mvp.autotrade_mvp.runtime_target_host_qualification."
                "verify_canonical_qualification_attestation",
            ) as canonical:
                with self.assertRaises(RuntimeTargetHostQualificationError):
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
                        expected_release_artifact_id=None,
                        expected_release_artifact_sha256=RELEASE_SHA,
                    )
            canonical.assert_not_called()

    def test_canonical_attestation_release_mismatch_is_rejected(self):
        raw_by_id, evidence_refs, _binding = material()
        mismatched = accepted(
            evidence_refs,
            release_artifact_id=ALT_RELEASE_ID,
            release_artifact_sha256=ALT_RELEASE_SHA,
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "different release artifact",
        ):
            self._verify(raw_by_id, evidence_refs, accepted_value=mismatched)

    def test_binding_release_identity_mismatch_is_rejected(self):
        raw_by_id, evidence_refs, _binding = material(
            binding_overrides={"release_artifact_id": ALT_RELEASE_ID}
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "binding identity does not match",
        ):
            self._verify(raw_by_id, evidence_refs)

    def test_provenance_release_identity_mismatch_is_rejected(self):
        raw_by_id, evidence_refs, _binding = material(
            provenance_overrides={
                RESOURCE_EVIDENCE_KIND: {
                    "release_artifact_sha256": ALT_RELEASE_SHA,
                }
            }
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "provenance identity conflicts.*RESOURCES",
        ):
            self._verify(raw_by_id, evidence_refs)

    def test_canonical_trust_failure_happens_before_any_profile_read(self):
        raw_by_id, evidence_refs, _binding = material()
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
                        expected_release_artifact_id=RELEASE_ID,
                        expected_release_artifact_sha256=RELEASE_SHA,
                    )
            reader.assert_not_called()

    def test_nonpass_cannot_be_promoted_to_terminal_target_host_qualification(self):
        raw_by_id, evidence_refs, _binding = material()
        inconclusive = accepted(
            evidence_refs,
            result="INCONCLUSIVE",
            unresolved_limits=("pressure-run-not-complete",),
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "requires signed PASS",
        ):
            self._verify(raw_by_id, evidence_refs, accepted_value=inconclusive)


if __name__ == "__main__":
    unittest.main()
