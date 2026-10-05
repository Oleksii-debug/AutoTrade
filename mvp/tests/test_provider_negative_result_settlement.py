from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.autotrade_mvp.provider_activity_window_observation import (
    qualify_complete_bybit_activity_no_match,
)
from mvp.autotrade_mvp.provider_activity_window_semantics import (
    qualified_activity_window_route_semantic_claim,
)
from mvp.autotrade_mvp.provider_core import Surface
from mvp.autotrade_mvp.provider_negative_result_horizon import (
    ProviderNegativeResultHorizonError,
    qualified_negative_result_horizon_claim_key,
)
from mvp.autotrade_mvp.provider_negative_result_observation import (
    qualify_empty_bybit_identity_observation,
)
from mvp.autotrade_mvp.provider_negative_result_query_scope import (
    require_canonical_negative_result_query_scope,
)
from mvp.autotrade_mvp.provider_negative_result_semantics import (
    qualified_negative_result_route_semantic_claim,
)
from mvp.autotrade_mvp.provider_negative_result_settlement import (
    ProviderNegativeResultSettlementError,
    SettledNegativeSurfaceObservation,
    settle_activity_negative_surface,
    settle_direct_negative_surface,
)
from mvp.autotrade_mvp.provider_qualification_authority import (
    _derive_accepted_provider_qualification,
    parse_provider_qualification_campaign,
)
from mvp.autotrade_mvp.provider_route_reads import (
    observe_qualified_provider_json_response,
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


PARSER_IDENTITY = "BYBIT_ORDER_V5_JSON_V1"
CLIENT_ORDER_ID = "autotrade-crash-ambiguity-1"


def accepted_q_with_claims(*, claims: dict[str, str], ordinal: int):
    protocol = _protocol()
    raw_ref = _raw_ref(100 + ordinal)
    payload = _campaign_payload(raw_ref=raw_ref)
    payload["product_family"] = "SPOT"
    payload["route_semantics"].update(claims)
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


class ProviderNegativeResultSettlementTests(unittest.TestCase):
    def authorities(self, directory: str, *, claims: dict[str, str], ordinal: int):
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
        q1, receipt1, protocol1 = accepted_q_with_claims(
            claims=claims,
            ordinal=ordinal,
        )
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
        return capabilities, qualifications, selection.selected

    def direct_claims(
        self,
        *,
        endpoint: str,
        permission_scope: str = "ORDER.READ",
        horizon_ms: str | None = "2000",
    ):
        read_key, read_digest = qualified_read_route_semantic_claim(
            provider_id="BYBIT",
            endpoint=endpoint,
            surface=Surface.AUTHENTICATED_READ,
            permission_scope=permission_scope,
        )
        negative_key, negative_digest = qualified_negative_result_route_semantic_claim(
            provider_id="BYBIT",
            endpoint=endpoint,
            surface=Surface.AUTHENTICATED_READ,
            permission_scope=permission_scope,
            parser_identity=PARSER_IDENTITY,
        )
        claims = {
            read_key: read_digest,
            negative_key: negative_digest,
        }
        if horizon_ms is not None:
            horizon_key = qualified_negative_result_horizon_claim_key(
                provider_id="BYBIT",
                endpoint=endpoint,
                surface=Surface.AUTHENTICATED_READ,
                permission_scope=permission_scope,
                parser_identity=PARSER_IDENTITY,
            )
            claims[horizon_key] = horizon_ms
        return claims

    def direct_observation(
        self,
        *,
        capabilities,
        qualifications,
        route,
        endpoint: str,
        query: dict[str, str],
        observed_after_s: int,
    ):
        binding = prepare_qualified_provider_read(
            route,
            capabilities,
            qualifications,
            surface=Surface.AUTHENTICATED_READ,
            endpoint=endpoint,
            query=query,
            at=NOW,
            permission_scope="ORDER.READ",
        )
        query_scope = require_canonical_negative_result_query_scope(
            query_binding=binding.query_binding,
            client_order_id=CLIENT_ORDER_ID,
            submission_at=NOW,
        )
        qualified_response = observe_qualified_provider_json_response(
            query_binding=binding,
            http_status=200,
            response_bytes=b'{"retCode":0,"result":{"list":[],"nextPageCursor":""}}',
            observed_at=NOW + timedelta(seconds=observed_after_s),
        )
        negative = qualify_empty_bybit_identity_observation(
            route=route,
            read_binding=binding,
            query_scope=query_scope,
            response=qualified_response.observation,
            qualification_registry=qualifications,
            at=NOW + timedelta(seconds=observed_after_s),
        )
        return binding, query_scope, negative

    def test_direct_negative_settles_only_after_signed_horizon(self):
        endpoint = "/v5/order/realtime"
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.authorities(
                directory,
                claims=self.direct_claims(endpoint=endpoint),
                ordinal=640,
            )
            binding, query_scope, negative = self.direct_observation(
                capabilities=capabilities,
                qualifications=qualifications,
                route=route,
                endpoint=endpoint,
                query={"category": "spot", "orderLinkId": CLIENT_ORDER_ID},
                observed_after_s=3,
            )
            settled = settle_direct_negative_surface(
                route=route,
                read_binding=binding,
                query_scope=query_scope,
                observation=negative,
                qualification_registry=qualifications,
                submission_at=NOW,
                at=NOW + timedelta(seconds=3),
            )
            self.assertEqual(settled.surface, "OPEN_ORDERS")
            self.assertEqual(settled.minimum_horizon_ms, 2000)
            self.assertTrue(
                settled.evidence_ref.startswith(
                    "settled-negative-provider-surface:sha256:"
                )
            )

    def test_direct_negative_before_signed_horizon_remains_unsettled(self):
        endpoint = "/v5/order/realtime"
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.authorities(
                directory,
                claims=self.direct_claims(endpoint=endpoint),
                ordinal=641,
            )
            binding, query_scope, negative = self.direct_observation(
                capabilities=capabilities,
                qualifications=qualifications,
                route=route,
                endpoint=endpoint,
                query={"category": "spot", "orderLinkId": CLIENT_ORDER_ID},
                observed_after_s=1,
            )
            with self.assertRaisesRegex(
                ProviderNegativeResultSettlementError,
                "has not satisfied signed consistency horizon",
            ):
                settle_direct_negative_surface(
                    route=route,
                    read_binding=binding,
                    query_scope=query_scope,
                    observation=negative,
                    qualification_registry=qualifications,
                    submission_at=NOW,
                    at=NOW + timedelta(seconds=1),
                )

    def test_missing_signed_horizon_cannot_settle_negative(self):
        endpoint = "/v5/order/realtime"
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.authorities(
                directory,
                claims=self.direct_claims(endpoint=endpoint, horizon_ms=None),
                ordinal=642,
            )
            binding, query_scope, negative = self.direct_observation(
                capabilities=capabilities,
                qualifications=qualifications,
                route=route,
                endpoint=endpoint,
                query={"category": "spot", "orderLinkId": CLIENT_ORDER_ID},
                observed_after_s=3,
            )
            with self.assertRaisesRegex(
                ProviderNegativeResultHorizonError,
                "does not sign a negative-result consistency horizon",
            ):
                settle_direct_negative_surface(
                    route=route,
                    read_binding=binding,
                    query_scope=query_scope,
                    observation=negative,
                    qualification_registry=qualifications,
                    submission_at=NOW,
                    at=NOW + timedelta(seconds=3),
                )

    def test_order_history_future_window_cannot_settle(self):
        endpoint = "/v5/order/history"
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.authorities(
                directory,
                claims=self.direct_claims(endpoint=endpoint, horizon_ms="0"),
                ordinal=643,
            )
            submitted_ms = int(NOW.timestamp() * 1000)
            binding, query_scope, negative = self.direct_observation(
                capabilities=capabilities,
                qualifications=qualifications,
                route=route,
                endpoint=endpoint,
                query={
                    "category": "spot",
                    "orderLinkId": CLIENT_ORDER_ID,
                    "startTime": str(submitted_ms - 60_000),
                    "endTime": str(submitted_ms + 60_000),
                },
                observed_after_s=1,
            )
            with self.assertRaisesRegex(
                ProviderNegativeResultSettlementError,
                "ends after provider observation",
            ):
                settle_direct_negative_surface(
                    route=route,
                    read_binding=binding,
                    query_scope=query_scope,
                    observation=negative,
                    qualification_registry=qualifications,
                    submission_at=NOW,
                    at=NOW + timedelta(seconds=1),
                )

    def activity_claims(self, *, horizon_ms: str = "2000"):
        endpoint = "/v5/account/transaction-log"
        read_key, read_digest = qualified_read_route_semantic_claim(
            provider_id="BYBIT",
            endpoint=endpoint,
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ACCOUNT.READ",
        )
        window_key, window_digest = qualified_activity_window_route_semantic_claim(
            provider_id="BYBIT",
            endpoint=endpoint,
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ACCOUNT.READ",
            parser_identity=PARSER_IDENTITY,
        )
        horizon_key = qualified_negative_result_horizon_claim_key(
            provider_id="BYBIT",
            endpoint=endpoint,
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ACCOUNT.READ",
            parser_identity=PARSER_IDENTITY,
        )
        return {
            read_key: read_digest,
            window_key: window_digest,
            horizon_key: horizon_ms,
        }

    def test_activity_window_settles_after_signed_horizon_and_completed_window(self):
        endpoint = "/v5/account/transaction-log"
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.authorities(
                directory,
                claims=self.activity_claims(),
                ordinal=644,
            )
            submitted_ms = int(NOW.timestamp() * 1000)
            query = {
                "accountType": "UNIFIED",
                "category": "spot",
                "startTime": str(submitted_ms - 60_000),
                "endTime": str(submitted_ms),
                "limit": "50",
            }
            binding = prepare_qualified_provider_read(
                route,
                capabilities,
                qualifications,
                surface=Surface.AUTHENTICATED_READ,
                endpoint=endpoint,
                query=query,
                at=NOW + timedelta(seconds=3),
                permission_scope="ACCOUNT.READ",
            )
            page = observe_qualified_provider_json_response(
                query_binding=binding,
                http_status=200,
                response_bytes=b'{"retCode":0,"result":{"list":[],"nextPageCursor":""}}',
                observed_at=NOW + timedelta(seconds=3),
            )
            activity = qualify_complete_bybit_activity_no_match(
                route=route,
                pages=(page,),
                client_order_id=CLIENT_ORDER_ID,
                submission_at=NOW,
                qualification_registry=qualifications,
                at=NOW + timedelta(seconds=3),
            )
            settled = settle_activity_negative_surface(
                route=route,
                representative_read_binding=binding,
                observation=activity,
                qualification_registry=qualifications,
                submission_at=NOW,
                at=NOW + timedelta(seconds=3),
            )
            self.assertEqual(settled.surface, "ACTIVITIES")
            self.assertEqual(settled.minimum_horizon_ms, 2000)
            self.assertLessEqual(settled.coverage_end, settled.observed_at)

    def test_activity_future_window_cannot_settle(self):
        endpoint = "/v5/account/transaction-log"
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.authorities(
                directory,
                claims=self.activity_claims(horizon_ms="0"),
                ordinal=645,
            )
            submitted_ms = int(NOW.timestamp() * 1000)
            query = {
                "accountType": "UNIFIED",
                "category": "spot",
                "startTime": str(submitted_ms - 60_000),
                "endTime": str(submitted_ms + 60_000),
                "limit": "50",
            }
            binding = prepare_qualified_provider_read(
                route,
                capabilities,
                qualifications,
                surface=Surface.AUTHENTICATED_READ,
                endpoint=endpoint,
                query=query,
                at=NOW + timedelta(seconds=1),
                permission_scope="ACCOUNT.READ",
            )
            page = observe_qualified_provider_json_response(
                query_binding=binding,
                http_status=200,
                response_bytes=b'{"retCode":0,"result":{"list":[],"nextPageCursor":""}}',
                observed_at=NOW + timedelta(seconds=1),
            )
            activity = qualify_complete_bybit_activity_no_match(
                route=route,
                pages=(page,),
                client_order_id=CLIENT_ORDER_ID,
                submission_at=NOW,
                qualification_registry=qualifications,
                at=NOW + timedelta(seconds=1),
            )
            with self.assertRaisesRegex(
                ProviderNegativeResultSettlementError,
                "ends after final provider observation",
            ):
                settle_activity_negative_surface(
                    route=route,
                    representative_read_binding=binding,
                    observation=activity,
                    qualification_registry=qualifications,
                    submission_at=NOW,
                    at=NOW + timedelta(seconds=1),
                )

    def test_settled_surface_constructor_is_sealed(self):
        with self.assertRaisesRegex(
            ProviderNegativeResultSettlementError,
            "signed provider horizon authority",
        ):
            SettledNegativeSurfaceObservation(provider_id="BYBIT")


if __name__ == "__main__":
    unittest.main()
