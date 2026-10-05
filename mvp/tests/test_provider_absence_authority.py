from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import inspect
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
import mvp.autotrade_mvp.provider_absence_authority as absence_module
from mvp.autotrade_mvp.provider_absence_authority import (
    ProviderAbsenceAuthorityError,
    QualifiedActivityNegativeSurface,
    QualifiedDirectNegativeSurface,
    QualifiedProviderAbsenceBundle,
    qualify_activity_negative_surface,
    qualify_direct_negative_surface,
    qualify_provider_absence_bundle,
    qualified_activity_negative_semantic_claim,
    qualified_direct_negative_semantic_claim,
    qualified_negative_horizon_claim_key,
)
from mvp.autotrade_mvp.provider_core import Surface
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
from mvp.tests.provider_qualification_test_support import ExactQualificationProjectionHarness
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


CLIENT_ORDER_ID = "autotrade-crash-ambiguity-1"
PARSER_IDENTITY = "BYBIT_ORDER_V5_JSON_V1"
DIRECT_ENDPOINTS = (
    "/v5/order/realtime",
    "/v5/order/history",
    "/v5/execution/list",
)
ACTIVITY_ENDPOINT = "/v5/account/transaction-log"


def accepted_absence_q(
    *,
    ordinal: int = 650,
    include_direct_negative: bool = True,
    include_activity_negative: bool = True,
    include_horizons: bool = True,
    horizon_ms: str = "2000",
):
    protocol = _protocol()
    raw_ref = _raw_ref(100 + ordinal)
    payload = _campaign_payload(raw_ref=raw_ref)
    payload["product_family"] = "SPOT"

    for endpoint in DIRECT_ENDPOINTS:
        read_key, read_digest = qualified_read_route_semantic_claim(
            provider_id="BYBIT",
            endpoint=endpoint,
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ORDER.READ",
        )
        payload["route_semantics"][read_key] = read_digest
        if include_direct_negative:
            key, digest = qualified_direct_negative_semantic_claim(
                provider_id="BYBIT",
                endpoint=endpoint,
                surface=Surface.AUTHENTICATED_READ,
                permission_scope="ORDER.READ",
                parser_identity=PARSER_IDENTITY,
            )
            payload["route_semantics"][key] = digest
        if include_horizons:
            horizon_key = qualified_negative_horizon_claim_key(
                provider_id="BYBIT",
                endpoint=endpoint,
                surface=Surface.AUTHENTICATED_READ,
                permission_scope="ORDER.READ",
                parser_identity=PARSER_IDENTITY,
            )
            payload["route_semantics"][horizon_key] = horizon_ms

    read_key, read_digest = qualified_read_route_semantic_claim(
        provider_id="BYBIT",
        endpoint=ACTIVITY_ENDPOINT,
        surface=Surface.AUTHENTICATED_READ,
        permission_scope="ACCOUNT.READ",
    )
    payload["route_semantics"][read_key] = read_digest
    if include_activity_negative:
        key, digest = qualified_activity_negative_semantic_claim(
            provider_id="BYBIT",
            endpoint=ACTIVITY_ENDPOINT,
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ACCOUNT.READ",
            parser_identity=PARSER_IDENTITY,
        )
        payload["route_semantics"][key] = digest
    if include_horizons:
        horizon_key = qualified_negative_horizon_claim_key(
            provider_id="BYBIT",
            endpoint=ACTIVITY_ENDPOINT,
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ACCOUNT.READ",
            parser_identity=PARSER_IDENTITY,
        )
        payload["route_semantics"][horizon_key] = horizon_ms

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


