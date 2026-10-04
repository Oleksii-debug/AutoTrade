from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from research.autotrade_research.artifacts import ArtifactStore

from mvp.autotrade_mvp.persistence import JournalStore, canonical_json, payload_digest
from mvp.autotrade_mvp.provider_domain import ProviderFinancialScope
from mvp.autotrade_mvp.provider_qualification_identity import ProviderQualificationIdentity
import mvp.autotrade_mvp.provider_qualification_authority as authority
from mvp.autotrade_mvp.provider_qualification_authority import (
    ProviderQualificationAuthorityError,
    accept_provider_qualification,
    require_current_provider_qualification,
    revoke_provider_qualification,
)
from mvp.autotrade_mvp.qualification_attestation import (
    AcceptedQualificationAttestation,
    EvidenceArtifactRef,
    QualificationAttestation,
    SignedQualificationAttestation,
)


SOURCE = "a" * 40
PACKAGED_ID = str(uuid5(NAMESPACE_URL, "wp08-packaged-provider-adapter"))
PACKAGED_DIGEST = "sha256:" + "b" * 64
POLICY_ID = "sha256:" + "c" * 64
ROOT_ID = "sha256:" + "d" * 64


def _scope(provider_environment: str = "TESTNET") -> ProviderFinancialScope:
    return ProviderFinancialScope(
        provider_id="BYBIT",
        runtime_environment="PAPER",
        provider_environment=provider_environment,
        entity_policy_id="UNIFIED_ACCOUNT",
    )


def _publish(
    store: ArtifactStore,
    *,
    name: str,
    kind: str,
    media_type: str,
    data: bytes,
) -> EvidenceArtifactRef:
    artifact_id = str(uuid5(NAMESPACE_URL, "wp08-authority-" + name))
    manifest = store.publish_bytes(
        artifact_id=artifact_id,
        data=data,
        media_type=media_type,
        rights={"storage": True, "export": False},
        source_refs=[f"git:{SOURCE}"],
        metadata={"evidence_kind": kind},
    )
    return EvidenceArtifactRef(
        artifact_id=artifact_id,
        sha256=manifest["sha256"],
        media_type=media_type,
        evidence_kind=kind,
        source_sha=SOURCE,
    )


def _accepted_for(receipt: SignedQualificationAttestation) -> AcceptedQualificationAttestation:
    value = receipt.attestation
    return AcceptedQualificationAttestation(
        attestation_id=value.attestation_id,
        attestation_digest=value.content_digest,
        policy_id=POLICY_ID,
        policy_version="2026.10",
        trust_root_id=ROOT_ID,
        result=value.result,
        source_sha=value.source_sha,
        domain=value.domain,
        gate=value.gate,
        package_id=value.package_id,
        protocol_id=value.protocol_id,
        protocol_version=value.protocol_version,
        requirement_id="provider-route-qualification",
        release_artifact_id=value.release_artifact_id,
        release_artifact_sha256=value.release_artifact_sha256,
    )


def _fake_canonical_verifier(receipt, **kwargs):
    value = receipt.attestation
    assert kwargs["expected_source_sha"] == SOURCE
    assert kwargs["expected_domain"] == "PROVIDER"
    assert kwargs["expected_gate"] == "ROUTE_QUALIFICATION"
    assert kwargs["expected_package_id"] == "WP-08"
    assert kwargs["expected_protocol_id"] == "provider-route-qualification-v1"
    assert kwargs["expected_protocol_version"] == "1.0.0"
    assert kwargs["expected_requirement_id"] == "provider-route-qualification"
    assert kwargs["expected_release_artifact_id"] == PACKAGED_ID
    assert kwargs["expected_release_artifact_sha256"] == PACKAGED_DIGEST
    return _accepted_for(receipt)


