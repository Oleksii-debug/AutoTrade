from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_runtime.artifacts.store import ArtifactStore
import mvp.autotrade_mvp.provider_qualification_authority as authority_module
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json, payload_digest
from mvp.autotrade_mvp.provider_core import REQUIRED_QUALIFICATION_CASES
from mvp.autotrade_mvp.provider_qualification_authority import (
    DurableProviderQualificationRegistry,
    ProviderQualificationAuthorityError,
    ProviderQualificationCampaign,
    ProviderQualificationCurrentReader,
    ProviderQualificationCurrentnessUnavailable,
    issue_accepted_provider_qualification,
)
from mvp.autotrade_mvp.qualification_attestation import (
    AcceptedQualificationAttestation,
    EvidenceArtifactRef,
)


SOURCE_SHA = "1" * 40
ARTIFACT_A = "00000000-0000-0000-0000-000000000001"
ARTIFACT_B = "00000000-0000-0000-0000-000000000002"
ARTIFACT_C = "00000000-0000-0000-0000-000000000003"
ATTEST_A = "00000000-0000-0000-0000-000000000101"
ATTEST_B = "00000000-0000-0000-0000-000000000102"
ATTEST_C = "00000000-0000-0000-0000-000000000103"


def _campaign(
    *,
    provider_environment: str = "TESTNET",
    artifact_id: str = ARTIFACT_A,
    completed_at: str = "2026-01-01T00:01:00Z",
    reconciliation: bool = True,
    account_class: str = "STANDARD",
):
    return {
        "schema_version": "1.0.0",
        "provider_id": "BYBIT",
        "product_family": "SPOT",
        "environment": "PAPER",
        "provider_environment": provider_environment,
        "route_policy_id": "bybit-v5-private",
        "entity_policy_id": "bybit-account-v1",
        "network_policy_id": "direct-tls-v1",
        "account_class": account_class,
        "adapter_source_sha": SOURCE_SHA,
        "campaign_protocol_id": "provider-qualification",
        "campaign_protocol_version": "1.0.0",
        "required_case_set_version": "provider-qualification:v1",
        "case_results": [
            {"case_id": case_id, "result": "PASS"}
            for case_id in sorted(REQUIRED_QUALIFICATION_CASES)
        ],
        "unsupported_features": ["withdrawals"],
        "documentation_revisions": ["bybit-v5:2026-01"],
        "campaign_artifact_id": artifact_id,
        "started_at": "2026-01-01T00:00:10Z",
        "completed_at": completed_at,
        "valid_until": "2027-01-01T00:00:00Z",
        "release_artifact_id": None,
        "release_artifact_sha256": None,
        "live_authorized": False,
        "reconciliation_semantics": (
            {
                "schema_version": "1.0.0",
                "generation_scheme": "SERIALIZED_ACQUISITION_GENERATION",
                "surfaces": [
                    {
                        "surface_id": "ORDERS",
                        "endpoint": "/v5/order/realtime",
                        "category": "ORDERS",
                        "exclusion_authority": True,
                        "pagination_rule_id": "bybit-v5-cursor",
                        "consistency_horizon_ms": 5000,
                        "cache_policy_id": "no-cache",
                    }
                ],
            }
            if reconciliation
            else None
        ),
    }


def _campaign_digest(campaign: dict[str, object]) -> str:
    return "sha256:" + sha256(
        canonical_json(campaign).encode("utf-8")
    ).hexdigest()


