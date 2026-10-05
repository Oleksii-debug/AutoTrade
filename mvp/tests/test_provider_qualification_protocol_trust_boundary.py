from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.provider_domain import ProviderFinancialScope
from mvp.autotrade_mvp.provider_qualification_authority import (
    ProviderQualificationError,
    ProviderQualificationScope,
    source_provider_qualification_protocol,
    verify_provider_qualification_campaign,
)
from mvp.autotrade_mvp.qualification_attestation import (
    EvidenceArtifactRef,
    QualificationAttestation,
    QualificationTrustUnavailable,
    SignedQualificationAttestation,
    canonical_packaged_qualification_trust_policy_digest,
)


SOURCE_SHA = "a" * 40
PACKAGE_DIGEST = "sha256:" + "1" * 64
CAMPAIGN_DIGEST = "sha256:" + "2" * 64
ROOT_ID = "sha256:" + "3" * 64


def _scope() -> ProviderQualificationScope:
    return ProviderQualificationScope(
        provider_scope=ProviderFinancialScope(
            provider_id="BYBIT",
            runtime_environment="PAPER",
            provider_environment="TESTNET",
            entity_policy_id="LINEAR_ORDER_V1",
        ),
        product_family="LINEAR_PERPETUAL",
        adapter_source_git_sha=SOURCE_SHA,
        packaged_artifact_digest=PACKAGE_DIGEST,
        campaign_id="bybit-linear-order",
        campaign_version=1,
        protocol_id="provider-route-v1",
        protocol_version="1.0.0",
    )


def _receipt() -> SignedQualificationAttestation:
    campaign_ref = EvidenceArtifactRef(
        artifact_id="00000000-0000-4000-8000-000000000001",
        sha256=CAMPAIGN_DIGEST,
        media_type="application/json",
        evidence_kind="PROVIDER_QUALIFICATION_CAMPAIGN",
        source_sha=SOURCE_SHA,
    )
    return SignedQualificationAttestation(
        attestation=QualificationAttestation(
            attestation_id="00000000-0000-4000-8000-000000000002",
            source_sha=SOURCE_SHA,
            domain="PROVIDER",
            gate="ROUTE_QUALIFICATION",
            package_id="AUTOTRADE",
            protocol_id="provider-route-v1",
            protocol_version="1.0.0",
            requirement_ids=("provider-route-required",),
            evidence_refs=(campaign_ref,),
            producer_id="qualification-producer",
            verifier_id="qualification-verifier",
            trust_root_id=ROOT_ID,
            runner_id="runner-1",
            harness_version="1.0.0",
            started_at="2026-10-04T04:59:00Z",
            completed_at="2026-10-04T05:00:00Z",
            signed_at="2026-10-04T05:01:00Z",
            result="PASS",
        ),
        signature_b64="eA==",
    )


class ProviderQualificationProtocolTrustBoundaryTests(unittest.TestCase):
    def test_known_provider_route_still_refuses_when_canonical_trust_is_unavailable(self):
        protocol = source_provider_qualification_protocol("PROVIDER_ROUTE_V1")
        self.assertEqual(protocol.protocol_id, "provider-route-v1")

        with TemporaryDirectory() as directory:
            evidence_root = Path(directory) / "evidence"
            evidence_store = ArtifactStore(evidence_root)
            with patch(
                "mvp.autotrade_mvp.provider_qualification_authority."
                "verify_canonical_qualification_attestation",
                side_effect=QualificationTrustUnavailable(
                    "canonical qualification trust authority unavailable"
                ),
            ) as verifier:
                with self.assertRaisesRegex(
                    ProviderQualificationError,
                    "canonical trust boundary",
                ):
                    verify_provider_qualification_campaign(
                        protocol_key="PROVIDER_ROUTE_V1",
                        receipt=_receipt(),
                        evidence_store=evidence_store,
                        evidence_root=evidence_root,
                        expected_scope=_scope(),
                    )

        verifier.assert_called_once()
        kwargs = verifier.call_args.kwargs
        self.assertEqual(kwargs["expected_domain"], "PROVIDER")
        self.assertEqual(kwargs["expected_gate"], "ROUTE_QUALIFICATION")
        self.assertEqual(kwargs["expected_package_id"], "AUTOTRADE")
        self.assertEqual(kwargs["expected_protocol_id"], "provider-route-v1")
        self.assertEqual(kwargs["expected_protocol_version"], "1.0.0")
        self.assertEqual(kwargs["expected_requirement_id"], "provider-route-required")

    def test_current_release_source_has_no_packaged_qualification_trust_pin(self):
        self.assertIsNone(canonical_packaged_qualification_trust_policy_digest())


if __name__ == "__main__":
    unittest.main()
