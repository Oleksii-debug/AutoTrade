from __future__ import annotations

import unittest
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp.qualification_attestation import (
    EvidenceArtifactRef,
    QualificationAttestation,
    SignedQualificationAttestation,
)
from mvp.autotrade_mvp.runtime_target_host_chronology_bound_qualification import (
    RuntimeTargetHostChronologyBindingError,
    verify_chronology_bound_runtime_target_host_qualification,
)


SOURCE_SHA = "a" * 40
RELEASE_ID = str(uuid5(NAMESPACE_URL, "wp65-receipt-snapshot-release"))
RELEASE_SHA = "sha256:" + "b" * 64
ROOT_ID = "sha256:" + "c" * 64


class _HostileSchemaVersion:
    def __init__(self) -> None:
        self.compared = False

    def __ne__(self, other: object) -> bool:
        self.compared = True
        raise AssertionError("hostile schema comparison executed")


class RuntimeTargetHostChronologyReceiptSnapshotTests(unittest.TestCase):
    @staticmethod
    def _receipt() -> SignedQualificationAttestation:
        evidence = EvidenceArtifactRef(
            artifact_id=str(uuid5(NAMESPACE_URL, "wp65-receipt-snapshot-evidence")),
            sha256="sha256:" + "d" * 64,
            media_type="application/json",
            evidence_kind="RUNTIME_TARGET_HOST_BINDING",
            source_sha=SOURCE_SHA,
        )
        attestation = QualificationAttestation(
            attestation_id=str(uuid5(NAMESPACE_URL, "wp65-receipt-snapshot-attestation")),
            source_sha=SOURCE_SHA,
            domain="PERFORMANCE",
            gate="RUNTIME_TARGET_HOST",
            package_id="WP-65",
            protocol_id="runtime-target-host-v1",
            protocol_version="1.0.0",
            requirement_ids=("target-host-pressure-budget",),
            evidence_refs=(evidence,),
            producer_id="independent.qualifier",
            verifier_id="autotrade.qualifier",
            trust_root_id=ROOT_ID,
            runner_id="runner-1",
            harness_version="1.0.0",
            started_at="2026-10-03T13:59:00Z",
            completed_at="2026-10-03T14:00:01Z",
            signed_at="2026-10-03T14:00:02Z",
            result="PASS",
            release_artifact_id=RELEASE_ID,
            release_artifact_sha256=RELEASE_SHA,
        )
        return SignedQualificationAttestation(attestation=attestation, signature_b64="AA==")

    def test_mutated_schema_version_is_rejected_before_executable_comparison(self):
        receipt = self._receipt()
        hostile = _HostileSchemaVersion()
        object.__setattr__(receipt.attestation, "schema_version", hostile)

        with self.assertRaisesRegex(
            RuntimeTargetHostChronologyBindingError,
            "schema_version must remain exact inert text",
        ):
            verify_chronology_bound_runtime_target_host_qualification(
                receipt,
                evidence_store=object(),
                evidence_root="unused",
                journal_store=object(),
                recovery=object(),
                runtime=object(),
                chronology_cut=object(),
                plan_id="unused",
                spec=object(),
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
                campaign_plan=object(),
                campaign_cut=object(),
                measurement=object(),
            )

        self.assertFalse(hostile.compared)


if __name__ == "__main__":
    unittest.main()