def _accepted(
    campaign: dict[str, object],
    *,
    attestation_id: str = ATTEST_A,
    signed_at: str = "2026-01-01T00:03:00Z",
) -> AcceptedQualificationAttestation:
    ref = EvidenceArtifactRef(
        artifact_id=campaign["campaign_artifact_id"],
        sha256=_campaign_digest(campaign),
        media_type="application/json",
        evidence_kind="PROVIDER_QUALIFICATION_CAMPAIGN",
        source_sha=SOURCE_SHA,
    )
    attestation_json = canonical_json(
        {
            "attestation_id": attestation_id,
            "source_sha": SOURCE_SHA,
            "domain": "PROVIDER",
            "gate": "PROVIDER_QUALIFICATION",
            "package_id": "AUTOTRADE_PROVIDER_QUALIFICATION",
            "protocol_id": campaign["campaign_protocol_id"],
            "protocol_version": campaign["campaign_protocol_version"],
            "result": "PASS",
        }
    )
    return AcceptedQualificationAttestation(
        attestation_id=attestation_id,
        attestation_digest="sha256:" + sha256(attestation_json.encode()).hexdigest(),
        policy_id="sha256:" + "b" * 64,
        policy_version="1.0.0",
        trust_root_id="sha256:" + "c" * 64,
        result="PASS",
        source_sha=SOURCE_SHA,
        domain="PROVIDER",
        gate="PROVIDER_QUALIFICATION",
        package_id="AUTOTRADE_PROVIDER_QUALIFICATION",
        protocol_id=campaign["campaign_protocol_id"],
        protocol_version=campaign["campaign_protocol_version"],
        requirement_id="provider-qualification:v1",
        requirement_ids=("provider-qualification:v1",),
        evidence_refs=(ref,),
        producer_id="provider-campaign",
        verifier_id="autotrade-verifier",
        runner_id="ci-runner",
        harness_version="1.0.0",
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:02:00Z",
        signed_at=signed_at,
        unresolved_limits=(),
        schema_version="1.0.0",
        verification_method="RSA_PKCS1V15_SHA256",
        release_artifact_id=None,
        release_artifact_sha256=None,
        attestation_json=attestation_json,
        signature_b64="AA==",
    )


def _q(**kwargs):
    campaign = _campaign(**kwargs)
    return issue_accepted_provider_qualification(
        accepted_attestation=_accepted(
            campaign,
            attestation_id=(
                ATTEST_A
                if campaign["campaign_artifact_id"] == ARTIFACT_A
                else ATTEST_B
                if campaign["campaign_artifact_id"] == ARTIFACT_B
                else ATTEST_C
            ),
        ),
        campaign_payload=campaign,
    )


