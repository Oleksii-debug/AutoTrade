from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.durable_provider_qualification import (
    DurableProviderQualificationRegistry,
    ProviderQualificationAuthorityAmbiguous,
    ProviderQualificationCurrentUnavailable,
    _event_id,
    _receipt_payload,
)
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json, payload_digest
from mvp.autotrade_mvp.provider_core import REQUIRED_QUALIFICATION_CASES
from mvp.autotrade_mvp.provider_qualification_authority import (
    AcceptedProviderQualification,
    ProviderQualificationError,
    ProviderQualificationProtocol,
    ProviderQualificationUnavailable,
    _derive_accepted_provider_qualification,
    parse_provider_qualification_campaign,
    source_provider_qualification_protocol,
)
from mvp.autotrade_mvp.provider_qualification_current_scope import (
    ProviderQualificationCurrentScope,
)
from mvp.autotrade_mvp.qualification_attestation import (
    AcceptedQualificationAttestation,
    EvidenceArtifactRef,
    QualificationAttestation,
    SignedQualificationAttestation,
)


SOURCE_SHA = "a" * 40
PACKAGE_DIGEST = "sha256:" + "1" * 64
POLICY_ID = "sha256:" + "2" * 64
ROOT_ID = "sha256:" + "3" * 64
RAW_DIGEST = "sha256:" + "4" * 64
CAMPAIGN_KIND = "PROVIDER_QUALIFICATION_CAMPAIGN"


def _artifact_id(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def _protocol() -> ProviderQualificationProtocol:
    return ProviderQualificationProtocol(
        key="TEST_PROVIDER_V1",
        domain="PROVIDER",
        gate="ROUTE_QUALIFICATION",
        package_id="AUTOTRADE",
        protocol_id="provider-route-v1",
        protocol_version="1.0.0",
        requirement_id="provider-route-required",
        campaign_evidence_kind=CAMPAIGN_KIND,
    )


def _raw_ref(number: int = 100) -> EvidenceArtifactRef:
    return EvidenceArtifactRef(
        artifact_id=_artifact_id(number),
        sha256=RAW_DIGEST,
        media_type="application/json",
        evidence_kind="PROVIDER_RAW_EVIDENCE",
        source_sha=SOURCE_SHA,
    )


def _campaign_payload(
    *,
    campaign_version: int = 1,
    completed_at: str = "2026-10-04T05:00:00Z",
    valid_until: str = "2026-10-05T05:00:00Z",
    supersedes: str | None = None,
    route_parser: str = "BYBIT_ORDER_V5_JSON_V1",
    raw_ref: EvidenceArtifactRef | None = None,
) -> dict[str, object]:
    raw_ref = raw_ref or _raw_ref()
    required = sorted(REQUIRED_QUALIFICATION_CASES)
    return {
        "adapter_source_git_sha": SOURCE_SHA,
        "campaign_id": "bybit-linear-order",
        "campaign_version": campaign_version,
        "completed_at": completed_at,
        "documentation_revisions": ["BYBIT_V5_2026_09", "PRODUCT_RULES_2026_10"],
        "entity_policy_id": "LINEAR_ORDER_V1",
        "failed_cases": [],
        "packaged_artifact_digest": PACKAGE_DIGEST,
        "passed_cases": required,
        "product_family": "LINEAR_PERPETUAL",
        "protocol_id": "provider-route-v1",
        "protocol_version": "1.0.0",
        "provider_environment": "TESTNET",
        "provider_id": "BYBIT",
        "raw_evidence_refs": [raw_ref.canonical()],
        "required_cases": required,
        "route_semantics": {
            "ENDPOINT_FAMILY": "BYBIT_V5_ORDER",
            "PARSER_IDENTITY": route_parser,
        },
        "runtime_environment": "PAPER",
        "schema_version": "1.0.0",
        "supersedes_qualification_id": supersedes,
        "unsupported_features": ["NATIVE_TRAILING_STOP"],
        "valid_until": valid_until,
    }


def _issued(
    *,
    ordinal: int,
    campaign_version: int = 1,
    completed_at: str = "2026-10-04T05:00:00Z",
    signed_at: str = "2026-10-04T05:01:00Z",
    valid_until: str = "2026-10-05T05:00:00Z",
    supersedes: str | None = None,
    route_parser: str = "BYBIT_ORDER_V5_JSON_V1",
    release_artifact_id: str | None = None,
) -> tuple[
    AcceptedProviderQualification,
    SignedQualificationAttestation,
    ProviderQualificationProtocol,
]:
    protocol = _protocol()
    raw_ref = _raw_ref(100 + ordinal)
    campaign_payload = _campaign_payload(
        campaign_version=campaign_version,
        completed_at=completed_at,
        valid_until=valid_until,
        supersedes=supersedes,
        route_parser=route_parser,
        raw_ref=raw_ref,
    )
    campaign_raw = canonical_json(campaign_payload).encode("utf-8")
    campaign_ref = EvidenceArtifactRef(
        artifact_id=_artifact_id(ordinal),
        sha256="sha256:" + sha256(campaign_raw).hexdigest(),
        media_type="application/json",
        evidence_kind=CAMPAIGN_KIND,
        source_sha=SOURCE_SHA,
    )
    campaign = parse_provider_qualification_campaign(campaign_raw)
    release_digest = PACKAGE_DIGEST if release_artifact_id is not None else None
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
        completed_at=completed_at,
        signed_at=signed_at,
        result="PASS",
        release_artifact_id=release_artifact_id,
        release_artifact_sha256=release_digest,
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
        release_artifact_id=release_artifact_id,
        release_artifact_sha256=release_digest,
    )
    record = _derive_accepted_provider_qualification(
        protocol=protocol,
        campaign=campaign,
        campaign_artifact_ref=campaign_ref,
        accepted_attestation=accepted,
        receipt=receipt,
    )
    return record, receipt, protocol