class ProviderAbsenceAuthorityTests(unittest.TestCase):
    def setup_route(
        self,
        directory: str,
        *,
        include_direct_negative: bool = True,
        include_activity_negative: bool = True,
        include_horizons: bool = True,
        horizon_ms: str = "2000",
        ordinal: int = 650,
    ):
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
        q1, receipt1, protocol1 = accepted_absence_q(
            ordinal=ordinal,
            include_direct_negative=include_direct_negative,
            include_activity_negative=include_activity_negative,
            include_horizons=include_horizons,
            horizon_ms=horizon_ms,
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

    def read_response(
        self,
        *,
        route,
        capabilities,
        qualifications,
        endpoint: str,
        query: dict[str, str],
        permission_scope: str,
        observed_after_s: int = 3,
        result_rows=(),
        next_cursor: str = "",
    ):
        binding = prepare_qualified_provider_read(
            route,
            capabilities,
            qualifications,
            surface=Surface.AUTHENTICATED_READ,
            endpoint=endpoint,
            query=query,
            at=NOW,
            permission_scope=permission_scope,
        )
        payload = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": list(result_rows),
                "nextPageCursor": next_cursor,
            },
        }
        return observe_qualified_provider_json_response(
            query_binding=binding,
            http_status=200,
            response_bytes=canonical_json(payload).encode("utf-8"),
            observed_at=NOW + timedelta(seconds=observed_after_s),
        )

    def direct_surface(
        self,
        *,
        route,
        capabilities,
        qualifications,
        endpoint: str,
        observed_after_s: int = 3,
        history_end_offset_ms: int = 0,
    ):
        query = {"category": "spot", "orderLinkId": CLIENT_ORDER_ID}
        if endpoint == "/v5/order/history":
            submitted_ms = int(NOW.timestamp() * 1000)
            query.update(
                {
                    "startTime": str(submitted_ms - 60_000),
                    "endTime": str(submitted_ms + history_end_offset_ms),
                }
            )
        response = self.read_response(
            route=route,
            capabilities=capabilities,
            qualifications=qualifications,
            endpoint=endpoint,
            query=query,
            permission_scope="ORDER.READ",
            observed_after_s=observed_after_s,
        )
        return qualify_direct_negative_surface(
            route=route,
            response=response,
            client_order_id=CLIENT_ORDER_ID,
            submission_at=NOW,
            qualification_registry=qualifications,
            at=NOW + timedelta(seconds=observed_after_s),
        )

    def activity_surface(
        self,
        *,
        route,
        capabilities,
        qualifications,
        observed_after_s: int = 3,
        end_offset_ms: int = 0,
        next_cursor: str = "",
        result_rows=(),
    ):
        submitted_ms = int(NOW.timestamp() * 1000)
        query = {
            "accountType": "UNIFIED",
            "category": "spot",
            "startTime": str(submitted_ms - 60_000),
            "endTime": str(submitted_ms + end_offset_ms),
            "limit": "50",
        }
        page = self.read_response(
            route=route,
            capabilities=capabilities,
            qualifications=qualifications,
            endpoint=ACTIVITY_ENDPOINT,
            query=query,
            permission_scope="ACCOUNT.READ",
            observed_after_s=observed_after_s,
            next_cursor=next_cursor,
            result_rows=result_rows,
        )
        return qualify_activity_negative_surface(
            route=route,
            pages=(page,),
            client_order_id=CLIENT_ORDER_ID,
            submission_at=NOW,
            qualification_registry=qualifications,
            at=NOW + timedelta(seconds=observed_after_s),
        )

    def test_minting_registers_and_raw_impls_are_not_module_attributes(self):
        forbidden = (
            "_register_direct_negative_surface_authority",
            "_register_activity_negative_surface_authority",
            "_register_provider_absence_bundle_authority",
            "_qualify_direct_negative_surface_impl",
            "_qualify_activity_negative_surface_impl",
            "_qualify_provider_absence_bundle_impl",
            "_issue_direct_negative_surface",
            "_issue_activity_negative_surface",
            "_issue_provider_absence_bundle",
        )
        for name in forbidden:
            with self.subTest(name=name):
                self.assertFalse(hasattr(absence_module, name))
        for fn in (
            qualify_direct_negative_surface,
            qualify_activity_negative_surface,
            qualify_provider_absence_bundle,
        ):
            self.assertNotIn("_register_authority", inspect.signature(fn).parameters)

    def test_output_constructors_are_sealed(self):
        with self.assertRaisesRegex(ProviderAbsenceAuthorityError, "sealed provider absence"):
            QualifiedDirectNegativeSurface(provider_id="BYBIT")
        with self.assertRaisesRegex(ProviderAbsenceAuthorityError, "sealed provider absence"):
            QualifiedActivityNegativeSurface(provider_id="BYBIT")
        with self.assertRaisesRegex(ProviderAbsenceAuthorityError, "sealed four-surface"):
            QualifiedProviderAbsenceBundle(provider_id="BYBIT")

    def test_exact_signed_direct_empty_response_after_horizon_is_qualified(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.setup_route(directory)
            value = self.direct_surface(
                route=route,
                capabilities=capabilities,
                qualifications=qualifications,
                endpoint="/v5/order/realtime",
            )
            self.assertEqual(value.surface, "OPEN_ORDERS")
            self.assertEqual(value.client_order_id, CLIENT_ORDER_ID)
            self.assertEqual(value.minimum_horizon_ms, 2000)
            self.assertTrue(value.evidence_ref.startswith("qualified-direct-negative-surface:sha256:"))

    def test_direct_empty_response_without_signed_negative_semantics_is_rejected(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.setup_route(
                directory,
                include_direct_negative=False,
                ordinal=651,
            )
            with self.assertRaisesRegex(
                ProviderAbsenceAuthorityError,
                "does not sign direct negative-result semantics",
            ):
                self.direct_surface(
                    route=route,
                    capabilities=capabilities,
                    qualifications=qualifications,
                    endpoint="/v5/order/realtime",
                )

    def test_direct_empty_response_before_signed_horizon_is_rejected(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.setup_route(directory, ordinal=652)
            with self.assertRaisesRegex(
                ProviderAbsenceAuthorityError,
                "has not satisfied signed consistency horizon",
            ):
                self.direct_surface(
                    route=route,
                    capabilities=capabilities,
                    qualifications=qualifications,
                    endpoint="/v5/execution/list",
                    observed_after_s=1,
                )

    def test_direct_query_with_pagination_or_extra_filter_is_rejected(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.setup_route(directory, ordinal=653)
            response = self.read_response(
                route=route,
                capabilities=capabilities,
                qualifications=qualifications,
                endpoint="/v5/order/realtime",
                query={
                    "category": "spot",
                    "orderLinkId": CLIENT_ORDER_ID,
                    "limit": "50",
                },
                permission_scope="ORDER.READ",
            )
            with self.assertRaisesRegex(
                ProviderAbsenceAuthorityError,
                "filters or pagination",
            ):
                qualify_direct_negative_surface(
                    route=route,
                    response=response,
                    client_order_id=CLIENT_ORDER_ID,
                    submission_at=NOW,
                    qualification_registry=qualifications,
                    at=NOW + timedelta(seconds=3),
                )

    def test_history_window_that_ends_after_observation_is_rejected(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.setup_route(directory, ordinal=654)
            with self.assertRaisesRegex(
                ProviderAbsenceAuthorityError,
                "ends after provider observation",
            ):
                self.direct_surface(
                    route=route,
                    capabilities=capabilities,
                    qualifications=qualifications,
                    endpoint="/v5/order/history",
                    observed_after_s=3,
                    history_end_offset_ms=60_000,
                )

    def test_complete_activity_window_after_horizon_is_qualified(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.setup_route(directory, ordinal=655)
            value = self.activity_surface(
                route=route,
                capabilities=capabilities,
                qualifications=qualifications,
            )
            self.assertEqual(value.surface, "ACTIVITIES")
            self.assertEqual(value.page_count, 1)
            self.assertEqual(value.minimum_horizon_ms, 2000)
            self.assertTrue(value.evidence_ref.startswith("qualified-activity-negative-surface:sha256:"))

    def test_incomplete_activity_cursor_chain_is_rejected(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.setup_route(directory, ordinal=656)
            with self.assertRaisesRegex(
                ProviderAbsenceAuthorityError,
                "page chain is incomplete",
            ):
                self.activity_surface(
                    route=route,
                    capabilities=capabilities,
                    qualifications=qualifications,
                    next_cursor="page-2",
                )

    def test_activity_row_with_target_identity_is_rejected(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.setup_route(directory, ordinal=657)
            row = {
                "category": "spot",
                "orderLinkId": CLIENT_ORDER_ID,
                "transactionTime": str(int(NOW.timestamp() * 1000)),
            }
            with self.assertRaisesRegex(
                ProviderAbsenceAuthorityError,
                "contains target client order identity",
            ):
                self.activity_surface(
                    route=route,
                    capabilities=capabilities,
                    qualifications=qualifications,
                    result_rows=(row,),
                )

    def test_activity_window_that_ends_after_final_observation_is_rejected(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.setup_route(directory, ordinal=658)
            with self.assertRaisesRegex(
                ProviderAbsenceAuthorityError,
                "ends after final provider observation",
            ):
                self.activity_surface(
                    route=route,
                    capabilities=capabilities,
                    qualifications=qualifications,
                    end_offset_ms=60_000,
                )

    def test_four_exact_surfaces_form_bundle_but_not_journal_proven_absent(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.setup_route(directory, ordinal=659)
            direct = tuple(
                self.direct_surface(
                    route=route,
                    capabilities=capabilities,
                    qualifications=qualifications,
                    endpoint=endpoint,
                )
                for endpoint in DIRECT_ENDPOINTS
            )
            activity = self.activity_surface(
                route=route,
                capabilities=capabilities,
                qualifications=qualifications,
            )
            bundle = qualify_provider_absence_bundle(
                route=route,
                direct_surfaces=direct,
                activity_surface=activity,
                qualification_registry=qualifications,
                at=NOW + timedelta(seconds=3),
            )
            self.assertEqual(
                tuple(surface for surface, _ref in bundle.surface_evidence),
                ("OPEN_ORDERS", "ORDER_HISTORY", "EXECUTIONS", "ACTIVITIES"),
            )
            self.assertEqual(bundle.client_order_id, CLIENT_ORDER_ID)
            self.assertTrue(bundle.evidence_ref.startswith("qualified-provider-absence-bundle:sha256:"))

    def test_bundle_rejects_duplicate_or_missing_direct_surface(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.setup_route(directory, ordinal=660)
            open_orders = self.direct_surface(
                route=route,
                capabilities=capabilities,
                qualifications=qualifications,
                endpoint="/v5/order/realtime",
            )
            executions = self.direct_surface(
                route=route,
                capabilities=capabilities,
                qualifications=qualifications,
                endpoint="/v5/execution/list",
            )
            activity = self.activity_surface(
                route=route,
                capabilities=capabilities,
                qualifications=qualifications,
            )
            with self.assertRaisesRegex(
                ProviderAbsenceAuthorityError,
                "requires OPEN_ORDERS, ORDER_HISTORY and EXECUTIONS",
            ):
                qualify_provider_absence_bundle(
                    route=route,
                    direct_surfaces=(open_orders, open_orders, executions),
                    activity_surface=activity,
                    qualification_registry=qualifications,
                    at=NOW + timedelta(seconds=3),
                )


if __name__ == "__main__":
    unittest.main()
