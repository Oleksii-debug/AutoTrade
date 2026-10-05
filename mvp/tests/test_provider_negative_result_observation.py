from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
)
from mvp.autotrade_mvp.provider_negative_result_observation import (
    ProviderNegativeResultObservationError,
    QualifiedEmptyIdentityObservation,
    qualify_empty_bybit_identity_observation,
)
from mvp.autotrade_mvp.provider_negative_result_query_scope import (
    require_canonical_negative_result_query_scope,
)
from mvp.autotrade_mvp.provider_negative_result_semantics import (
    qualified_negative_result_route_semantic_claim,
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
from mvp.tests.test_provider_selection import NOW, candidate, request as route_request


ENDPOINT = "/v5/order/realtime"
CLIENT_ORDER_ID = "autotrade-crash-ambiguity-1"
PARSER_IDENTITY = "BYBIT_ORDER_V5_JSON_V1"


def accepted_q_for_realtime_negative(*, ordinal: int = 620):
    protocol = _protocol()
    raw_ref = _raw_ref(100 + ordinal)
    payload = _campaign_payload(raw_ref=raw_ref)
    payload["product_family"] = "SPOT"
    read_key, read_digest = qualified_read_route_semantic_claim(
        provider_id="BYBIT",
        endpoint=ENDPOINT,
        surface=Surface.AUTHENTICATED_READ,
        permission_scope="ORDER.READ",
    )
    negative_key, negative_digest = qualified_negative_result_route_semantic_claim(
        provider_id="BYBIT",
        endpoint=ENDPOINT,
        surface=Surface.AUTHENTICATED_READ,
        permission_scope="ORDER.READ",
        parser_identity=PARSER_IDENTITY,
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


class ProviderNegativeResultObservationTests(unittest.TestCase):
    def setup_authority(self, directory: str):
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
        q1, receipt1, protocol1 = accepted_q_for_realtime_negative()
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
        route = selection.selected
        self.assertIsNotNone(route)
        binding = prepare_qualified_provider_read(
            route,
            capabilities,
            qualifications,
            surface=Surface.AUTHENTICATED_READ,
            endpoint=ENDPOINT,
            query={"category": "spot", "orderLinkId": CLIENT_ORDER_ID},
            at=NOW,
            permission_scope="ORDER.READ",
        )
        query_scope = require_canonical_negative_result_query_scope(
            query_binding=binding.query_binding,
            client_order_id=CLIENT_ORDER_ID,
            submission_at=NOW,
        )
        return qualifications, route, binding, query_scope

    def response(self, binding, raw: bytes):
        return observe_authenticated_json_response(
            query_binding=binding.query_binding,
            http_status=200,
            response_bytes=raw,
            observed_at=NOW + timedelta(seconds=1),
        )

    def test_exact_empty_response_is_qualified_for_one_surface_only(self):
        with TemporaryDirectory() as directory:
            qualifications, route, binding, query_scope = self.setup_authority(directory)
            response = self.response(
                binding,
                b'{"retCode":0,"retMsg":"OK","result":{"list":[],"nextPageCursor":""},"retExtInfo":{},"time":1}',
            )
            observation = qualify_empty_bybit_identity_observation(
                route=route,
                read_binding=binding,
                query_scope=query_scope,
                response=response,
                qualification_registry=qualifications,
                at=NOW + timedelta(seconds=2),
            )
            self.assertEqual(observation.surface, "OPEN_ORDERS")
            self.assertEqual(observation.client_order_id, CLIENT_ORDER_ID)
            self.assertEqual(observation.qualification_id, binding.qualification_id)
            self.assertEqual(observation.qualified_query_digest, binding.query_digest)
            self.assertEqual(observation.provider_response_sha256, response.response_sha256)
            self.assertTrue(
                observation.evidence_ref.startswith(
                    "qualified-empty-provider-identity:sha256:"
                )
            )

    def test_matching_record_prevents_empty_observation(self):
        with TemporaryDirectory() as directory:
            qualifications, route, binding, query_scope = self.setup_authority(directory)
            response = self.response(
                binding,
                b'{"retCode":0,"result":{"list":[{"orderLinkId":"autotrade-crash-ambiguity-1"}]}}',
            )
            with self.assertRaisesRegex(
                ProviderNegativeResultObservationError,
                "contains matching records",
            ):
                qualify_empty_bybit_identity_observation(
                    route=route,
                    read_binding=binding,
                    query_scope=query_scope,
                    response=response,
                    qualification_registry=qualifications,
                    at=NOW + timedelta(seconds=2),
                )

    def test_nonempty_cursor_prevents_empty_observation(self):
        with TemporaryDirectory() as directory:
            qualifications, route, binding, query_scope = self.setup_authority(directory)
            response = self.response(
                binding,
                b'{"retCode":0,"result":{"list":[],"nextPageCursor":"page-2"}}',
            )
            with self.assertRaisesRegex(
                ProviderNegativeResultObservationError,
                "paginated",
            ):
                qualify_empty_bybit_identity_observation(
                    route=route,
                    read_binding=binding,
                    query_scope=query_scope,
                    response=response,
                    qualification_registry=qualifications,
                    at=NOW + timedelta(seconds=2),
                )

    def test_provider_error_response_prevents_empty_observation(self):
        with TemporaryDirectory() as directory:
            qualifications, route, binding, query_scope = self.setup_authority(directory)
            response = self.response(
                binding,
                b'{"retCode":10001,"retMsg":"bad request","result":{"list":[]}}',
            )
            with self.assertRaisesRegex(
                ProviderNegativeResultObservationError,
                "successful retCode",
            ):
                qualify_empty_bybit_identity_observation(
                    route=route,
                    read_binding=binding,
                    query_scope=query_scope,
                    response=response,
                    qualification_registry=qualifications,
                    at=NOW + timedelta(seconds=2),
                )

    def test_response_from_different_binding_is_rejected(self):
        with TemporaryDirectory() as directory:
            qualifications, route, binding, query_scope = self.setup_authority(directory)
            other = prepare_qualified_provider_read(
                route,
                DurableCapabilityRegistry(qualifications.store),
                qualifications,
                surface=Surface.AUTHENTICATED_READ,
                endpoint=ENDPOINT,
                query={"category": "spot", "orderLinkId": CLIENT_ORDER_ID},
                at=NOW,
                permission_scope="ORDER.READ",
            )
            response = self.response(other, b'{"retCode":0,"result":{"list":[]}}')
            with self.assertRaisesRegex(
                ProviderNegativeResultObservationError,
                "does not belong to exact qualified read binding",
            ):
                qualify_empty_bybit_identity_observation(
                    route=route,
                    read_binding=binding,
                    query_scope=query_scope,
                    response=response,
                    qualification_registry=qualifications,
                    at=NOW + timedelta(seconds=2),
                )

    def test_constructor_is_sealed(self):
        with self.assertRaisesRegex(
            ProviderNegativeResultObservationError,
            "exact provider evidence authority",
        ):
            QualifiedEmptyIdentityObservation(provider_id="BYBIT")


if __name__ == "__main__":
    unittest.main()
