from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.autotrade_mvp.provider_activity_window_observation import (
    ProviderActivityWindowObservationError,
    QualifiedActivityWindowNoMatchObservation,
    qualify_complete_bybit_activity_no_match,
)
from mvp.autotrade_mvp.provider_activity_window_semantics import (
    qualified_activity_window_route_semantic_claim,
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


ENDPOINT = "/v5/account/transaction-log"
CLIENT_ORDER_ID = "autotrade-crash-ambiguity-1"
PARSER_IDENTITY = "BYBIT_ORDER_V5_JSON_V1"


def accepted_q_for_activity_window(*, ordinal: int = 630, include_window_claim: bool = True):
    protocol = _protocol()
    raw_ref = _raw_ref(100 + ordinal)
    payload = _campaign_payload(raw_ref=raw_ref)
    payload["product_family"] = "SPOT"
    read_key, read_digest = qualified_read_route_semantic_claim(
        provider_id="BYBIT",
        endpoint=ENDPOINT,
        surface=Surface.AUTHENTICATED_READ,
        permission_scope="ACCOUNT.READ",
    )
    payload["route_semantics"][read_key] = read_digest
    if include_window_claim:
        window_key, window_digest = qualified_activity_window_route_semantic_claim(
            provider_id="BYBIT",
            endpoint=ENDPOINT,
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ACCOUNT.READ",
            parser_identity=PARSER_IDENTITY,
        )
        payload["route_semantics"][window_key] = window_digest
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


class ProviderActivityWindowObservationTests(unittest.TestCase):
    def setup_authority(self, directory: str, *, include_window_claim: bool = True):
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
        q1, receipt1, protocol1 = accepted_q_for_activity_window(
            include_window_claim=include_window_claim,
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

    def base_query(self):
        submitted_ms = int(NOW.timestamp() * 1000)
        return {
            "accountType": "UNIFIED",
            "category": "spot",
            "startTime": str(submitted_ms - 60_000),
            "endTime": str(submitted_ms + 60_000),
            "limit": "50",
        }

    def page(self, route, capabilities, qualifications, query, payload, *, seconds):
        binding = prepare_qualified_provider_read(
            route,
            capabilities,
            qualifications,
            surface=Surface.AUTHENTICATED_READ,
            endpoint=ENDPOINT,
            query=query,
            at=NOW + timedelta(milliseconds=seconds),
            permission_scope="ACCOUNT.READ",
        )
        return observe_qualified_provider_json_response(
            query_binding=binding,
            http_status=200,
            response_bytes=canonical_json(payload).encode("utf-8"),
            observed_at=NOW + timedelta(seconds=seconds),
        )

    def row(self, *, order_link_id: str = "", seconds: int = 0):
        return {
            "id": "activity-" + str(seconds),
            "category": "spot",
            "orderLinkId": order_link_id,
            "transactionTime": str(int((NOW + timedelta(seconds=seconds)).timestamp() * 1000)),
        }

    def two_pages(self, route, capabilities, qualifications, *, target_on_second=False):
        base = self.base_query()
        first = self.page(
            route,
            capabilities,
            qualifications,
            base,
            {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "list": [self.row(seconds=-1)],
                    "nextPageCursor": "cursor-2",
                },
            },
            seconds=1,
        )
        second_query = dict(base)
        second_query["cursor"] = "cursor-2"
        second = self.page(
            route,
            capabilities,
            qualifications,
            second_query,
            {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "list": [
                        self.row(
                            order_link_id=(CLIENT_ORDER_ID if target_on_second else "other-order"),
                            seconds=1,
                        )
                    ],
                    "nextPageCursor": "",
                },
            },
            seconds=2,
        )
        return first, second

    def test_complete_two_page_window_issues_activity_no_match_observation(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.setup_authority(directory)
            pages = self.two_pages(route, capabilities, qualifications)
            result = qualify_complete_bybit_activity_no_match(
                route=route,
                pages=pages,
                client_order_id=CLIENT_ORDER_ID,
                submission_at=NOW,
                qualification_registry=qualifications,
                at=NOW + timedelta(seconds=3),
            )
            self.assertEqual(result.surface, "ACTIVITIES")
            self.assertEqual(result.client_order_id, CLIENT_ORDER_ID)
            self.assertEqual(result.page_count, 2)
            self.assertEqual(len(result.query_digests), 2)
            self.assertEqual(len(result.provider_response_refs), 2)
            self.assertTrue(
                result.evidence_ref.startswith(
                    "qualified-activity-window-no-match:sha256:"
                )
            )

    def test_target_identity_on_any_page_prevents_no_match(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.setup_authority(directory)
            pages = self.two_pages(
                route,
                capabilities,
                qualifications,
                target_on_second=True,
            )
            with self.assertRaisesRegex(
                ProviderActivityWindowObservationError,
                "contains target client order identity",
            ):
                qualify_complete_bybit_activity_no_match(
                    route=route,
                    pages=pages,
                    client_order_id=CLIENT_ORDER_ID,
                    submission_at=NOW,
                    qualification_registry=qualifications,
                    at=NOW + timedelta(seconds=3),
                )

    def test_nonterminal_cursor_without_next_page_is_incomplete(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.setup_authority(directory)
            first, _second = self.two_pages(route, capabilities, qualifications)
            with self.assertRaisesRegex(
                ProviderActivityWindowObservationError,
                "page chain is incomplete",
            ):
                qualify_complete_bybit_activity_no_match(
                    route=route,
                    pages=(first,),
                    client_order_id=CLIENT_ORDER_ID,
                    submission_at=NOW,
                    qualification_registry=qualifications,
                    at=NOW + timedelta(seconds=3),
                )

    def test_cursor_query_mismatch_is_rejected(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.setup_authority(directory)
            base = self.base_query()
            first = self.page(
                route,
                capabilities,
                qualifications,
                base,
                {"retCode": 0, "result": {"list": [], "nextPageCursor": "cursor-2"}},
                seconds=1,
            )
            wrong_query = dict(base)
            wrong_query["cursor"] = "wrong-cursor"
            second = self.page(
                route,
                capabilities,
                qualifications,
                wrong_query,
                {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}},
                seconds=2,
            )
            with self.assertRaisesRegex(
                ProviderActivityWindowObservationError,
                "query drifted",
            ):
                qualify_complete_bybit_activity_no_match(
                    route=route,
                    pages=(first, second),
                    client_order_id=CLIENT_ORDER_ID,
                    submission_at=NOW,
                    qualification_registry=qualifications,
                    at=NOW + timedelta(seconds=3),
                )

    def test_cursor_cycle_is_rejected(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.setup_authority(directory)
            base = self.base_query()
            first = self.page(
                route,
                capabilities,
                qualifications,
                base,
                {"retCode": 0, "result": {"list": [], "nextPageCursor": "cursor-2"}},
                seconds=1,
            )
            second_query = dict(base)
            second_query["cursor"] = "cursor-2"
            second = self.page(
                route,
                capabilities,
                qualifications,
                second_query,
                {"retCode": 0, "result": {"list": [], "nextPageCursor": "cursor-2"}},
                seconds=2,
            )
            with self.assertRaisesRegex(
                ProviderActivityWindowObservationError,
                "cursor cycle",
            ):
                qualify_complete_bybit_activity_no_match(
                    route=route,
                    pages=(first, second),
                    client_order_id=CLIENT_ORDER_ID,
                    submission_at=NOW,
                    qualification_registry=qualifications,
                    at=NOW + timedelta(seconds=3),
                )

    def test_unsigned_activity_window_semantics_are_rejected(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.setup_authority(
                directory,
                include_window_claim=False,
            )
            base = self.base_query()
            page = self.page(
                route,
                capabilities,
                qualifications,
                base,
                {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}},
                seconds=1,
            )
            with self.assertRaisesRegex(
                Exception,
                "does not authorize complete paginated activity-window semantics",
            ):
                qualify_complete_bybit_activity_no_match(
                    route=route,
                    pages=(page,),
                    client_order_id=CLIENT_ORDER_ID,
                    submission_at=NOW,
                    qualification_registry=qualifications,
                    at=NOW + timedelta(seconds=2),
                )

    def test_activity_row_outside_window_is_rejected(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.setup_authority(directory)
            base = self.base_query()
            page = self.page(
                route,
                capabilities,
                qualifications,
                base,
                {
                    "retCode": 0,
                    "result": {
                        "list": [self.row(seconds=120)],
                        "nextPageCursor": "",
                    },
                },
                seconds=1,
            )
            with self.assertRaisesRegex(
                ProviderActivityWindowObservationError,
                "outside qualified window",
            ):
                qualify_complete_bybit_activity_no_match(
                    route=route,
                    pages=(page,),
                    client_order_id=CLIENT_ORDER_ID,
                    submission_at=NOW,
                    qualification_registry=qualifications,
                    at=NOW + timedelta(seconds=2),
                )

    def test_constructor_is_sealed(self):
        with self.assertRaisesRegex(
            ProviderActivityWindowObservationError,
            "complete provider page-chain authority",
        ):
            QualifiedActivityWindowNoMatchObservation(provider_id="BYBIT")


if __name__ == "__main__":
    unittest.main()