def _receipt(
    store: ArtifactStore,
    *,
    campaign_name: str,
    result_bytes: bytes = b'{"status":"PASS"}',
    scope: ProviderFinancialScope | None = None,
) -> tuple[SignedQualificationAttestation, dict[str, EvidenceArtifactRef]]:
    if scope is None:
        scope = _scope()
    case_policy = _publish(
        store,
        name=campaign_name + "-policy",
        kind="PROVIDER_QUALIFICATION_CASE_POLICY",
        media_type="application/json",
        data=b'{"required":["auth-read","order-prepare"]}',
    )
    results = _publish(
        store,
        name=campaign_name + "-results",
        kind="PROVIDER_QUALIFICATION_RESULTS",
        media_type="application/json",
        data=result_bytes,
    )
    routes = _publish(
        store,
        name=campaign_name + "-routes",
        kind="PROVIDER_QUALIFICATION_ROUTE_SEMANTICS",
        media_type="application/json",
        data=b'{"private_read":"wallet-balance","write":"order-create"}',
    )
    docs = _publish(
        store,
        name=campaign_name + "-docs",
        kind="PROVIDER_QUALIFICATION_DOCUMENTATION",
        media_type="application/json",
        data=b'{"api":"v5","revision":"2026-10"}',
    )
    campaign_payload = {
        "schema_version": "1.0.0",
        "provider_scope": scope.payload(),
        "product_family": "UNIFIED_TRADING",
        "adapter_source_git_sha": SOURCE,
        "packaged_artifact_id": PACKAGED_ID,
        "packaged_artifact_digest": PACKAGED_DIGEST,
        "campaign_id": campaign_name,
        "campaign_version": 1,
        "required_case_policy_artifact_id": case_policy.artifact_id,
        "result_set_artifact_id": results.artifact_id,
        "route_semantics_artifact_id": routes.artifact_id,
        "documentation_revision_artifact_id": docs.artifact_id,
    }
    campaign = _publish(
        store,
        name=campaign_name + "-campaign",
        kind="PROVIDER_QUALIFICATION_CAMPAIGN",
        media_type="application/vnd.autotrade.provider-qualification+json",
        data=canonical_json(campaign_payload).encode("utf-8"),
    )
    refs = (campaign, case_policy, results, routes, docs)
    attestation = QualificationAttestation(
        attestation_id=str(uuid5(NAMESPACE_URL, "wp08-attestation-" + campaign_name)),
        source_sha=SOURCE,
        domain="PROVIDER",
        gate="ROUTE_QUALIFICATION",
        package_id="WP-08",
        protocol_id="provider-route-qualification-v1",
        protocol_version="1.0.0",
        requirement_ids=("provider-route-qualification",),
        evidence_refs=refs,
        producer_id="provider.qualifier",
        verifier_id="autotrade.provider.verifier",
        trust_root_id=ROOT_ID,
        runner_id="provider-campaign-runner",
        harness_version="1.0.0",
        started_at="2026-10-04T00:00:00Z",
        completed_at="2026-10-04T00:10:00Z",
        signed_at="2026-10-04T00:11:00Z",
        result="PASS",
        release_artifact_id=PACKAGED_ID,
        release_artifact_sha256=PACKAGED_DIGEST,
    )
    receipt = SignedQualificationAttestation(
        attestation=attestation,
        signature_b64="AA==",
    )
    return receipt, {ref.evidence_kind: ref for ref in refs}


