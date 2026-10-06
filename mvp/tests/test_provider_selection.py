from datetime import datetime, timedelta, timezone, tzinfo
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.durable_provider_qualification import (
    DurableProviderQualificationRegistry,
)
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.autotrade_mvp.provider_core import Surface
from mvp.autotrade_mvp.provider_qualification_authority import (
    _derive_accepted_provider_qualification,
    parse_provider_qualification_campaign,
)
from mvp.autotrade_mvp.provider_route_reads import (
    qualified_read_route_semantic_claim,
)
from mvp.autotrade_mvp.provider_selection import (
    ProviderCandidate,
    ProviderRouteRequest,
    ProviderSelectionError,
    SelectedProviderRoute,
    select_provider,
)
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
    PACKAGE_DIGEST,
    POLICY_ID,
    ROOT_ID,
    SOURCE_SHA,
    _ProjectionOnlyRegistry,
    _artifact_id,
    _campaign_payload,
    _protocol,
    _raw_ref,
)


NOW = datetime(2026, 10, 4, 5, 5, tzinfo=timezone.utc)
INSTRUMENT = "instrument-v1"


def request(**overrides):
    values = dict(
        asset_class="CRYPTO_SPOT",
        environment="PAPER",
        instrument_version=INSTRUMENT,
        order_type="LIMIT",
        time_in_force="DAY",
        permission_scope="ORDER.WRITE",
    )
    values.update(overrides)
    return ProviderRouteRequest(**values)


def candidate(*, provider_environment="TESTNET", package_digest=PACKAGE_DIGEST):
    return ProviderCandidate(
        provider_id="BYBIT",
        product_family="SPOT",
        provider_environment=provider_environment,
        account_id="paper-account",
        entity_id="entity-1",
        entity_policy_id="LINEAR_ORDER_V1",
        adapter_code_sha=SOURCE_SHA,
        packaged_artifact_digest=package_digest,
        protocol_id="provider-route-v1",
        protocol_version="1.0.0",
    )


def accepted_spot_q(
    *,
    ordinal=40,
    unsupported=(),
    include_read_rule=True,
    product_family="SPOT",
    extra_route_semantics=None,
):
    protocol = _protocol()
    raw_ref = _raw_ref(100 + ordinal)
    payload = _campaign_payload(raw_ref=raw_ref)
    payload["product_family"] = product_family
    payload["unsupported_features"] = sorted(unsupported)
    if include_read_rule:
        claim_key, claim_digest = qualified_read_route_semantic_claim(
            provider_id="BYBIT",
            endpoint="/v5/account/wallet-balance",
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ACCOUNT.READ",
        )
        payload["route_semantics"][claim_key] = claim_digest
    if extra_route_semantics is not None:
        payload["route_semantics"].update(extra_route_semantics)
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


class _HostileTimezone(tzinfo):
    calls = 0

    def utcoffset(self, _dt):
        type(self).calls += 1
        raise AssertionError("hostile timezone callback executed")

    def dst(self, _dt):
        type(self).calls += 1
        raise AssertionError("hostile timezone callback executed")


class _HostileCandidateList(list):
    calls = 0

    def __iter__(self):
        type(self).calls += 1
        raise AssertionError("hostile candidate iterator executed")


