from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.autotrade_mvp.provider_core import Surface
from mvp.autotrade_mvp.provider_funding_income import (
    ProviderFundingIncomeError,
    ProviderFundingIncomeObservation,
    bybit_funding_income_observations,
    require_provider_funding_income_authority,
)
from mvp.autotrade_mvp.provider_origin import (
    ProviderOriginJournal,
    execute_direct_provider_origin_read,
    observe_provider_origin_json_response,
)
from mvp.autotrade_mvp.provider_qualification_authority import (
    _derive_accepted_provider_qualification,
    parse_provider_qualification_campaign,
)
from mvp.autotrade_mvp.provider_route_reads import (
    prepare_qualified_provider_read,
    qualified_read_route_semantic_claim,
)
from mvp.autotrade_mvp.provider_selection import (
    ProviderCandidate,
    ProviderRouteRequest,
    select_provider,
)
from mvp.autotrade_mvp.provider_transport import (
    BYBIT_V5_ENDPOINT_POLICIES,
    BybitV5AuthenticatedReadTransport,
    UrllibJsonWireClient,
)
from mvp.autotrade_mvp.qualification_attestation import (
    AcceptedQualificationAttestation,
    EvidenceArtifactRef,
    QualificationAttestation,
    SignedQualificationAttestation,
)
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle
from mvp.tests.provider_qualification_test_support import ExactQualificationProjectionHarness
from mvp.tests.test_provider_qualification_authority import (
    CAMPAIGN_KIND,
    PACKAGE_DIGEST,
    POLICY_ID,
    ROOT_ID,
    SOURCE_SHA,
    _artifact_id,
    _campaign_payload,
    _protocol,
    _raw_ref,
)
from mvp.tests.test_provider_route_reads import verified_read_capability
from mvp.tests.test_provider_selection import INSTRUMENT, NOW


ENDPOINT = "/v5/account/transaction-log"
PARSER = "BYBIT_V5_TRANSACTION_LOG_FUNDING_V1"


class _Resolver:
    @contextmanager
    def lease_for_execution(self, *_args, **_kwargs):
        yield '{"api_key":"SYNTHETIC-KEY","api_secret":"SYNTHETIC-SECRET"}'


