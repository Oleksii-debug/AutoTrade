import base64
from decimal import Decimal
from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from research.autotrade_research.artifacts.store import ArtifactStore
from research.autotrade_research.forward_paper import (
    ForwardOutcome,
    ForwardPaperEvidence,
    ForwardPaperProtocol,
    OperationalObservation,
    SealedPrediction,
    forward_paper_protocol_hash,
)

from mvp.autotrade_mvp.forward_paper_qualification import (
    forward_paper_qualification_bytes,
    forward_paper_qualification_metadata,
    qualify_forward_paper,
)
from mvp.autotrade_mvp.qualification_attestation import (
    AcceptedQualificationAttestation,
    EvidenceArtifactRef,
    QualificationAttestation,
    SignedQualificationAttestation,
)


BUILD = "3cae63fac37820611cddd38128a91185cb271fff"
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
EVIDENCE_ID = str(uuid5(NAMESPACE_URL, "wp57-forward-paper-evidence"))
OTHER_EVIDENCE_ID = str(uuid5(NAMESPACE_URL, "wp57-other-evidence"))
MEDIA_TYPE = "application/vnd.autotrade.forward-paper-qualification"
EVIDENCE_KIND = "FORWARD_PAPER_CAMPAIGN"


class ForwardPaperTerminalQualificationTests(unittest.TestCase):
    def protocol(self):
        values = dict(
            campaign_id="paper-campaign-terminal-1",
            exact_build_sha=BUILD,
            registered_at="2026-09-24T19:59:00Z",
            starts_at="2026-09-24T20:00:00Z",
            ends_at="2026-09-24T21:00:00Z",
            minimum_predictions=1,
            maximum_decision_latency_ms=1000,
            required_provider_capabilities=("KRAKEN:SPOT:LIMIT",),
            required_operational_cases=("RECONNECT",),
        )
        return ForwardPaperProtocol.create(
            **values,
            protocol_hash=forward_paper_protocol_hash(**values),
        )

    def evidence(self, protocol, **overrides):
        prediction = SealedPrediction.create(
            prediction_id="pred-1",
            provider_capability="KRAKEN:SPOT:LIMIT",
            input_hash=HASH_A,
            proposal_hash=HASH_B,
            information_cutoff_at="2026-09-24T20:04:59Z",
            sealed_at="2026-09-24T20:05:00Z",
            decision_deadline_at="2026-09-24T20:05:02Z",
            outcome_horizon_end_at="2026-09-24T20:30:00Z",
            decision_latency_ms=400,
        )
        outcome = ForwardOutcome.create(
            prediction_id="pred-1",
            outcome_hash=HASH_C,
            outcome_available_at="2026-09-24T20:30:01Z",
            evaluated_at="2026-09-24T20:31:00Z",
        )
        observation = OperationalObservation.create(
            provider_capability="KRAKEN:SPOT:LIMIT",
            case="RECONNECT",
            observed_at="2026-09-24T20:50:00Z",
            reconciled=True,
        )
        values = dict(
            exact_build_sha=BUILD,
            protocol_hash=protocol.protocol_hash,
            observed_until="2026-09-24T21:01:00Z",
            predictions=(prediction,),
            outcomes=(outcome,),
            operational_observations=(observation,),
            costs_by_currency={"USD": Decimal("12.34")},
            costs_complete=True,
            account_reconciliation_complete=True,
        )
        values.update(overrides)
        return ForwardPaperEvidence.create(**values)

    def publish(self, store, protocol, evidence, *, artifact_id=EVIDENCE_ID):
        data = forward_paper_qualification_bytes(protocol, evidence)
        manifest = store.publish_bytes(
            artifact_id=artifact_id,
            data=data,
            media_type=MEDIA_TYPE,
            rights={"storage": True, "export": False},
            source_refs=[f"git:{BUILD}"],
            metadata=forward_paper_qualification_metadata(protocol),
        )
        self.assertEqual(
            manifest["sha256"],
            "sha256:" + sha256(data).hexdigest(),
        )
        return manifest

    def receipt(
        self,
        protocol,
        *,
        artifact_id=EVIDENCE_ID,
        artifact_sha256,
        result="PASS",
        started_at="2026-09-24T21:02:00Z",
        completed_at="2026-09-24T21:03:00Z",
        signed_at="2026-09-24T21:04:00Z",
    ):
        ref = EvidenceArtifactRef(
            artifact_id=artifact_id,
            sha256=artifact_sha256,
            media_type=MEDIA_TYPE,
            evidence_kind=EVIDENCE_KIND,
            source_sha=BUILD,
        )
        attestation = QualificationAttestation(
            attestation_id=str(uuid5(NAMESPACE_URL, "wp57-terminal-attestation")),
            source_sha=BUILD,
            domain="FORWARD_PAPER",
            gate="QUALIFICATION",
            package_id="WP-57",
            protocol_id="forward-paper-qualification-v1",
            protocol_version="1.0.0",
            requirement_ids=(
                "forward-paper-terminal-evidence",
                f"protocol/{protocol.protocol_hash}",
            ),
            evidence_refs=(ref,),
            producer_id="qualifier.release.service",
            verifier_id="autotrade.trust.verifier",
            trust_root_id="sha256:" + "f" * 64,
            runner_id="qualification-runner-1",
            harness_version="1.0.0",
            started_at=started_at,
            completed_at=completed_at,
            signed_at=signed_at,
            result=result,
        )
        receipt = SignedQualificationAttestation(
            attestation,
            base64.b64encode(b"test-signature-placeholder").decode("ascii"),
        )
        accepted = AcceptedQualificationAttestation(
            attestation_id=attestation.attestation_id,
            attestation_digest=attestation.content_digest,
            policy_id="sha256:" + "e" * 64,
            policy_version="2026.09",
            trust_root_id=attestation.trust_root_id,
            result=result,
            source_sha=BUILD,
            domain="FORWARD_PAPER",
            gate="QUALIFICATION",
            package_id="WP-57",
            protocol_id="forward-paper-qualification-v1",
            protocol_version="1.0.0",
            requirement_id="forward-paper-terminal-evidence",
            release_artifact_id=None,
            release_artifact_sha256=None,
        )
        return receipt, accepted

    def test_valid_mechanics_without_immutable_artifact_is_inconclusive(self):
        protocol = self.protocol()
        result = qualify_forward_paper(protocol, self.evidence(protocol))

        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertIn(
            "forward_paper_artifact_identity_missing",
            result.reason_codes,
        )
        self.assertIn(
            "immutable_forward_paper_evidence_unavailable",
            result.reason_codes,
        )
        self.assertFalse(result.trading_authority_granted)
        self.assertEqual(result.mechanics.economic_edge_status, "NOT_ESTABLISHED")

    def test_matching_artifact_without_independent_receipt_is_inconclusive(self):
        protocol = self.protocol()
        evidence = self.evidence(protocol)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            manifest = self.publish(store, protocol, evidence)
            result = qualify_forward_paper(
                protocol,
                evidence,
                evidence_store=store,
                evidence_artifact_id=EVIDENCE_ID,
                evidence_artifact_sha256=manifest["sha256"],
            )

        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertIn(
            "independent_forward_paper_trust_unavailable",
            result.reason_codes,
        )

    def test_store_match_uses_one_authenticated_snapshot_not_split_reads(self):
        protocol = self.protocol()
        evidence = self.evidence(protocol)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            manifest = self.publish(store, protocol, evidence)
            with (
                patch.object(
                    store,
                    "load_manifest",
                    side_effect=AssertionError("legacy split manifest read"),
                ),
                patch.object(
                    store,
                    "read_bytes",
                    side_effect=AssertionError("legacy split object read"),
                ),
                patch.object(
                    store,
                    "read_authenticated_snapshot",
                    wraps=store.read_authenticated_snapshot,
                ) as snapshot,
            ):
                result = qualify_forward_paper(
                    protocol,
                    evidence,
                    evidence_store=store,
                    evidence_artifact_id=EVIDENCE_ID,
                    evidence_artifact_sha256=manifest["sha256"],
                )

        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertNotIn(
            "immutable_forward_paper_evidence_mismatch",
            result.reason_codes,
        )
        self.assertIn(
            "independent_forward_paper_trust_unavailable",
            result.reason_codes,
        )
        snapshot.assert_called_once_with(EVIDENCE_ID)

    def test_exact_artifact_and_independent_pass_can_produce_terminal_pass(self):
        protocol = self.protocol()
        evidence = self.evidence(protocol)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            manifest = self.publish(store, protocol, evidence)
            receipt, accepted = self.receipt(
                protocol,
                artifact_sha256=manifest["sha256"],
            )
            with patch(
                "mvp.autotrade_mvp.forward_paper_qualification."
                "verify_canonical_qualification_attestation",
                return_value=accepted,
            ) as verify:
                result = qualify_forward_paper(
                    protocol,
                    evidence,
                    evidence_store=store,
                    evidence_artifact_id=EVIDENCE_ID,
                    evidence_artifact_sha256=manifest["sha256"],
                    qualification_receipt=receipt,
                )

        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            result.qualification_attestation_id,
            accepted.attestation_id,
        )
        self.assertFalse(result.trading_authority_granted)
        verify.assert_called_once()
        call = verify.call_args
        self.assertEqual(call.kwargs["expected_source_sha"], BUILD)
        self.assertEqual(call.kwargs["expected_domain"], "FORWARD_PAPER")
        self.assertEqual(call.kwargs["expected_gate"], "QUALIFICATION")
        self.assertEqual(call.kwargs["expected_package_id"], "WP-57")
        self.assertEqual(
            call.kwargs["expected_requirement_id"],
            "forward-paper-terminal-evidence",
        )

    def test_tampered_artifact_identity_is_a_hard_failure(self):
        protocol = self.protocol()
        evidence = self.evidence(protocol)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            manifest = self.publish(store, protocol, evidence)
            wrong_digest = "sha256:" + "0" * 64
            self.assertNotEqual(wrong_digest, manifest["sha256"])
            result = qualify_forward_paper(
                protocol,
                evidence,
                evidence_store=store,
                evidence_artifact_id=EVIDENCE_ID,
                evidence_artifact_sha256=wrong_digest,
            )

        self.assertEqual(result.status, "FAIL")
        self.assertIn(
            "immutable_forward_paper_evidence_mismatch",
            result.reason_codes,
        )

    def test_signed_evidence_set_must_match_exact_campaign_artifact(self):
        protocol = self.protocol()
        evidence = self.evidence(protocol)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            manifest = self.publish(store, protocol, evidence)
            receipt, accepted = self.receipt(
                protocol,
                artifact_id=OTHER_EVIDENCE_ID,
                artifact_sha256="sha256:" + "1" * 64,
            )
            with patch(
                "mvp.autotrade_mvp.forward_paper_qualification."
                "verify_canonical_qualification_attestation",
                return_value=accepted,
            ):
                result = qualify_forward_paper(
                    protocol,
                    evidence,
                    evidence_store=store,
                    evidence_artifact_id=EVIDENCE_ID,
                    evidence_artifact_sha256=manifest["sha256"],
                    qualification_receipt=receipt,
                )

        self.assertEqual(result.status, "FAIL")
        self.assertIn(
            "independent_forward_paper_evidence_set_mismatch",
            result.reason_codes,
        )

    def test_terminal_receipt_cannot_complete_before_observed_evidence(self):
        protocol = self.protocol()
        evidence = self.evidence(protocol)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            manifest = self.publish(store, protocol, evidence)
            receipt, accepted = self.receipt(
                protocol,
                artifact_sha256=manifest["sha256"],
                started_at="2026-09-24T20:58:00Z",
                completed_at="2026-09-24T21:00:30Z",
                signed_at="2026-09-24T21:00:45Z",
            )
            with patch(
                "mvp.autotrade_mvp.forward_paper_qualification."
                "verify_canonical_qualification_attestation",
                return_value=accepted,
            ) as verify:
                result = qualify_forward_paper(
                    protocol,
                    evidence,
                    evidence_store=store,
                    evidence_artifact_id=EVIDENCE_ID,
                    evidence_artifact_sha256=manifest["sha256"],
                    qualification_receipt=receipt,
                )

        self.assertEqual(result.status, "FAIL")
        self.assertIn(
            "independent_forward_paper_qualification_predates_evidence",
            result.reason_codes,
        )
        self.assertIsNone(result.qualification_attestation_id)
        verify.assert_not_called()

    def test_independent_pass_cannot_upgrade_incomplete_campaign_mechanics(self):
        protocol = self.protocol()
        evidence = self.evidence(protocol, costs_complete=False)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            manifest = self.publish(store, protocol, evidence)
            receipt, accepted = self.receipt(
                protocol,
                artifact_sha256=manifest["sha256"],
            )
            with patch(
                "mvp.autotrade_mvp.forward_paper_qualification."
                "verify_canonical_qualification_attestation",
                return_value=accepted,
            ):
                result = qualify_forward_paper(
                    protocol,
                    evidence,
                    evidence_store=store,
                    evidence_artifact_id=EVIDENCE_ID,
                    evidence_artifact_sha256=manifest["sha256"],
                    qualification_receipt=receipt,
                )

        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertIn("actual_costs_incomplete", result.reason_codes)
        self.assertFalse(result.trading_authority_granted)


if __name__ == "__main__":
    unittest.main()