class ProviderQualificationCampaignTests(unittest.TestCase):
    def test_provider_environment_changes_scope_and_qualification_identity(self):
        testnet = _q(provider_environment="TESTNET")
        demo_campaign = _campaign(provider_environment="DEMO")
        demo = issue_accepted_provider_qualification(
            accepted_attestation=_accepted(demo_campaign),
            campaign_payload=demo_campaign,
        )
        self.assertNotEqual(testnet.aggregate_id, demo.aggregate_id)
        self.assertNotEqual(testnet.qualification_id, demo.qualification_id)

    def test_unknown_or_missing_campaign_fields_fail_closed(self):
        value = _campaign()
        value["unexpected"] = "x"
        with self.assertRaises(ProviderQualificationAuthorityError):
            ProviderQualificationCampaign.from_mapping(
                value,
                campaign_sha256=_campaign_digest(value),
            )
        value = _campaign()
        del value["network_policy_id"]
        with self.assertRaises(ProviderQualificationAuthorityError):
            ProviderQualificationCampaign.from_mapping(
                value,
                campaign_sha256=_campaign_digest(value),
            )

    def test_duplicate_or_nonpassing_required_case_fails(self):
        value = _campaign()
        value["case_results"].append(
            {"case_id": sorted(REQUIRED_QUALIFICATION_CASES)[0], "result": "PASS"}
        )
        with self.assertRaises(ProviderQualificationAuthorityError):
            ProviderQualificationCampaign.from_mapping(
                value,
                campaign_sha256=_campaign_digest(value),
            )
        value = _campaign()
        value["case_results"][0]["result"] = "UNSUPPORTED"
        with self.assertRaisesRegex(
            ProviderQualificationAuthorityError, "must PASS"
        ):
            issue_accepted_provider_qualification(
                accepted_attestation=_accepted(value),
                campaign_payload=value,
            )

    def test_campaign_artifact_must_be_accepted_exactly(self):
        value = _campaign()
        accepted = _accepted(value)
        changed = deepcopy(value)
        changed["documentation_revisions"] = ["bybit-v5:2026-02"]
        with self.assertRaisesRegex(
            ProviderQualificationAuthorityError, "artifact digest"
        ):
            issue_accepted_provider_qualification(
                accepted_attestation=accepted,
                campaign_payload=changed,
            )

    def test_reconciliation_semantics_is_content_derived_not_boolean(self):
        qualified = _q()
        self.assertIsNotNone(qualified.reconciliation_semantics_id)
        diagnostic = _q(reconciliation=False)
        self.assertIsNone(diagnostic.reconciliation_semantics_id)


    def test_required_case_set_must_be_exact_and_complete(self):
        missing = _campaign()
        missing["case_results"].pop()
        with self.assertRaisesRegex(
            ProviderQualificationAuthorityError, "exact required case set"
        ):
            ProviderQualificationCampaign.from_mapping(
                missing,
                campaign_sha256=_campaign_digest(missing),
            )

        extra = _campaign()
        extra["case_results"].append(
            {"case_id": "invented-provider-case", "result": "PASS"}
        )
        with self.assertRaisesRegex(
            ProviderQualificationAuthorityError, "exact required case set"
        ):
            ProviderQualificationCampaign.from_mapping(
                extra,
                campaign_sha256=_campaign_digest(extra),
            )

    def test_account_class_is_part_of_durable_qualification_scope(self):
        standard = _q()
        unified_campaign = _campaign(
            account_class="UNIFIED",
            artifact_id=ARTIFACT_B,
        )
        unified = issue_accepted_provider_qualification(
            accepted_attestation=_accepted(
                unified_campaign,
                attestation_id=ATTEST_B,
            ),
            campaign_payload=unified_campaign,
        )
        self.assertNotEqual(standard.aggregate_id, unified.aggregate_id)

    def test_reconciliation_semantics_identity_is_bound_to_parent_q(self):
        testnet = _q()
        demo_campaign = _campaign(
            provider_environment="DEMO",
            artifact_id=ARTIFACT_B,
        )
        demo = issue_accepted_provider_qualification(
            accepted_attestation=_accepted(
                demo_campaign,
                attestation_id=ATTEST_B,
            ),
            campaign_payload=demo_campaign,
        )
        self.assertNotEqual(
            testnet.reconciliation_semantics_id,
            demo.reconciliation_semantics_id,
        )

    def test_live_campaign_is_categorically_unavailable(self):
        value = _campaign()
        value["environment"] = "LIVE"
        value["provider_environment"] = "MAINNET"
        with self.assertRaisesRegex(
            ProviderQualificationAuthorityError, "LIVE"
        ):
            ProviderQualificationCampaign.from_mapping(
                value,
                campaign_sha256=_campaign_digest(value),
            )

    def test_campaign_digest_is_external_to_canonical_campaign_bytes(self):
        value = _campaign()
        self.assertNotIn("campaign_sha256", value)
        qualification = issue_accepted_provider_qualification(
            accepted_attestation=_accepted(value),
            campaign_payload=value,
        )
        self.assertEqual(
            qualification.campaign.campaign_sha256,
            _campaign_digest(value),
        )
        self.assertNotIn(
            "campaign_sha256",
            qualification.campaign.canonical_payload(),
        )