class ProviderFundingIncomeTests(unittest.TestCase):
    def _qualification(self, *, ordinal: int, parser: str = PARSER):
        protocol = _protocol()
        raw_ref = _raw_ref(700 + ordinal)
        payload = _campaign_payload(route_parser=parser, raw_ref=raw_ref)
        payload["campaign_id"] = "bybit-linear-funding-transaction-log"
        payload["product_family"] = "LINEAR_DERIVATIVES"
        claim_key, claim_digest = qualified_read_route_semantic_claim(
            provider_id="BYBIT",
            endpoint=ENDPOINT,
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ACCOUNT.READ",
        )
        payload["route_semantics"][claim_key] = claim_digest
        payload["route_semantics"] = dict(sorted(payload["route_semantics"].items()))
        campaign_raw = canonical_json(payload).encode("utf-8")
        campaign_ref = EvidenceArtifactRef(
            artifact_id=_artifact_id(700 + ordinal),
            sha256="sha256:" + sha256(campaign_raw).hexdigest(),
            media_type="application/json",
            evidence_kind=CAMPAIGN_KIND,
            source_sha=SOURCE_SHA,
        )
        campaign = parse_provider_qualification_campaign(campaign_raw)
        attestation = QualificationAttestation(
            attestation_id=_artifact_id(800 + ordinal),
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
            runner_id="runner-funding",
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

    def _origin_observation(self, directory: str, body: bytes, *, parser: str = PARSER):
        journal = JournalStore(Path(directory) / "journal.sqlite3")
        capabilities = DurableCapabilityRegistry(journal)
        capabilities.add(
            verified_read_capability(
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                NOW - timedelta(minutes=1),
                permission_scopes=frozenset(
                    {"ORDER.READ", "ORDER.WRITE", "ACCOUNT.READ"}
                ),
                data_entitlements=frozenset({"ACTIVITIES"}),
            )
        )
        evidence_root = Path(directory) / "qualification-evidence"
        harness = ExactQualificationProjectionHarness().start()
        self.addCleanup(harness.stop)
        qualifications = harness.registry(
            journal,
            evidence_store=ArtifactStore(evidence_root),
            evidence_root=evidence_root,
        )
        qualification, receipt, protocol = self._qualification(ordinal=1, parser=parser)
        harness.register(
            protocol_key=protocol.key,
            record=qualification,
            receipt=receipt,
        )
        qualifications._append_accepted(
            protocol_key=protocol.key,
            record=qualification,
            receipt=receipt,
        )
        candidate = ProviderCandidate(
            provider_id="BYBIT",
            product_family="LINEAR_DERIVATIVES",
            provider_environment="TESTNET",
            account_id="paper-account",
            entity_id="entity-1",
            entity_policy_id="LINEAR_ORDER_V1",
            adapter_code_sha=SOURCE_SHA,
            packaged_artifact_digest=PACKAGE_DIGEST,
            protocol_id="provider-route-v1",
            protocol_version="1.0.0",
        )
        request = ProviderRouteRequest(
            asset_class="LINEAR_PERPETUAL",
            environment="PAPER",
            instrument_version=INSTRUMENT,
            order_type="LIMIT",
            time_in_force="DAY",
            permission_scope="ORDER.WRITE",
        )
        selection = select_provider(
            request,
            [candidate],
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
            query={
                "accountType": "UNIFIED",
                "category": "linear",
                "type": "SETTLEMENT",
            },
            at=NOW,
            permission_scope="ACCOUNT.READ",
        )

        class Stream(BytesIO):
            status = 200

        client = UrllibJsonWireClient(max_response_bytes=16 * 1024)
        client._opener.open = lambda *_args, **_kwargs: Stream(body)
        base = binding.query_binding
        transport = BybitV5AuthenticatedReadTransport(
            policy=BYBIT_V5_ENDPOINT_POLICIES["TESTNET"],
            provider_environment="TESTNET",
            account_id=base.account_id,
            capability_snapshot_id=base.capability_snapshot_id,
            capability_registry=capabilities,
            secret_resolver=_Resolver(),
            credential_handle=PersistentCredentialHandle(
                handle_id="provider-funding-income-test",
                account_id=base.account_id,
                provider="BYBIT",
                environment=base.environment,
                provider_environment="TESTNET",
                purpose="READ",
                generation=1,
            ),
            session_token="provider-funding-income-session",
            origin="https://localhost",
            execution_identity="provider-funding-income-host",
            clock_millis=lambda: 1_728_000_000_000,
            clock_utc=lambda: NOW,
            wire_client=client,
        )
        origin = ProviderOriginJournal(
            journal,
            response_store=ArtifactStore(Path(directory) / "provider-origin"),
        )
        response_binding = execute_direct_provider_origin_read(
            origin=origin,
            route=route,
            capability_registry=capabilities,
            qualification_registry=qualifications,
            query_binding=binding,
            transport=transport,
        )
        observation = observe_provider_origin_json_response(
            response_binding=response_binding,
            query_binding=binding,
        )
        return observation

    @staticmethod
    def _body(*, duplicate=False, ret_code=0):
        settlement = {
            "transSubType": "",
            "id": "592324_XRPUSDT_161440249321",
            "symbol": "XRPUSDT",
            "side": "Buy",
            "funding": "-0.003676",
            "orderLinkId": "",
            "orderId": "1672128000-8-592324-1-2",
            "fee": "0.00000000",
            "change": "-0.003676",
            "cashFlow": "0",
            "transactionTime": "1672128000000",
            "type": "SETTLEMENT",
            "feeRate": "0.0001",
            "bonusChange": "",
            "size": "100",
            "qty": "100",
            "cashBalance": "5086.55825002",
            "currency": "USDT",
            "category": "linear",
            "tradePrice": "0.3676",
            "tradeId": "534c0003-4bf7-486f-aa02-78cee36825e4",
            "extraFees": "",
        }
        trade = dict(settlement)
        trade.update(
            {
                "id": "trade-row",
                "type": "TRADE",
                "funding": "",
                "transactionTime": "1672121182224",
            }
        )
        rows = [settlement, trade]
        if duplicate:
            rows.append(dict(settlement))
        return json.dumps(
            {
                "retCode": ret_code,
                "retMsg": "OK" if ret_code == 0 else "ERROR",
                "result": {"nextPageCursor": "", "list": rows},
                "retExtInfo": {},
                "time": 1672132481405,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def test_genuine_direct_origin_projects_only_native_funding_income_fact(self):
        with TemporaryDirectory() as directory:
            origin = self._origin_observation(directory, self._body())
            observations = bybit_funding_income_observations(origin)
            self.assertEqual(len(observations), 1)
            funding = observations[0]
            self.assertIs(require_provider_funding_income_authority(funding), funding)
            self.assertEqual(funding.provider_transaction_id, "592324_XRPUSDT_161440249321")
            self.assertEqual(funding.instrument_id, "XRPUSDT")
            self.assertEqual(funding.settlement_currency, "USDT")
            self.assertEqual(str(funding.funding_amount), "-0.003676")
            self.assertEqual(funding.provider_environment, "TESTNET")
            self.assertEqual(funding.origin_ref, origin.origin_ref)
            self.assertEqual(funding.qualified_evidence_ref, origin.qualified_evidence_ref)
            self.assertFalse(hasattr(funding, "position_size"))
            self.assertFalse(hasattr(funding, "funding_rate"))
            self.assertFalse(hasattr(funding, "mark_price"))
            self.assertTrue(funding.evidence_ref.startswith("sha256:"))

    def test_projected_income_constructor_and_post_issue_mutation_are_sealed(self):
        with self.assertRaisesRegex(
            ProviderFundingIncomeError,
            "must come from qualified provider-origin bytes",
        ):
            ProviderFundingIncomeObservation()

        with TemporaryDirectory() as directory:
            origin = self._origin_observation(directory, self._body())
            funding = bybit_funding_income_observations(origin)[0]
            object.__setattr__(funding, "funding_amount", Decimal("999"))
            with self.assertRaisesRegex(
                ProviderFundingIncomeError,
                "changed after qualified origin projection",
            ):
                require_provider_funding_income_authority(funding)

    def test_wrong_qualified_parser_identity_cannot_mint_funding_income(self):
        with TemporaryDirectory() as directory:
            origin = self._origin_observation(
                directory,
                self._body(),
                parser="BYBIT_ORDER_V5_JSON_V1",
            )
            with self.assertRaisesRegex(
                ProviderFundingIncomeError,
                "funding transaction-log semantics",
            ):
                bybit_funding_income_observations(origin)

    def test_duplicate_provider_transaction_id_fails_closed(self):
        with TemporaryDirectory() as directory:
            origin = self._origin_observation(directory, self._body(duplicate=True))
            with self.assertRaisesRegex(
                ProviderFundingIncomeError,
                "duplicated",
            ):
                bybit_funding_income_observations(origin)

    def test_non_success_provider_envelope_cannot_be_funding_income(self):
        with TemporaryDirectory() as directory:
            origin = self._origin_observation(directory, self._body(ret_code=10001))
            with self.assertRaisesRegex(
                ProviderFundingIncomeError,
                "not successful",
            ):
                bybit_funding_income_observations(origin)

    def test_mutated_origin_wrapper_fails_closed(self):
        with TemporaryDirectory() as directory:
            origin = self._origin_observation(directory, self._body())
            object.__setattr__(origin, "qualified_observation", object())
            with self.assertRaises(ProviderFundingIncomeError):
                bybit_funding_income_observations(origin)


if __name__ == "__main__":
    unittest.main()