def _current_scope(record: AcceptedProviderQualification) -> ProviderQualificationCurrentScope:
    return ProviderQualificationCurrentScope(
        provider_scope=record.scope.provider_scope,
        product_family=record.scope.product_family,
        adapter_source_git_sha=record.scope.adapter_source_git_sha,
        packaged_artifact_digest=record.scope.packaged_artifact_digest,
        protocol_id=record.scope.protocol_id,
        protocol_version=record.scope.protocol_version,
    )


class _ProjectionOnlyRegistry(DurableProviderQualificationRegistry):
    """Unit-only projection harness; never evidence of provider qualification."""

    def _authenticate_record(self, *, protocol_key, record, receipt):
        return record


class ProviderQualificationAuthorityTests(unittest.TestCase):
    def test_source_protocol_is_fail_closed_until_canonical_policy_exists(self):
        with self.assertRaises(ProviderQualificationUnavailable):
            source_provider_qualification_protocol("TEST_PROVIDER_V1")

    def test_campaign_requires_exact_source_owned_case_universe(self):
        payload = _campaign_payload()
        payload["required_cases"] = sorted(REQUIRED_QUALIFICATION_CASES)[:-1]
        payload["passed_cases"] = list(payload["required_cases"])
        raw = canonical_json(payload).encode("utf-8")
        with self.assertRaisesRegex(ProviderQualificationError, "source-owned"):
            parse_provider_qualification_campaign(raw)

    def test_campaign_rejects_unknown_fields_and_noncanonical_bytes(self):
        payload = _campaign_payload()
        payload["caller_override"] = True
        with self.assertRaisesRegex(ProviderQualificationError, "fields mismatch"):
            parse_provider_qualification_campaign(canonical_json(payload).encode("utf-8"))
        canonical = canonical_json(_campaign_payload()).encode("utf-8")
        with self.assertRaisesRegex(ProviderQualificationError, "not canonical JSON"):
            parse_provider_qualification_campaign(canonical + b"\n")

    def test_accepted_q_constructor_is_sealed(self):
        record, _receipt, _protocol_value = _issued(ordinal=1)
        with self.assertRaisesRegex(
            ProviderQualificationError,
            "must come from canonical provider qualification authority",
        ):
            AcceptedProviderQualification(
                identity=record.identity,
                scope=record.scope,
                required_cases=record.required_cases,
                unsupported_features=record.unsupported_features,
                route_semantics_json=record.route_semantics_json,
                documentation_revisions=record.documentation_revisions,
                completed_at=record.completed_at,
                valid_until=record.valid_until,
                campaign_artifact_ref=record.campaign_artifact_ref,
                raw_evidence_refs=record.raw_evidence_refs,
                supersedes_qualification_id=record.supersedes_qualification_id,
                attestation_id=record.attestation_id,
                attestation_digest=record.attestation_digest,
                policy_id=record.policy_id,
                policy_version=record.policy_version,
                trust_root_id=record.trust_root_id,
                producer_id=record.producer_id,
                verifier_id=record.verifier_id,
                signed_at=record.signed_at,
                release_artifact_id=record.release_artifact_id,
            )

    def test_q_id_binds_protocol_freshness_lineage_and_release_identity(self):
        record, _receipt, _protocol_value = _issued(
            ordinal=2,
            release_artifact_id=_artifact_id(900),
        )
        changes = {
            "protocol_version": "2.0.0",
            "chronology_digest": "sha256:" + "5" * 64,
            "lineage_digest": "sha256:" + "6" * 64,
            "packaged_artifact_id": _artifact_id(901),
            "acceptance_metadata_digest": "sha256:" + "7" * 64,
        }
        for name, value in changes.items():
            with self.subTest(field=name):
                changed = replace(record.identity, **{name: value})
                self.assertNotEqual(changed.content_digest, record.qualification_id)

    def test_new_campaign_generation_stays_in_same_current_scope(self):
        q1, _r1, _p1 = _issued(ordinal=3, campaign_version=1)
        q2, _r2, _p2 = _issued(
            ordinal=4,
            campaign_version=2,
            route_parser="BYBIT_ORDER_V5_JSON_V2",
        )
        self.assertNotEqual(q1.scope, q2.scope)
        self.assertNotEqual(q1.qualification_id, q2.qualification_id)
        self.assertEqual(_current_scope(q1), _current_scope(q2))

    def test_generic_journal_acceptance_cannot_forge_current_q(self):
        record, receipt, protocol = _issued(ordinal=5)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.db")
            evidence_root = root / "evidence"
            evidence_store = ArtifactStore(evidence_root)
            scope = _current_scope(record)
            payload = {
                "schema_version": "1.0.0",
                "protocol_key": protocol.key,
                "record": record.payload(),
                "signed_receipt": _receipt_payload(receipt),
            }
            store.append_event(
                {
                    "event_id": _event_id("accepted", payload),
                    "event_type": "ProviderQualificationAccepted.v1",
                    "aggregate_type": "provider_qualification",
                    "aggregate_id": scope.content_digest,
                    "aggregate_version": "1",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": record.signed_at,
                }
            )
            registry = DurableProviderQualificationRegistry(
                store,
                evidence_store=evidence_store,
                evidence_root=evidence_root,
            )
            with self.assertRaises(ProviderQualificationUnavailable):
                registry.current(
                    scope=scope,
                    at=datetime(2026, 10, 4, 6, tzinfo=timezone.utc),
                )

    def test_projection_overlap_is_ambiguous_not_latest_wins(self):
        q1, r1, p1 = _issued(ordinal=6, campaign_version=1)
        q2, r2, p2 = _issued(
            ordinal=7,
            campaign_version=2,
            route_parser="BYBIT_ORDER_V5_JSON_V2",
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry = _ProjectionOnlyRegistry(
                JournalStore(root / "journal.db"),
                evidence_store=ArtifactStore(root / "evidence"),
                evidence_root=root / "evidence",
            )
            registry._append_accepted(protocol_key=p1.key, record=q1, receipt=r1)
            registry._append_accepted(protocol_key=p2.key, record=q2, receipt=r2)
            with self.assertRaises(ProviderQualificationAuthorityAmbiguous):
                registry.current(
                    scope=_current_scope(q1),
                    at=datetime(2026, 10, 4, 6, tzinfo=timezone.utc),
                )

    def test_q_is_not_current_before_signature_or_at_expiry(self):
        record, receipt, protocol = _issued(ordinal=10)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry = _ProjectionOnlyRegistry(
                JournalStore(root / "journal.db"),
                evidence_store=ArtifactStore(root / "evidence"),
                evidence_root=root / "evidence",
            )
            registry._append_accepted(
                protocol_key=protocol.key,
                record=record,
                receipt=receipt,
            )
            with self.assertRaises(ProviderQualificationCurrentUnavailable):
                registry.current(
                    scope=_current_scope(record),
                    at=datetime(2026, 10, 4, 5, 0, 30, tzinfo=timezone.utc),
                )
            with self.assertRaises(ProviderQualificationCurrentUnavailable):
                registry.current(
                    scope=_current_scope(record),
                    at=datetime(2026, 10, 5, 5, tzinfo=timezone.utc),
                )

    def test_global_cut_includes_unrelated_journal_events(self):
        record, receipt, protocol = _issued(ordinal=11)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.db")
            registry = _ProjectionOnlyRegistry(
                store,
                evidence_store=ArtifactStore(root / "evidence"),
                evidence_root=root / "evidence",
            )
            registry._append_accepted(
                protocol_key=protocol.key,
                record=record,
                receipt=receipt,
            )
            unrelated_payload = {"value": "other-authority-change"}
            store.append_event(
                {
                    "event_id": "other-authority-event-1",
                    "event_type": "OtherAuthorityChanged.v1",
                    "aggregate_type": "other_authority",
                    "aggregate_id": "other-authority",
                    "aggregate_version": "1",
                    "payload": unrelated_payload,
                    "payload_hash": payload_digest(unrelated_payload),
                    "committed_at": "2026-10-04T05:02:00Z",
                }
            )
            current = registry.current(
                scope=_current_scope(record),
                at=datetime(2026, 10, 4, 6, tzinfo=timezone.utc),
            )
            self.assertEqual(current.qualification_id, record.qualification_id)
            self.assertEqual(current.journal_sequence_cut, 2)

    def test_future_journal_cut_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry = _ProjectionOnlyRegistry(
                JournalStore(root / "journal.db"),
                evidence_store=ArtifactStore(root / "evidence"),
                evidence_root=root / "evidence",
            )
            with self.assertRaisesRegex(
                ProviderQualificationError,
                "journal cut is in the future",
            ):
                registry.history_cut(journal_sequence_cut=1)

    def test_latest_cut_does_not_retroactively_apply_future_supersession(self):
        q1, r1, p1 = _issued(
            ordinal=12,
            campaign_version=1,
            completed_at="2026-10-04T05:00:00Z",
            signed_at="2026-10-04T05:01:00Z",
            valid_until="2026-10-05T05:00:00Z",
        )
        q2, r2, p2 = _issued(
            ordinal=13,
            campaign_version=2,
            completed_at="2026-10-04T06:00:00Z",
            signed_at="2026-10-04T06:01:00Z",
            valid_until="2026-10-05T06:00:00Z",
            supersedes=q1.qualification_id,
            route_parser="BYBIT_ORDER_V5_JSON_V2",
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry = _ProjectionOnlyRegistry(
                JournalStore(root / "journal.db"),
                evidence_store=ArtifactStore(root / "evidence"),
                evidence_root=root / "evidence",
            )
            registry._append_accepted(protocol_key=p1.key, record=q1, receipt=r1)
            registry._append_accepted(protocol_key=p2.key, record=q2, receipt=r2)
            registry._append_supersession(
                old_id=q1.qualification_id,
                new_id=q2.qualification_id,
            )

            before_successor_signature = registry.current(
                scope=_current_scope(q1),
                at=datetime(2026, 10, 4, 5, 30, tzinfo=timezone.utc),
            )
            after_successor_signature = registry.current(
                scope=_current_scope(q1),
                at=datetime(2026, 10, 4, 6, 30, tzinfo=timezone.utc),
            )

            self.assertEqual(
                before_successor_signature.qualification_id,
                q1.qualification_id,
            )
            self.assertEqual(
                after_successor_signature.qualification_id,
                q2.qualification_id,
            )
            self.assertEqual(
                before_successor_signature.journal_sequence_cut,
                after_successor_signature.journal_sequence_cut,
            )

    def test_signed_supersession_changes_current_q_and_preserves_historical_cut(self):
        q1, r1, p1 = _issued(ordinal=8, campaign_version=1)
        q2, r2, p2 = _issued(
            ordinal=9,
            campaign_version=2,
            route_parser="BYBIT_ORDER_V5_JSON_V2",
            supersedes=q1.qualification_id,
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry = _ProjectionOnlyRegistry(
                JournalStore(root / "journal.db"),
                evidence_store=ArtifactStore(root / "evidence"),
                evidence_root=root / "evidence",
            )
            registry._append_accepted(protocol_key=p1.key, record=q1, receipt=r1)
            before = registry.current(
                scope=_current_scope(q1),
                at=datetime(2026, 10, 4, 6, tzinfo=timezone.utc),
            )
            registry._append_accepted(protocol_key=p2.key, record=q2, receipt=r2)
            registry._append_supersession(
                old_id=q1.qualification_id,
                new_id=q2.qualification_id,
            )
            now = registry.current(
                scope=_current_scope(q1),
                at=datetime(2026, 10, 4, 6, tzinfo=timezone.utc),
            )
            historical = registry.current(
                scope=_current_scope(q1),
                at=datetime(2026, 10, 4, 6, tzinfo=timezone.utc),
                journal_sequence_cut=before.journal_sequence_cut,
            )
            self.assertEqual(before.qualification_id, q1.qualification_id)
            self.assertEqual(historical.qualification_id, q1.qualification_id)
            self.assertEqual(now.qualification_id, q2.qualification_id)
            self.assertGreater(now.journal_sequence_cut, before.journal_sequence_cut)
            with self.assertRaises(ProviderQualificationCurrentUnavailable):
                registry.require_exact_current(
                    scope=_current_scope(q1),
                    at=datetime(2026, 10, 4, 6, tzinfo=timezone.utc),
                    expected_qualification_id=q1.qualification_id,
                )


if __name__ == "__main__":
    unittest.main()