class ProviderSelectionTests(unittest.TestCase):
    def authorities(self, directory: str, *, unsupported=()):
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
        evidence = ArtifactStore(evidence_root)
        harness = ExactQualificationProjectionHarness().start()
        self.addCleanup(harness.stop)
        qualifications = harness.registry(
            journal,
            evidence_store=evidence,
            evidence_root=evidence_root,
        )
        record, receipt, protocol = accepted_spot_q(unsupported=unsupported)
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
        return capabilities, qualifications, record

    def test_selection_rejects_executable_timezone_before_callback(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, _record = self.authorities(directory)
            _HostileTimezone.calls = 0
            hostile_at = datetime(
                2026,
                10,
                6,
                9,
                0,
                tzinfo=_HostileTimezone(),
            )

            with self.assertRaisesRegex(
                ProviderSelectionError,
                "exact stdlib timezone datetime",
            ):
                select_provider(
                    request(),
                    [candidate()],
                    at=hostile_at,
                    capability_registry=capabilities,
                    qualification_registry=qualifications,
                )

            self.assertEqual(_HostileTimezone.calls, 0)

    def test_selection_rejects_executable_candidate_container_before_iteration(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, _record = self.authorities(directory)
            _HostileCandidateList.calls = 0
            hostile_candidates = _HostileCandidateList([candidate()])

            with self.assertRaisesRegex(TypeError, "exact list or tuple"):
                select_provider(
                    request(),
                    hostile_candidates,
                    at=NOW,
                    capability_registry=capabilities,
                    qualification_registry=qualifications,
                )

            self.assertEqual(_HostileCandidateList.calls, 0)

    def test_selection_accepts_exact_tuple_candidate_population(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, _record = self.authorities(directory)
            result = select_provider(
                request(),
                (candidate(),),
                at=NOW,
                capability_registry=capabilities,
                qualification_registry=qualifications,
            )
            self.assertEqual(result.status, "SELECTED_UNAMBIGUOUS")

    def test_candidate_contains_no_caller_capability_or_qualification_authority(self):
        route = candidate()
        self.assertFalse(hasattr(route, "capability"))
        self.assertFalse(hasattr(route, "qualification"))
        with self.assertRaises(TypeError):
            ProviderCandidate(
                provider_id="BYBIT",
                product_family="SPOT",
                provider_environment="TESTNET",
                account_id="paper-account",
                entity_id="entity-1",
                entity_policy_id="LINEAR_ORDER_V1",
                adapter_code_sha=SOURCE_SHA,
                packaged_artifact_digest=PACKAGE_DIGEST,
                protocol_id="provider-route-v1",
                protocol_version="1.0.0",
                capability=object(),
            )

    def test_exact_current_c_and_q_are_selected_at_one_journal_cut(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, record = self.authorities(directory)
            result = select_provider(
                request(),
                [candidate()],
                at=NOW,
                capability_registry=capabilities,
                qualification_registry=qualifications,
            )
            self.assertEqual(result.status, "SELECTED_UNAMBIGUOUS")
            self.assertIsNotNone(result.selected)
            self.assertEqual(result.selected.qualification_id, record.qualification_id)
            self.assertEqual(
                result.selected.decision_journal_sequence_cut,
                result.decision_journal_sequence_cut,
            )
            self.assertEqual(result.selected.capability.provider_environment, "TESTNET")
            self.assertTrue(result.selected.capability.admits(
                at=NOW,
                order_type="LIMIT",
                time_in_force="DAY",
                permission_scope="ORDER.WRITE",
            ))

    def test_selected_route_constructor_is_sealed(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, _record = self.authorities(directory)
            result = select_provider(
                request(),
                [candidate()],
                at=NOW,
                capability_registry=capabilities,
                qualification_registry=qualifications,
            )
            route = result.selected
            self.assertIsNotNone(route)
            with self.assertRaisesRegex(ProviderSelectionError, "canonical provider selection"):
                SelectedProviderRoute(
                    candidate=route.candidate,
                    capability=route.capability,
                    qualification=route.qualification,
                    decision_journal_sequence_cut=route.decision_journal_sequence_cut,
                )

    def test_selected_route_rejects_post_selection_candidate_mutation(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, _record = self.authorities(directory)
            result = select_provider(
                request(),
                [candidate()],
                at=NOW,
                capability_registry=capabilities,
                qualification_registry=qualifications,
            )
            route = result.selected
            self.assertIsNotNone(route)
            selected_candidate = route.candidate
            original_account_id = selected_candidate.account_id

            object.__setattr__(
                selected_candidate,
                "account_id",
                "retargeted-account",
            )
            try:
                with self.assertRaisesRegex(
                    (ProviderSelectionError, PermissionError),
                    "selected provider route authority changed",
                ):
                    _ = route.candidate
            finally:
                object.__setattr__(
                    selected_candidate,
                    "account_id",
                    original_account_id,
                )

            self.assertIs(route.candidate, selected_candidate)

    def test_testnet_authority_does_not_admit_demo_route(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, _record = self.authorities(directory)
            result = select_provider(
                request(),
                [candidate(provider_environment="DEMO")],
                at=NOW,
                capability_registry=capabilities,
                qualification_registry=qualifications,
            )
            self.assertEqual(result.status, "NO_ELIGIBLE_PROVIDER")
            self.assertIn("QUALIFICATION_NOT_CURRENT", result.decisions[0].reasons)
            self.assertIn("CAPABILITY_NOT_CURRENT_VERIFIED", result.decisions[0].reasons)

    def test_exact_build_digest_is_part_of_current_q_scope(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, _record = self.authorities(directory)
            result = select_provider(
                request(),
                [candidate(package_digest="sha256:" + "9" * 64)],
                at=NOW,
                capability_registry=capabilities,
                qualification_registry=qualifications,
            )
            self.assertEqual(result.status, "NO_ELIGIBLE_PROVIDER")
            self.assertIn("QUALIFICATION_NOT_CURRENT", result.decisions[0].reasons)

    def test_explicit_unsupported_feature_blocks_route(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, _record = self.authorities(
                directory,
                unsupported=("ORDER_TYPE:LIMIT",),
            )
            result = select_provider(
                request(),
                [candidate()],
                at=NOW,
                capability_registry=capabilities,
                qualification_registry=qualifications,
            )
            self.assertEqual(result.status, "NO_ELIGIBLE_PROVIDER")
            self.assertIn(
                "QUALIFICATION_EXPLICITLY_UNSUPPORTED",
                result.decisions[0].reasons,
            )

    def test_different_journal_store_instances_are_not_one_decision_authority(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, _record = self.authorities(directory)
            other = DurableProviderQualificationRegistry(
                JournalStore(Path(directory) / "journal.sqlite3"),
                evidence_store=qualifications.evidence_store,
                evidence_root=qualifications.evidence_root,
            )
            with self.assertRaisesRegex(ProviderSelectionError, "share one JournalStore"):
                select_provider(
                    request(),
                    [candidate()],
                    at=NOW,
                    capability_registry=capabilities,
                    qualification_registry=other,
                )

    def test_projection_subclass_is_not_production_q_authority(self):
        with TemporaryDirectory() as directory:
            capabilities, qualifications, _record = self.authorities(directory)
            projection_subclass = _ProjectionOnlyRegistry(
                capabilities.store,
                evidence_store=qualifications.evidence_store,
                evidence_root=qualifications.evidence_root,
            )
            with self.assertRaisesRegex(
                TypeError,
                "qualification_registry must be exact DurableProviderQualificationRegistry",
            ):
                select_provider(
                    request(),
                    [candidate()],
                    at=NOW,
                    capability_registry=capabilities,
                    qualification_registry=projection_subclass,
                )

    def test_adapter_sha_is_exact_lowercase_40_hex(self):
        with self.assertRaisesRegex(ProviderSelectionError, "40-character"):
            ProviderCandidate(
                provider_id="BYBIT",
                product_family="SPOT",
                provider_environment="TESTNET",
                account_id="paper-account",
                entity_id="entity-1",
                entity_policy_id="LINEAR_ORDER_V1",
                adapter_code_sha="A" * 40,
                packaged_artifact_digest=PACKAGE_DIGEST,
                protocol_id="provider-route-v1",
                protocol_version="1.0.0",
            )


if __name__ == "__main__":
    unittest.main()
