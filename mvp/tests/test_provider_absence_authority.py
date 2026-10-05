from datetime import timedelta
import inspect
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.autotrade_mvp.provider_absence_authority import (
    ProviderAbsenceAuthorityError,
    QualifiedProviderAbsenceSemantics,
    qualified_absence_route_semantic_claim,
    require_qualified_absence_semantics,
)
from mvp.autotrade_mvp.provider_core import Surface
from mvp.autotrade_mvp.provider_qualification_authority import (
    _derive_accepted_provider_qualification,
    parse_provider_qualification_campaign,
)
from mvp.autotrade_mvp.provider_route_reads import (
    QualifiedProviderResponseObservation,
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


ENDPOINT = "/v5/execution/list"
SURFACE = "EXECUTIONS"


def accepted_absence_q(*, ordinal: int, include_absence_claim: bool):
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
    payload["route_semantics"][read_key] = read_digest
    if include_absence_claim:
        absence_key, absence_digest = qualified_absence_route_semantic_claim(
            provider_id="BYBIT",
            endpoint=ENDPOINT,
            reconciliation_surface=SURFACE,
        )
        payload["route_semantics"][absence_key] = absence_digest
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
        runner_id="runner-absence",
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
    def authorities(self, directory: str, *, include_absence_claim: bool = True):
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
        record, receipt, protocol = accepted_absence_q(
            ordinal=70 if include_absence_claim else 71,
            include_absence_claim=include_absence_claim,
        )
        harness.register(
            protocol_key=protocol.key,
            record=record,
            receipt=receipt,
        )
        qualifications._append_accepted(
            protocol_key=protocol.key,
            record=record,
            receipt=receipt,
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

    def observed_execution_read(self, route, capabilities, qualifications):
        binding = prepare_qualified_provider_read(
            route,
            capabilities,
            qualifications,
            surface=Surface.AUTHENTICATED_READ,
            endpoint=ENDPOINT,
            query={"category": "spot", "limit": "100"},
            at=NOW,
            permission_scope="ORDER.READ",
        )
        return observe_qualified_provider_json_response(
            query_binding=binding,
            http_status=200,
            response_bytes=b'{"retCode":0,"result":{"list":[],"nextPageCursor":""}}',
            observed_at=NOW + timedelta(seconds=1),
        )

    def semantics(self, observation, qualifications, *, surface=SURFACE):
        return require_qualified_absence_semantics(
            observation,
            qualifications,
            reconciliation_surface=surface,
        )

    def test_exact_q_claim_and_qualified_response_issue_semantic_capability_only(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.authorities(directory)
            observation = self.observed_execution_read(
                route,
                capabilities,
                qualifications,
            )
            authority = self.semantics(observation, qualifications)
            self.assertIsInstance(authority, QualifiedProviderAbsenceSemantics)
            self.assertEqual(authority.provider_id, "BYBIT")
            self.assertEqual(authority.account_id, "paper-account")
            self.assertEqual(authority.environment, "PAPER")
            self.assertEqual(authority.endpoint, ENDPOINT)
            self.assertEqual(authority.reconciliation_surface, SURFACE)
            self.assertTrue(
                authority.evidence_ref.startswith(
                    "qualified-provider-absence-semantics:sha256:"
                )
            )

    def test_semantic_issuer_has_no_caller_coverage_or_horizon_inputs(self):
        parameters = inspect.signature(
            require_qualified_absence_semantics
        ).parameters
        for forbidden in (
            "provider_id",
            "account_id",
            "environment",
            "coverage_start",
            "coverage_end",
            "pagination_complete",
            "consistency_horizon_satisfied",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, parameters)

    def test_read_rule_without_exact_absence_claim_stays_fail_closed(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.authorities(
                directory,
                include_absence_claim=False,
            )
            observation = self.observed_execution_read(
                route,
                capabilities,
                qualifications,
            )
            with self.assertRaisesRegex(
                ProviderAbsenceAuthorityError,
                "does not cover exact absence-semantics rule",
            ):
                self.semantics(observation, qualifications)

    def test_endpoint_cannot_be_relabelled_as_another_absence_surface(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.authorities(directory)
            observation = self.observed_execution_read(
                route,
                capabilities,
                qualifications,
            )
            with self.assertRaisesRegex(
                ProviderAbsenceAuthorityError,
                "endpoint does not match",
            ):
                self.semantics(
                    observation,
                    qualifications,
                    surface="ORDER_HISTORY",
                )

    def test_object_new_forged_qualified_response_has_no_absence_authority(self):
        with TemporaryDirectory() as directory:
            _capabilities, qualifications, _route = self.authorities(directory)
            forged = object.__new__(QualifiedProviderResponseObservation)
            with self.assertRaisesRegex(
                ProviderAbsenceAuthorityError,
                "construction authority is unavailable",
            ):
                self.semantics(forged, qualifications)

    def test_qualified_response_mutation_invalidates_absence_issuance(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.authorities(directory)
            observation = self.observed_execution_read(
                route,
                capabilities,
                qualifications,
            )
            object.__setattr__(
                observation.query_binding,
                "qualification_id",
                "provider-qualification:sha256:" + "0" * 64,
            )
            with self.assertRaisesRegex(
                ProviderAbsenceAuthorityError,
                "construction authority is unavailable",
            ):
                self.semantics(observation, qualifications)

    def test_semantics_constructor_is_sealed(self):
        with self.assertRaisesRegex(
            ProviderAbsenceAuthorityError,
            "must come from canonical provider-Q authority",
        ):
            QualifiedProviderAbsenceSemantics()

    def test_mutating_issued_semantics_invalidates_its_evidence_ref(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, route = self.authorities(directory)
            observation = self.observed_execution_read(
                route,
                capabilities,
                qualifications,
            )
            authority = self.semantics(observation, qualifications)
            object.__setattr__(
                authority,
                "reconciliation_surface",
                "ORDER_HISTORY",
            )
            with self.assertRaisesRegex(
                ProviderAbsenceAuthorityError,
                "changed after issuance",
            ):
                _ = authority.evidence_ref


if __name__ == "__main__":
    unittest.main()
