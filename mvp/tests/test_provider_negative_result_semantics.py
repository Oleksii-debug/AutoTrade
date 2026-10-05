from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.autotrade_mvp.provider_core import Surface
from mvp.autotrade_mvp.provider_negative_result_semantics import (
    ProviderNegativeResultSemanticsError,
    QualifiedNegativeResultSemantics,
    qualified_negative_result_route_semantic_claim,
    require_qualified_negative_result_semantics,
)
from mvp.autotrade_mvp.provider_qualification_authority import (
    _derive_accepted_provider_qualification,
    parse_provider_qualification_campaign,
)
from mvp.autotrade_mvp.provider_route_reads import (
    prepare_qualified_provider_read,
    qualified_read_route_semantic_claim,
)
from mvp.autotrade_mvp.provider_selection import select_provider
from mvp.autotrade_mvp.qualification_attestation import (
    AcceptedQualificationAttestation,
    EvidenceArtifactRef,
    QualificationAttestation,
    SignedQualificationAttestation,
)
from mvp.tests.provider_qualification_test_support import (
    ExactQualificationProjectionHarness,
)
from mvp.tests.test_durable_capabilities import verified
from mvp.tests.test_provider_qualification_authority import (
    CAMPAIGN_KIND,
    POLICY_ID,
    ROOT_ID,
    SOURCE_SHA,
    _artifact_id,
    _campaign_payload,
    _protocol,
    _raw_ref,
)
from mvp.tests.test_provider_route_dispatch import successor_spot_q
from mvp.tests.test_provider_selection import (
    NOW,
    accepted_spot_q,
    candidate,
    request as route_request,
)


ENDPOINT = "/v5/account/wallet-balance"
PERMISSION = "ACCOUNT.READ"
PARSER_IDENTITY = "BYBIT_ORDER_V5_JSON_V1"


def accepted_spot_q_with_negative_result_claim(
    *,
    ordinal: int = 610,
    claim_parser_identity: str = PARSER_IDENTITY,
):
    protocol = _protocol()
    raw_ref = _raw_ref(100 + ordinal)
    payload = _campaign_payload(raw_ref=raw_ref)
    payload["product_family"] = "SPOT"

    read_key, read_digest = qualified_read_route_semantic_claim(
        provider_id="BYBIT",
        endpoint=ENDPOINT,
        surface=Surface.AUTHENTICATED_READ,
        permission_scope=PERMISSION,
    )
    negative_key, negative_digest = qualified_negative_result_route_semantic_claim(
        provider_id="BYBIT",
        endpoint=ENDPOINT,
        surface=Surface.AUTHENTICATED_READ,
        permission_scope=PERMISSION,
        parser_identity=claim_parser_identity,
    )
    payload["route_semantics"][read_key] = read_digest
    payload["route_semantics"][negative_key] = negative_digest
    payload["route_semantics"] = dict(sorted(payload["route_semantics"].items()))

    campaign_raw = canonical_json(payload).encode("utf-8")
    campaign_ref = EvidenceArtifactRef(
        artifact_id=_artifact_id(ordinal),
        sha256="sha256:" + sha256(campaign_raw).hexdigest(),
        media_type="application/json",
        evidence_kind=CAMPAIGN_KIND,
        source_sha=SOURCE_SHA,
    )
    campaign = parse_provider_qualification_campaign(campaign_raw)
    attestation = QualificationAttestation(
        attestation_id=_artifact_id(500 + ordinal),
        source_sha=SOURCE_SHA,
        domain=protocol.domain,
        gate=protocol.gate,
        package_id=protocol.package_id,
        protocol_id=protocol.protocol_id,
        protocol_version=protocol.protocol_version,
        requirement_ids=(protocol.requirement_id,),
        evidence_refs=(campaign_ref, raw_ref),
        producer_id="qualification-producer",
        verifier_id="qualification-verifier",
        trust_root_id=ROOT_ID,
        runner_id="runner-1",
        harness_version="1.0.0",
        started_at="2026-10-04T04:59:00Z",
        completed_at="2026-10-04T05:00:00Z",
        signed_at="2026-10-04T05:01:00Z",
        result="PASS",
        release_artifact_id=None,
        release_artifact_sha256=None,
    )
    receipt = SignedQualificationAttestation(
        attestation=attestation,
        signature_b64="eA==",
    )
    accepted = AcceptedQualificationAttestation(
        attestation_id=attestation.attestation_id,
        attestation_digest=attestation.content_digest,
        policy_id=POLICY_ID,
        policy_version="1.0.0",
        trust_root_id=ROOT_ID,
        result="PASS",
        source_sha=SOURCE_SHA,
        domain=protocol.domain,
        gate=protocol.gate,
        package_id=protocol.package_id,
        protocol_id=protocol.protocol_id,
        protocol_version=protocol.protocol_version,
        requirement_id=protocol.requirement_id,
        release_artifact_id=None,
        release_artifact_sha256=None,
    )
    record = _derive_accepted_provider_qualification(
        protocol=protocol,
        campaign=campaign,
        campaign_artifact_ref=campaign_ref,
        accepted_attestation=accepted,
        receipt=receipt,
    )
    return record, receipt, protocol