class ProviderQualificationAuthorityCurrentTests(unittest.TestCase):
    def _accept(self, store, artifact_store, artifact_root, receipt, *, scope=None):
        if scope is None:
            scope = _scope()
        return accept_provider_qualification(
            store,
            receipt=receipt,
            evidence_store=artifact_store,
            evidence_root=artifact_root,
            expected_provider_scope=scope,
            expected_product_family="UNIFIED_TRADING",
            expected_adapter_source_git_sha=SOURCE,
            expected_packaged_artifact_digest=PACKAGED_DIGEST,
        )

    def _require(self, store, artifact_store, artifact_root, qid, *, scope=None):
        if scope is None:
            scope = _scope()
        return require_current_provider_qualification(
            store,
            expected_provider_scope=scope,
            expected_product_family="UNIFIED_TRADING",
            expected_adapter_source_git_sha=SOURCE,
            expected_packaged_artifact_digest=PACKAGED_DIGEST,
            expected_qualification_id=qid,
            evidence_store=artifact_store,
            evidence_root=artifact_root,
        )

    def test_acceptance_derives_identity_from_authenticated_signed_graph(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_store = ArtifactStore(artifact_root)
            receipt, refs = _receipt(artifact_store, campaign_name="campaign-a")
            store = JournalStore(root / "journal.sqlite")

            with patch.object(
                authority,
                "verify_canonical_qualification_attestation",
                side_effect=_fake_canonical_verifier,
            ):
                accepted = self._accept(
                    store, artifact_store, artifact_root, receipt
                )
                replayed = self._require(
                    store,
                    artifact_store,
                    artifact_root,
                    accepted.qualification_id,
                )

            self.assertEqual(replayed.qualification_id, accepted.qualification_id)
            self.assertEqual(accepted.identity.provider_scope, _scope())
            self.assertEqual(
                accepted.identity.required_case_policy_digest,
                refs["PROVIDER_QUALIFICATION_CASE_POLICY"].sha256,
            )
            self.assertEqual(
                accepted.identity.result_set_digest,
                refs["PROVIDER_QUALIFICATION_RESULTS"].sha256,
            )
            self.assertEqual(
                accepted.identity.route_semantics_digest,
                refs["PROVIDER_QUALIFICATION_ROUTE_SEMANTICS"].sha256,
            )
            self.assertEqual(
                accepted.identity.documentation_revision_digest,
                refs["PROVIDER_QUALIFICATION_DOCUMENTATION"].sha256,
            )
            self.assertEqual(accepted.identity.attestation_digest, receipt.attestation.content_digest)
            self.assertEqual(accepted.identity.trust_policy_digest, POLICY_ID)

    def test_q1_supersession_never_silently_substitutes_q2(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_store = ArtifactStore(artifact_root)
            receipt_a, _ = _receipt(artifact_store, campaign_name="campaign-a")
            receipt_b, _ = _receipt(
                artifact_store,
                campaign_name="campaign-b",
                result_bytes=b'{"status":"PASS","cases":2}',
            )
            store = JournalStore(root / "journal.sqlite")

            with patch.object(
                authority,
                "verify_canonical_qualification_attestation",
                side_effect=_fake_canonical_verifier,
            ):
                q1 = self._accept(store, artifact_store, artifact_root, receipt_a)
                q2 = self._accept(store, artifact_store, artifact_root, receipt_b)
                self.assertNotEqual(q1.qualification_id, q2.qualification_id)
                with self.assertRaisesRegex(
                    ProviderQualificationAuthorityError,
                    "superseded or mismatched",
                ):
                    self._require(
                        store,
                        artifact_store,
                        artifact_root,
                        q1.qualification_id,
                    )
                current = self._require(
                    store,
                    artifact_store,
                    artifact_root,
                    q2.qualification_id,
                )

            self.assertEqual(current.qualification_id, q2.qualification_id)

    def test_revoked_q_cannot_be_required_after_restart_style_replay(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_store = ArtifactStore(artifact_root)
            receipt, _ = _receipt(artifact_store, campaign_name="campaign-a")
            journal_path = root / "journal.sqlite"
            store = JournalStore(journal_path)

            with patch.object(
                authority,
                "verify_canonical_qualification_attestation",
                side_effect=_fake_canonical_verifier,
            ):
                accepted = self._accept(store, artifact_store, artifact_root, receipt)
                revoke_provider_qualification(
                    store,
                    expected_provider_scope=_scope(),
                    expected_product_family="UNIFIED_TRADING",
                    expected_adapter_source_git_sha=SOURCE,
                    expected_packaged_artifact_digest=PACKAGED_DIGEST,
                    expected_qualification_id=accepted.qualification_id,
                    reason="campaign superseded by operator policy",
                    evidence_store=artifact_store,
                    evidence_root=artifact_root,
                )
                restarted = JournalStore(journal_path)
                with self.assertRaisesRegex(
                    ProviderQualificationAuthorityError,
                    "revoked",
                ):
                    self._require(
                        restarted,
                        artifact_store,
                        artifact_root,
                        accepted.qualification_id,
                    )

    def test_caller_constructed_q_and_direct_journal_event_cannot_mint_current_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_store = ArtifactStore(artifact_root)
            store = JournalStore(root / "journal.sqlite")
            fake = ProviderQualificationIdentity(
                provider_scope=_scope(),
                product_family="UNIFIED_TRADING",
                adapter_source_git_sha=SOURCE,
                packaged_artifact_digest=PACKAGED_DIGEST,
                campaign_id="caller",
                campaign_version=1,
                required_case_policy_digest="sha256:" + "1" * 64,
                result_set_digest="sha256:" + "2" * 64,
                route_semantics_digest="sha256:" + "3" * 64,
                documentation_revision_digest="sha256:" + "4" * 64,
                evidence_set_digest="sha256:" + "5" * 64,
                attestation_digest="sha256:" + "6" * 64,
                trust_policy_digest="sha256:" + "7" * 64,
                issuer_identity_digest="sha256:" + "8" * 64,
                verifier_identity_digest="sha256:" + "9" * 64,
            )
            aggregate_id = authority._aggregate_id_for_identity(fake)
            store_digest = authority._store_identity_digest(store)
            fake_payload = {
                "schema_version": "1.0.0",
                "store_identity_digest": store_digest,
                "qualification_id": fake.content_digest,
                "identity_payload": fake.payload(),
                "receipt": {"attestation": {}, "signature_b64": "AA=="},
                "campaign_artifact_id": str(uuid5(NAMESPACE_URL, "forged-campaign")),
                "supersedes_qualification_id": None,
            }
            store.append_event(
                {
                    "event_id": "forged-provider-qualification",
                    "event_type": "ProviderQualificationAccepted",
                    "aggregate_type": "provider_qualification_authority",
                    "aggregate_id": aggregate_id,
                    "aggregate_version": "1",
                    "payload": fake_payload,
                    "payload_hash": payload_digest(fake_payload),
                    "committed_at": "2026-10-04T00:00:00+00:00",
                }
            )

            with self.assertRaises(Exception):
                self._require(
                    store,
                    artifact_store,
                    artifact_root,
                    fake.content_digest,
                )

    def test_retained_evidence_loss_invalidates_previously_accepted_q(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_store = ArtifactStore(artifact_root)
            receipt, refs = _receipt(artifact_store, campaign_name="campaign-a")
            store = JournalStore(root / "journal.sqlite")

            with patch.object(
                authority,
                "verify_canonical_qualification_attestation",
                side_effect=_fake_canonical_verifier,
            ):
                accepted = self._accept(store, artifact_store, artifact_root, receipt)
                result_ref = refs["PROVIDER_QUALIFICATION_RESULTS"]
                digest = result_ref.sha256.removeprefix("sha256:")
                object_path = artifact_store.objects / digest[:2] / digest
                object_path.unlink()
                with self.assertRaises(Exception):
                    self._require(
                        store,
                        artifact_store,
                        artifact_root,
                        accepted.qualification_id,
                    )

    def test_product_selected_provider_environment_cannot_be_rebound(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_root = root / "artifacts"
            artifact_store = ArtifactStore(artifact_root)
            receipt, _ = _receipt(
                artifact_store,
                campaign_name="campaign-testnet",
                scope=_scope("TESTNET"),
            )
            store = JournalStore(root / "journal.sqlite")

            with patch.object(
                authority,
                "verify_canonical_qualification_attestation",
                side_effect=_fake_canonical_verifier,
            ):
                with self.assertRaisesRegex(
                    ProviderQualificationAuthorityError,
                    "product-selected scope",
                ):
                    self._accept(
                        store,
                        artifact_store,
                        artifact_root,
                        receipt,
                        scope=_scope("DEMO"),
                    )

            self.assertEqual(
                store.load_events_by_aggregate_type("provider_qualification_authority"),
                [],
            )


if __name__ == "__main__":
    unittest.main()