class DurableProviderQualificationRegistryTests(unittest.TestCase):
    def _store(self, directory: str) -> JournalStore:
        return JournalStore(Path(directory) / "journal.db")

    def test_exact_store_type_only(self):
        class DerivedJournalStore(JournalStore):
            pass

        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(TypeError, "exact JournalStore"):
                DurableProviderQualificationRegistry(
                    DerivedJournalStore(Path(directory) / "journal.db")
                )

    def test_accept_restart_supersede_revoke_and_current_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            registry = DurableProviderQualificationRegistry(store)
            q1 = _q()
            self.assertTrue(registry.register(q1))
            self.assertFalse(registry.register(q1))
            restarted = DurableProviderQualificationRegistry(self._store(directory))
            self.assertEqual(
                restarted.historical(q1.qualification_id), q1
            )
            with self.assertRaises(ProviderQualificationCurrentnessUnavailable):
                restarted.require_current(q1.qualification_id)

            q2 = _q(
                artifact_id=ARTIFACT_B,
                completed_at="2026-01-01T00:01:10Z",
            )
            self.assertTrue(
                restarted.supersede(q1.qualification_id, q2)
            )
            self.assertFalse(
                restarted.supersede(q1.qualification_id, q2)
            )
            self.assertEqual(
                restarted.historical_current_for(q2), q2
            )
            self.assertTrue(
                restarted.revoke(
                    q2.qualification_id,
                    revoked_at="2026-01-02T00:00:00Z",
                    reason="campaign-revoked",
                )
            )
            self.assertFalse(
                restarted.revoke(
                    q2.qualification_id,
                    revoked_at="2026-01-02T00:00:00Z",
                    reason="campaign-revoked",
                )
            )
            self.assertIsNone(restarted.historical_current_for(q2))


    def test_accept_event_time_is_attestation_signature_not_campaign_completion(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            registry = DurableProviderQualificationRegistry(store)
            q1 = _q()
            registry.register(q1)
            events = store.load_events_by_aggregate_type(
                "provider_qualification"
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["committed_at"], q1.accepted_at)
            self.assertEqual(q1.accepted_at, "2026-01-01T00:03:00Z")

    def test_replay_rejects_noncanonical_accept_event_identity(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            qualification = _q()
            payload = {
                "transition": "ACCEPT",
                "qualification": qualification.record_payload(),
            }
            store.append_event(
                {
                    "event_id": "provider-qualification:caller-selected-event",
                    "event_type": "ProviderQualificationAccepted.v1",
                    "aggregate_type": "provider_qualification",
                    "aggregate_id": qualification.aggregate_id,
                    "aggregate_version": "1",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": qualification.accepted_at,
                }
            )
            restarted = DurableProviderQualificationRegistry(store)
            with self.assertRaisesRegex(
                ProviderQualificationAuthorityError,
                "event identity",
            ):
                restarted.historical(qualification.qualification_id)

    def test_replay_rejects_accept_committed_time_divergent_from_signed_authority(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            registry = DurableProviderQualificationRegistry(store)
            qualification = _q()
            payload = {
                "transition": "ACCEPT",
                "qualification": qualification.record_payload(),
            }
            store.append_event(
                {
                    "event_id": registry._event_id(
                        "ACCEPT\n" + qualification.qualification_id
                    ),
                    "event_type": "ProviderQualificationAccepted.v1",
                    "aggregate_type": "provider_qualification",
                    "aggregate_id": qualification.aggregate_id,
                    "aggregate_version": "1",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": "2026-01-01T00:03:01Z",
                }
            )
            restarted = DurableProviderQualificationRegistry(store)
            with self.assertRaisesRegex(
                ProviderQualificationAuthorityError,
                "chronology",
            ):
                restarted.historical(qualification.qualification_id)

    def test_supersession_and_revocation_cannot_backdate_accepted_authority(self):
        with TemporaryDirectory() as directory:
            registry = DurableProviderQualificationRegistry(
                self._store(directory)
            )
            q1 = _q()
            registry.register(q1)

            campaign = _campaign(
                artifact_id=ARTIFACT_B,
                completed_at="2026-01-01T00:01:10Z",
            )
            backdated_q2 = issue_accepted_provider_qualification(
                accepted_attestation=_accepted(
                    campaign,
                    attestation_id=ATTEST_B,
                    signed_at="2026-01-01T00:02:30Z",
                ),
                campaign_payload=campaign,
            )
            with self.assertRaisesRegex(
                ProviderQualificationAuthorityError, "backdates accepted authority"
            ):
                registry.supersede(q1.qualification_id, backdated_q2)

            with self.assertRaisesRegex(
                ProviderQualificationAuthorityError, "backdates accepted authority"
            ):
                registry.revoke(
                    q1.qualification_id,
                    revoked_at="2026-01-01T00:02:59Z",
                    reason="backdated-revoke",
                )

    def test_different_qualification_requires_explicit_supersession(self):
        with TemporaryDirectory() as directory:
            registry = DurableProviderQualificationRegistry(
                self._store(directory)
            )
            q1 = _q()
            q2 = _q(
                artifact_id=ARTIFACT_B,
                completed_at="2026-01-01T00:01:10Z",
            )
            registry.register(q1)
            with self.assertRaisesRegex(
                ProviderQualificationAuthorityError, "supersession required"
            ):
                registry.register(q2)

    def test_same_scope_stale_writer_conflicts_transactionally(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            stale = DurableProviderQualificationRegistry(store)
            q1 = _q()
            stale.register(q1)
            stale_cut = stale._validated_history_cut()

            winner = DurableProviderQualificationRegistry(self._store(directory))
            q2 = _q(
                artifact_id=ARTIFACT_B,
                completed_at="2026-01-01T00:01:10Z",
            )
            winner.supersede(q1.qualification_id, q2)

            q3 = _q(
                artifact_id=ARTIFACT_C,
                completed_at="2026-01-01T00:01:20Z",
            )
            with patch.object(
                stale,
                "_validated_history_cut",
                return_value=stale_cut,
            ):
                with self.assertRaisesRegex(
                    ProviderQualificationAuthorityError, "changed concurrently"
                ):
                    stale.supersede(q1.qualification_id, q3)

            restarted = DurableProviderQualificationRegistry(
                self._store(directory)
            )
            self.assertEqual(restarted.historical_current_for(q2), q2)
            events = store.load_events_by_aggregate_type(
                "provider_qualification"
            )
            self.assertEqual(
                [event["aggregate_version"] for event in events], [1, 2]
            )

    def test_unrelated_scope_write_does_not_conflict_with_same_cut(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            first = DurableProviderQualificationRegistry(store)
            q1 = _q()
            first.register(q1)
            first_cut = first._validated_history_cut()

            other_campaign = _campaign()
            other_campaign["product_family"] = "LINEAR"
            other_campaign["campaign_artifact_id"] = ARTIFACT_B
            other = issue_accepted_provider_qualification(
                accepted_attestation=_accepted(
                    other_campaign, attestation_id=ATTEST_B
                ),
                campaign_payload=other_campaign,
            )
            DurableProviderQualificationRegistry(
                self._store(directory)
            ).register(other)

            replacement = _q(
                artifact_id=ARTIFACT_C,
                completed_at="2026-01-01T00:01:20Z",
            )
            with patch.object(
                first,
                "_validated_history_cut",
                return_value=first_cut,
            ):
                self.assertTrue(
                    first.supersede(q1.qualification_id, replacement)
                )
            self.assertEqual(
                DurableProviderQualificationRegistry(
                    self._store(directory)
                ).historical_current_for(replacement),
                replacement,
            )


    def test_historical_current_route_lookup_binds_complete_domain_and_build_scope(self):
        with TemporaryDirectory() as directory:
            registry = DurableProviderQualificationRegistry(
                self._store(directory)
            )
            q1 = _q()
            registry.register(q1)
            resolved = registry.historical_current_for_route(
                provider_id="BYBIT",
                product_family="SPOT",
                environment="PAPER",
                provider_environment="TESTNET",
                route_policy_id="bybit-v5-private",
                entity_policy_id="bybit-account-v1",
                network_policy_id="direct-tls-v1",
                account_class="STANDARD",
                adapter_source_sha=SOURCE_SHA,
            )
            self.assertEqual(resolved, q1)
            with self.assertRaises(
                ProviderQualificationCurrentnessUnavailable
            ):
                registry.historical_current_for_route(
                    provider_id="BYBIT",
                    product_family="SPOT",
                    environment="PAPER",
                    provider_environment="DEMO",
                    route_policy_id="bybit-v5-private",
                    entity_policy_id="bybit-account-v1",
                    network_policy_id="direct-tls-v1",
                    account_class="STANDARD",
                    adapter_source_sha=SOURCE_SHA,
                )



class ProviderQualificationCurrentReaderTests(unittest.TestCase):
    def _registry(self, directory: str):
        store = JournalStore(Path(directory) / "journal.db")
        registry = DurableProviderQualificationRegistry(store)
        return store, registry

    @staticmethod
    def _snapshot(campaign: dict[str, object]):
        data = canonical_json(campaign).encode("utf-8")
        manifest = {
            "artifact_id": campaign["campaign_artifact_id"],
            "sha256": _campaign_digest(campaign),
            "media_type": "application/json",
            "metadata": {
                "evidence_kind": "PROVIDER_QUALIFICATION_CAMPAIGN",
            },
            "source_refs": [f"git:{SOURCE_SHA}"],
        }
        return manifest, data

    def test_route_lookup_revalidates_exact_expected_current_q(self):
        with TemporaryDirectory() as directory:
            _store, registry = self._registry(directory)
            q1 = _q()
            registry.register(q1)
            reader = object.__new__(ProviderQualificationCurrentReader)
            reader._registry = registry
            with patch.object(reader, "require_current", return_value=q1):
                self.assertEqual(
                    reader.require_current_for_route(
                        provider_id="BYBIT",
                        product_family="SPOT",
                        environment="PAPER",
                        provider_environment="TESTNET",
                        route_policy_id="bybit-v5-private",
                        entity_policy_id="bybit-account-v1",
                        network_policy_id="direct-tls-v1",
                        account_class="STANDARD",
                        adapter_source_sha=SOURCE_SHA,
                        expected_qualification_id=q1.qualification_id,
                    ),
                    q1,
                )
            with self.assertRaises(
                ProviderQualificationCurrentnessUnavailable
            ):
                reader.require_current_for_route(
                    provider_id="BYBIT",
                    product_family="SPOT",
                    environment="PAPER",
                    provider_environment="TESTNET",
                    route_policy_id="bybit-v5-private",
                    entity_policy_id="bybit-account-v1",
                    network_policy_id="direct-tls-v1",
                    account_class="STANDARD",
                    adapter_source_sha=SOURCE_SHA,
                    expected_qualification_id="sha256:" + "f" * 64,
                )

    def test_reverifies_signed_authority_and_exact_retained_campaign(self):
        with TemporaryDirectory() as directory:
            campaign = _campaign()
            qualification = issue_accepted_provider_qualification(
                accepted_attestation=_accepted(campaign),
                campaign_payload=campaign,
            )
            _store, registry = self._registry(directory)
            registry.register(qualification)
            artifact_store = ArtifactStore(Path(directory) / "artifacts")
            manifest, data = self._snapshot(campaign)

            with patch.object(
                authority_module,
                "trusted_authenticated_reader",
                return_value=lambda artifact_id: (manifest, data),
            ), patch.object(
                authority_module,
                "parse_signed_qualification_attestation",
                return_value=object(),
            ), patch.object(
                authority_module,
                "verify_canonical_qualification_attestation",
                return_value=_accepted(campaign),
            ), patch.object(
                authority_module,
                "_utc_now",
                return_value=datetime(
                    2026, 6, 1, tzinfo=timezone.utc
                ),
            ):
                reader = ProviderQualificationCurrentReader(
                    registry,
                    evidence_store=artifact_store,
                    evidence_root=artifact_store.root,
                )
                self.assertEqual(
                    reader.require_current(qualification.qualification_id),
                    qualification,
                )


    def test_campaign_completion_before_signature_is_not_current_authority(self):
        with TemporaryDirectory() as directory:
            campaign = _campaign()
            qualification = issue_accepted_provider_qualification(
                accepted_attestation=_accepted(campaign),
                campaign_payload=campaign,
            )
            _store, registry = self._registry(directory)
            registry.register(qualification)
            artifact_store = ArtifactStore(Path(directory) / "artifacts")
            with patch.object(
                authority_module,
                "trusted_authenticated_reader",
                return_value=lambda artifact_id: self.fail(
                    "pre-signature Q must fail before campaign read"
                ),
            ), patch.object(
                authority_module,
                "_utc_now",
                return_value=datetime(
                    2026, 1, 1, 0, 2, 30, tzinfo=timezone.utc
                ),
            ):
                reader = ProviderQualificationCurrentReader(
                    registry,
                    evidence_store=artifact_store,
                    evidence_root=artifact_store.root,
                )
                with self.assertRaises(
                    ProviderQualificationCurrentnessUnavailable
                ):
                    reader.require_current(
                        qualification.qualification_id
                    )

    def test_expired_campaign_fails_before_terminal_currentness(self):
        with TemporaryDirectory() as directory:
            campaign = _campaign()
            qualification = issue_accepted_provider_qualification(
                accepted_attestation=_accepted(campaign),
                campaign_payload=campaign,
            )
            _store, registry = self._registry(directory)
            registry.register(qualification)
            artifact_store = ArtifactStore(Path(directory) / "artifacts")
            with patch.object(
                authority_module,
                "trusted_authenticated_reader",
                return_value=lambda artifact_id: self.fail(
                    "expired Q must fail before campaign read"
                ),
            ), patch.object(
                authority_module,
                "_utc_now",
                return_value=datetime(
                    2028, 1, 1, tzinfo=timezone.utc
                ),
            ):
                reader = ProviderQualificationCurrentReader(
                    registry,
                    evidence_store=artifact_store,
                    evidence_root=artifact_store.root,
                )
                with self.assertRaises(
                    ProviderQualificationCurrentnessUnavailable
                ):
                    reader.require_current(
                        qualification.qualification_id
                    )

    def test_reverified_trust_material_must_equal_durable_q(self):
        with TemporaryDirectory() as directory:
            campaign = _campaign()
            accepted = _accepted(campaign)
            qualification = issue_accepted_provider_qualification(
                accepted_attestation=accepted,
                campaign_payload=campaign,
            )
            _store, registry = self._registry(directory)
            registry.register(qualification)
            artifact_store = ArtifactStore(Path(directory) / "artifacts")
            manifest, data = self._snapshot(campaign)
            changed = replace(
                accepted,
                policy_id="sha256:" + "f" * 64,
            )
            with patch.object(
                authority_module,
                "trusted_authenticated_reader",
                return_value=lambda artifact_id: (manifest, data),
            ), patch.object(
                authority_module,
                "parse_signed_qualification_attestation",
                return_value=object(),
            ), patch.object(
                authority_module,
                "verify_canonical_qualification_attestation",
                return_value=changed,
            ), patch.object(
                authority_module,
                "_utc_now",
                return_value=datetime(
                    2026, 6, 1, tzinfo=timezone.utc
                ),
            ):
                reader = ProviderQualificationCurrentReader(
                    registry,
                    evidence_store=artifact_store,
                    evidence_root=artifact_store.root,
                )
                with self.assertRaisesRegex(
                    ProviderQualificationAuthorityError,
                    "differs from durable Q",
                ):
                    reader.require_current(
                        qualification.qualification_id
                    )

    def test_campaign_bytes_must_match_accepted_digest_and_semantics(self):
        with TemporaryDirectory() as directory:
            campaign = _campaign()
            accepted = _accepted(campaign)
            qualification = issue_accepted_provider_qualification(
                accepted_attestation=accepted,
                campaign_payload=campaign,
            )
            _store, registry = self._registry(directory)
            registry.register(qualification)
            artifact_store = ArtifactStore(Path(directory) / "artifacts")
            manifest, _data = self._snapshot(campaign)
            changed = deepcopy(campaign)
            changed["documentation_revisions"] = ["bybit-v5:tampered"]
            tampered = canonical_json(changed).encode("utf-8")
            with patch.object(
                authority_module,
                "trusted_authenticated_reader",
                return_value=lambda artifact_id: (manifest, tampered),
            ), patch.object(
                authority_module,
                "parse_signed_qualification_attestation",
                return_value=object(),
            ), patch.object(
                authority_module,
                "verify_canonical_qualification_attestation",
                return_value=accepted,
            ), patch.object(
                authority_module,
                "_utc_now",
                return_value=datetime(
                    2026, 6, 1, tzinfo=timezone.utc
                ),
            ):
                reader = ProviderQualificationCurrentReader(
                    registry,
                    evidence_store=artifact_store,
                    evidence_root=artifact_store.root,
                )
                with self.assertRaisesRegex(
                    ProviderQualificationAuthorityError,
                    "bytes changed",
                ):
                    reader.require_current(
                        qualification.qualification_id
                    )

    def test_supersession_during_retained_read_is_caught(self):
        with TemporaryDirectory() as directory:
            campaign1 = _campaign()
            q1 = issue_accepted_provider_qualification(
                accepted_attestation=_accepted(campaign1),
                campaign_payload=campaign1,
            )
            campaign2 = _campaign(
                artifact_id=ARTIFACT_B,
                completed_at="2026-01-01T00:01:10Z",
            )
            q2 = issue_accepted_provider_qualification(
                accepted_attestation=_accepted(
                    campaign2,
                    attestation_id=ATTEST_B,
                ),
                campaign_payload=campaign2,
            )
            _store, registry = self._registry(directory)
            registry.register(q1)
            artifact_store = ArtifactStore(Path(directory) / "artifacts")
            manifest, data = self._snapshot(campaign1)

            def read_and_supersede(artifact_id):
                registry.supersede(q1.qualification_id, q2)
                return manifest, data

            with patch.object(
                authority_module,
                "trusted_authenticated_reader",
                return_value=read_and_supersede,
            ), patch.object(
                authority_module,
                "parse_signed_qualification_attestation",
                return_value=object(),
            ), patch.object(
                authority_module,
                "verify_canonical_qualification_attestation",
                return_value=_accepted(campaign1),
            ), patch.object(
                authority_module,
                "_utc_now",
                return_value=datetime(
                    2026, 6, 1, tzinfo=timezone.utc
                ),
            ):
                reader = ProviderQualificationCurrentReader(
                    registry,
                    evidence_store=artifact_store,
                    evidence_root=artifact_store.root,
                )
                with self.assertRaisesRegex(
                    ProviderQualificationCurrentnessUnavailable,
                    "changed during revalidation",
                ):
                    reader.require_current(q1.qualification_id)

    def test_exact_store_and_registry_types_are_required(self):
        class DerivedArtifactStore(ArtifactStore):
            pass

        with TemporaryDirectory() as directory:
            _store, registry = self._registry(directory)
            artifact_store = DerivedArtifactStore(
                Path(directory) / "artifacts"
            )
            with self.assertRaisesRegex(
                TypeError, "canonical ArtifactStore"
            ):
                ProviderQualificationCurrentReader(
                    registry,
                    evidence_store=artifact_store,
                    evidence_root=artifact_store.root,
                )


    def test_current_scope_resolves_exact_route_qualified_authority(self):
        with TemporaryDirectory() as directory:
            campaign = _campaign()
            qualification = issue_accepted_provider_qualification(
                accepted_attestation=_accepted(campaign),
                campaign_payload=campaign,
            )
            _store, registry = self._registry(directory)
            registry.register(qualification)
            artifact_store = ArtifactStore(Path(directory) / "artifacts")
            manifest, data = self._snapshot(campaign)
            with patch.object(
                authority_module,
                "trusted_authenticated_reader",
                return_value=lambda _artifact_id: (manifest, data),
            ), patch.object(
                authority_module,
                "parse_signed_qualification_attestation",
                return_value=object(),
            ), patch.object(
                authority_module,
                "verify_canonical_qualification_attestation",
                return_value=_accepted(campaign),
            ), patch.object(
                authority_module,
                "_utc_now",
                return_value=datetime(2026, 6, 1, tzinfo=timezone.utc),
            ):
                reader = ProviderQualificationCurrentReader(
                    registry,
                    evidence_store=artifact_store,
                    evidence_root=artifact_store.root,
                )
                self.assertEqual(
                    reader.require_current_scope(
                        provider_id="BYBIT",
                        environment="PAPER",
                        provider_environment="TESTNET",
                        route_policy_id="bybit-v5-private",
                    ),
                    qualification,
                )

    def test_current_scope_rejects_missing_route_identity(self):
        with TemporaryDirectory() as directory:
            qualification = _q()
            _store, registry = self._registry(directory)
            registry.register(qualification)
            artifact_store = ArtifactStore(Path(directory) / "artifacts")
            with patch.object(
                authority_module,
                "trusted_authenticated_reader",
                return_value=lambda _artifact_id: ({}, b""),
            ):
                reader = ProviderQualificationCurrentReader(
                    registry,
                    evidence_store=artifact_store,
                    evidence_root=artifact_store.root,
                )
                with self.assertRaisesRegex(
                    ProviderQualificationCurrentnessUnavailable,
                    "missing or ambiguous",
                ):
                    reader.require_current_scope(
                        provider_id="BYBIT",
                        environment="PAPER",
                        provider_environment="TESTNET",
                        route_policy_id="sha256:" + "9" * 64,
                    )

    def test_current_scope_rejects_two_current_qs_for_same_route(self):
        with TemporaryDirectory() as directory:
            _store, registry = self._registry(directory)
            q1 = _q()
            campaign2 = _campaign(
                artifact_id=ARTIFACT_B,
                completed_at="2026-01-01T00:01:10Z",
                account_class="UNIFIED",
            )
            q2 = issue_accepted_provider_qualification(
                accepted_attestation=_accepted(
                    campaign2,
                    attestation_id=ATTEST_B,
                    signed_at="2026-01-01T00:03:10Z",
                ),
                campaign_payload=campaign2,
            )
            registry.register(q1)
            registry.register(q2)
            artifact_store = ArtifactStore(Path(directory) / "artifacts")
            with patch.object(
                authority_module,
                "trusted_authenticated_reader",
                return_value=lambda _artifact_id: ({}, b""),
            ):
                reader = ProviderQualificationCurrentReader(
                    registry,
                    evidence_store=artifact_store,
                    evidence_root=artifact_store.root,
                )
                with self.assertRaisesRegex(
                    ProviderQualificationCurrentnessUnavailable,
                    "missing or ambiguous",
                ):
                    reader.require_current_scope(
                        provider_id="BYBIT",
                        environment="PAPER",
                        provider_environment="TESTNET",
                        route_policy_id="bybit-v5-private",
                    )

if __name__ == "__main__":
    unittest.main()