class ProviderNegativeResultSemanticsTests(unittest.TestCase):
    def setup_route(self, directory: str, *, signed_negative: bool, claim_parser=PARSER_IDENTITY):
        journal = JournalStore(Path(directory) / "journal.sqlite3")
        capabilities = DurableCapabilityRegistry(journal)
        capabilities.add(
            verified(
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                NOW - timedelta(minutes=1),
                provider_id="BYBIT",
                provider_environment="TESTNET",
            )
        )
        evidence_root = Path(directory) / "evidence"
        harness = ExactQualificationProjectionHarness().start()
        self.addCleanup(harness.stop)
        qualifications = harness.registry(
            journal,
            evidence_store=ArtifactStore(evidence_root),
            evidence_root=evidence_root,
        )
        if signed_negative:
            q1, receipt1, protocol1 = accepted_spot_q_with_negative_result_claim(
                claim_parser_identity=claim_parser,
            )
        else:
            q1, receipt1, protocol1 = accepted_spot_q(ordinal=611)
        harness.register(
            protocol_key=protocol1.key,
            record=q1,
            receipt=receipt1,
        )
        qualifications._append_accepted(
            protocol_key=protocol1.key,
            record=q1,
            receipt=receipt1,
        )
        selection = select_provider(
            route_request(),
            [candidate()],
            at=NOW,
            capability_registry=capabilities,
            qualification_registry=qualifications,
        )
        self.assertEqual(selection.status, "SELECTED_UNAMBIGUOUS")
        self.assertIsNotNone(selection.selected)
        read_binding = prepare_qualified_provider_read(
            selection.selected,
            capabilities,
            qualifications,
            surface=Surface.AUTHENTICATED_READ,
            endpoint=ENDPOINT,
            query={"accountType": "UNIFIED"},
            at=NOW,
            permission_scope=PERMISSION,
        )
        return qualifications, selection.selected, read_binding, q1, harness

    def test_claim_is_endpoint_stable_but_parser_bound(self):
        key1, digest1 = qualified_negative_result_route_semantic_claim(
            provider_id="BYBIT",
            endpoint=ENDPOINT,
            surface=Surface.AUTHENTICATED_READ,
            permission_scope=PERMISSION,
            parser_identity=PARSER_IDENTITY,
        )
        key2, digest2 = qualified_negative_result_route_semantic_claim(
            provider_id="BYBIT",
            endpoint=ENDPOINT,
            surface=Surface.AUTHENTICATED_READ,
            permission_scope=PERMISSION,
            parser_identity="BYBIT_ORDER_V5_JSON_V2",
        )
        self.assertEqual(key1, key2)
        self.assertNotEqual(digest1, digest2)
        self.assertTrue(key1.startswith("NEGATIVE_RESULT_RULE:"))
        self.assertTrue(digest1.startswith("sha256:"))

    def test_unsigned_q_cannot_authorize_negative_result_semantics(self):
        with TemporaryDirectory() as directory:
            qualifications, route, binding, _q1, _harness = self.setup_route(
                directory,
                signed_negative=False,
            )
            with self.assertRaisesRegex(
                ProviderNegativeResultSemanticsError,
                "does not authorize negative-result semantics",
            ):
                require_qualified_negative_result_semantics(
                    route=route,
                    read_binding=binding,
                    qualification_registry=qualifications,
                    at=NOW,
                )

    def test_exact_signed_q_authorizes_only_semantics_capability(self):
        with TemporaryDirectory() as directory:
            qualifications, route, binding, q1, _harness = self.setup_route(
                directory,
                signed_negative=True,
            )
            authority = require_qualified_negative_result_semantics(
                route=route,
                read_binding=binding,
                qualification_registry=qualifications,
                at=NOW,
            )
            self.assertEqual(authority.qualification_id, q1.qualification_id)
            self.assertEqual(authority.qualified_query_digest, binding.query_digest)
            self.assertEqual(authority.provider_id, "BYBIT")
            self.assertEqual(authority.account_id, "paper-account")
            self.assertEqual(authority.environment, "PAPER")
            self.assertEqual(authority.endpoint, ENDPOINT)
            self.assertIs(authority.surface, Surface.AUTHENTICATED_READ)
            self.assertEqual(authority.permission_scope, PERMISSION)
            self.assertEqual(authority.parser_identity, PARSER_IDENTITY)
            self.assertEqual(authority.route_semantics_digest, binding.route_semantics_digest)
            self.assertTrue(authority.negative_result_rule_key.startswith("NEGATIVE_RESULT_RULE:"))
            self.assertTrue(authority.negative_result_rule_digest.startswith("sha256:"))
            self.assertTrue(
                authority.evidence_ref.startswith(
                    "qualified-negative-result-semantics:sha256:"
                )
            )

    def test_negative_result_semantics_constructor_is_sealed(self):
        with self.assertRaisesRegex(
            ProviderNegativeResultSemanticsError,
            "exact current qualification authority",
        ):
            QualifiedNegativeResultSemantics(
                qualification_id="provider-qualification:sha256:" + "1" * 64,
            )

    def test_signed_claim_for_different_parser_does_not_authorize_current_parser(self):
        with TemporaryDirectory() as directory:
            qualifications, route, binding, _q1, _harness = self.setup_route(
                directory,
                signed_negative=True,
                claim_parser="BYBIT_ORDER_V5_JSON_V2",
            )
            self.assertEqual(binding.parser_identity, PARSER_IDENTITY)
            with self.assertRaisesRegex(
                ProviderNegativeResultSemanticsError,
                "does not authorize negative-result semantics",
            ):
                require_qualified_negative_result_semantics(
                    route=route,
                    read_binding=binding,
                    qualification_registry=qualifications,
                    at=NOW,
                )

    def test_superseded_q_revokes_old_negative_result_semantics_authorization(self):
        with TemporaryDirectory() as directory:
            qualifications, route, binding, q1, harness = self.setup_route(
                directory,
                signed_negative=True,
            )
            q2, receipt2, protocol2 = successor_spot_q(
                old_qualification_id=q1.qualification_id,
                ordinal=612,
            )
            harness.register(
                protocol_key=protocol2.key,
                record=q2,
                receipt=receipt2,
            )
            qualifications._append_accepted(
                protocol_key=protocol2.key,
                record=q2,
                receipt=receipt2,
            )
            qualifications._append_supersession(
                old_id=q1.qualification_id,
                new_id=q2.qualification_id,
            )
            with self.assertRaisesRegex(
                ProviderNegativeResultSemanticsError,
                "not exact current",
            ):
                require_qualified_negative_result_semantics(
                    route=route,
                    read_binding=binding,
                    qualification_registry=qualifications,
                    at=NOW + timedelta(seconds=1),
                )


if __name__ == "__main__":
    unittest.main()
