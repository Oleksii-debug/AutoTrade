from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.autotrade_mvp.provider_core import Surface
from mvp.autotrade_mvp.provider_qualification_authority import (
    _derive_accepted_provider_qualification,
    parse_provider_qualification_campaign,
)
from mvp.autotrade_mvp.provider_route_dispatch import (
    ProviderRouteDispatchError,
    dispatch_selected_provider_route,
)
from mvp.autotrade_mvp.provider_route_reads import (
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
from mvp.tests.test_provider_selection import (
    NOW,
    accepted_spot_q,
    candidate,
    request as route_request,
)


def successor_spot_q(*, old_qualification_id: str, ordinal: int = 41):
    protocol = _protocol()
    raw_ref = _raw_ref(100 + ordinal)
    payload = _campaign_payload(
        campaign_version=2,
        completed_at="2026-10-04T05:02:00Z",
        valid_until="2026-10-05T05:00:00Z",
        supersedes=old_qualification_id,
        route_parser="BYBIT_ORDER_V5_JSON_V2",
        raw_ref=raw_ref,
    )
    payload["product_family"] = "SPOT"
    claim_key, claim_digest = qualified_read_route_semantic_claim(
        provider_id="BYBIT",
        endpoint="/v5/account/wallet-balance",
        surface=Surface.AUTHENTICATED_READ,
        permission_scope="ACCOUNT.READ",
    )
    payload["route_semantics"][claim_key] = claim_digest
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
        started_at="2026-10-04T05:01:30Z",
        completed_at="2026-10-04T05:02:00Z",
        signed_at="2026-10-04T05:03:00Z",
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


class ProviderRouteDispatchTests(unittest.TestCase):
    def setup_route(self, directory: str):
        journal = JournalStore((Path(directory) / "journal.sqlite3").resolve(strict=False))
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
        q1, receipt1, protocol1 = accepted_spot_q(ordinal=40)
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
        dispatcher = GuardedDispatcher(
            journal,
            environment="PAPER",
            account_id="paper-account",
            owner_token="route-owner",
        )
        return (
            journal,
            capabilities,
            qualifications,
            selection.selected,
            dispatcher,
            q1,
            harness,
        )

    def dispatch(
        self,
        dispatcher,
        route,
        capabilities,
        qualifications,
        transport,
        **overrides,
    ):
        values = dict(
            capability_registry=capabilities,
            attempt_id="attempt-route-1",
            intent_id="intent-route-1",
            intent_hash="intent-hash-1",
            request={"symbol": "BTCUSDT"},
            now="2026-10-04T05:05:00Z",
            authority_check=lambda _intent_hash, _at: (True, "product_authority_allowed"),
            transport_send=transport,
            sender_check=lambda _owner, _epoch: None,
        )
        values.update(overrides)
        return dispatch_selected_provider_route(
            dispatcher,
            route,
            qualifications,
            **values,
        )

    def test_success_binds_q_and_c_into_durable_submission_scope(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, dispatcher, q1, _harness = self.setup_route(directory)
            wire = []

            def transport(_client_id, _request, final_guard):
                final_guard()
                wire.append("sent")
                return {"ok": True}

            outcome = self.dispatch(
                dispatcher, route, capabilities, qualifications, transport
            )
            self.assertEqual(outcome.status, "SENT")
            self.assertEqual(wire, ["sent"])
            events = journal.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("attempt-route-1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )
            scope = events[0]["payload"]["submission_scope"]
            self.assertEqual(scope["provider_route_qualification_id"], q1.qualification_id)
            self.assertEqual(
                scope["provider_route_capability_snapshot_id"],
                route.capability_snapshot_id,
            )
            self.assertEqual(
                scope["provider_route_decision_journal_sequence_cut"],
                route.decision_journal_sequence_cut,
            )

    def test_q1_superseded_by_q2_between_checks_produces_zero_wire(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, dispatcher, q1, harness = self.setup_route(directory)
            wire = []

            def transport(_client_id, _request, final_guard):
                q2, receipt2, protocol2 = successor_spot_q(
                    old_qualification_id=q1.qualification_id,
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
                final_guard()
                wire.append("sent")
                return {"ok": True}

            outcome = self.dispatch(
                dispatcher, route, capabilities, qualifications, transport
            )
            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(outcome.reason, "provider_qualification_not_exact_current")
            self.assertEqual(wire, [])
            events = journal.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("attempt-route-1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertNotIn("SubmissionSending", [event["event_type"] for event in events])

    def test_c1_superseded_by_c2_between_checks_produces_zero_wire(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, dispatcher, _q1, _harness = self.setup_route(directory)
            wire = []

            def transport(_client_id, _request, final_guard):
                capabilities.add(
                    verified(
                        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                        NOW + timedelta(seconds=30),
                        provider_id="BYBIT",
                        provider_environment="TESTNET",
                    )
                )
                final_guard()
                wire.append("sent")
                return {"ok": True}

            outcome = self.dispatch(
                dispatcher,
                route,
                capabilities,
                qualifications,
                transport,
                final_barrier_clock=lambda: "2026-10-04T05:06:00Z",
            )
            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(outcome.reason, "provider_capability_not_exact_current")
            self.assertEqual(wire, [])
            events = journal.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("attempt-route-1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertNotIn("SubmissionSending", [event["event_type"] for event in events])

    def test_q_expiry_at_final_barrier_produces_zero_wire(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, dispatcher, _q1, _harness = self.setup_route(directory)
            wire = []

            def transport(_client_id, _request, final_guard):
                final_guard()
                wire.append("sent")
                return {"ok": True}

            outcome = self.dispatch(
                dispatcher,
                route,
                capabilities,
                qualifications,
                transport,
                final_barrier_clock=lambda: "2026-10-05T05:00:00Z",
            )
            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(outcome.reason, "provider_capability_not_exact_current")
            self.assertEqual(wire, [])
            events = journal.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("attempt-route-1"),
            )
            self.assertEqual(events[-1]["event_type"], "SubmissionBlocked")

    def test_caller_cannot_override_route_authority_in_submission_scope(self):
        with TemporaryDirectory() as directory:
            _journal, capabilities, qualifications, route, dispatcher, _q1, _harness = self.setup_route(directory)
            with self.assertRaisesRegex(ProviderRouteDispatchError, "override"):
                self.dispatch(
                    dispatcher,
                    route,
                    capabilities,
                    qualifications,
                    lambda *_args: {"ok": True},
                    submission_scope={
                        "provider_route_qualification_id": "caller-selected-q"
                    },
                )

    def test_dispatch_and_registries_must_share_exact_store_instance(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, dispatcher, _q1, harness = self.setup_route(directory)
            other_q = harness.registry(
                JournalStore(Path(directory) / "journal.sqlite3"),
                evidence_store=qualifications.evidence_store,
                evidence_root=qualifications.evidence_root,
            )
            with self.assertRaisesRegex(ProviderRouteDispatchError, "share one JournalStore"):
                self.dispatch(
                    dispatcher,
                    route,
                    capabilities,
                    other_q,
                    lambda *_args: {"ok": True},
                )
            other_c = DurableCapabilityRegistry(
                JournalStore(Path(directory) / "journal.sqlite3")
            )
            with self.assertRaisesRegex(ProviderRouteDispatchError, "share one JournalStore"):
                self.dispatch(
                    dispatcher,
                    route,
                    other_c,
                    qualifications,
                    lambda *_args: {"ok": True},
                    attempt_id="attempt-route-2",
                )


if __name__ == "__main__":
    unittest.main()
