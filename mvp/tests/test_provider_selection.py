from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import MappingProxyType
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    CapabilityRegistry,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.persistence import canonical_json
from mvp.autotrade_mvp.provider_core import (
    QualificationEvidence,
    REQUIRED_QUALIFICATION_CASES,
)
import mvp.autotrade_mvp.provider_selection as selection_module
from mvp.autotrade_mvp.provider_selection import (
    ProviderCandidate,
    ProviderRouteRequest,
    SelectedProviderAuthority,
    revalidate_selected_provider_authority,
    select_provider,
)
from mvp.autotrade_mvp.provider_qualification_authority import (
    ProviderQualificationCurrentReader,
    issue_accepted_provider_qualification,
)
from mvp.autotrade_mvp.qualification_attestation import (
    AcceptedQualificationAttestation,
    EvidenceArtifactRef,
)


NOW = datetime(2026, 9, 24, 18, tzinfo=timezone.utc)
INSTRUMENT = "instrument-v1"
SOURCE_SHA = "1" * 40


def capability(
    *,
    provider_environment: str = "TESTNET",
    snapshot_suffix: str = "a",
    observed_offset_seconds: int = 0,
):
    provider = "BYBIT"
    observed = NOW + timedelta(seconds=observed_offset_seconds)
    claims = []
    for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT"):
        artifact_id = str(
            uuid5(
                NAMESPACE_URL,
                f"{provider}:{source}:{provider_environment}:{snapshot_suffix}",
            )
        )
        claims.append(
            CapabilityClaim(
                source=source,
                provider_id=provider,
                account_id="bybit-account",
                entity_id="entity-1",
                environment="PAPER",
                provider_environment=provider_environment,
                instrument_version=INSTRUMENT,
                observed_at=observed - timedelta(minutes=1),
                expires_at=NOW + timedelta(hours=2),
                supported_order_types=frozenset({"LIMIT", "MARKET"}),
                time_in_force=frozenset({"GTC", "IOC"}),
                permission_scopes=frozenset({"ORDER.WRITE", "ORDER.READ"}),
                position_mode="NET",
                native_protection=frozenset(),
                rate_limit_policy_id="bybit-limits",
                data_entitlements=frozenset({"QUOTE", "TRADE"}),
                evidence_ref={
                    "artifact_id": artifact_id,
                    "sha256": "sha256:" + "a" * 64,
                    "observed_at": (
                        observed - timedelta(minutes=1)
                    ).isoformat().replace("+00:00", "Z"),
                },
            )
        )
    return derive_capability_snapshot(
        snapshot_id=str(
            uuid5(
                NAMESPACE_URL,
                f"snapshot:{provider_environment}:{snapshot_suffix}",
            )
        ),
        claims=claims,
        observed_at=observed,
        evidence_verifier=lambda claim: EvidenceVerification(valid=True),
    )


def raw_qualification(*, unsupported=()):
    return QualificationEvidence(
        provider_id="BYBIT",
        product_family="SPOT",
        environment="PAPER",
        adapter_code_sha=SOURCE_SHA,
        documentation_ref="official:bybit:spot",
        observed_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(days=1),
        passed_cases=REQUIRED_QUALIFICATION_CASES,
        unsupported_features=tuple(unsupported),
    )


def campaign(
    *,
    provider_environment: str = "TESTNET",
    artifact_id: str = "00000000-0000-0000-0000-000000000001",
    unsupported=(),
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
        "account_class": "STANDARD",
        "adapter_source_sha": SOURCE_SHA,
        "campaign_protocol_id": "provider-qualification",
        "campaign_protocol_version": "1.0.0",
        "required_case_set_version": "provider-qualification:v1",
        "case_results": [
            {"case_id": case_id, "result": "PASS"}
            for case_id in sorted(REQUIRED_QUALIFICATION_CASES)
        ],
        "unsupported_features": list(unsupported),
        "documentation_revisions": ["bybit-v5:2026-01"],
        "campaign_artifact_id": artifact_id,
        "started_at": "2026-01-01T00:00:10Z",
        "completed_at": "2026-01-01T00:01:00Z",
        "valid_until": "2027-01-01T00:00:00Z",
        "release_artifact_id": None,
        "release_artifact_sha256": None,
        "live_authorized": False,
        "reconciliation_semantics": None,
    }


def accepted(campaign_payload, *, attestation_id):
    encoded = canonical_json(campaign_payload).encode("utf-8")
    campaign_sha = "sha256:" + sha256(encoded).hexdigest()
    attestation_json = canonical_json(
        {
            "attestation_id": attestation_id,
            "source_sha": SOURCE_SHA,
            "domain": "PROVIDER",
            "gate": "PROVIDER_QUALIFICATION",
            "package_id": "AUTOTRADE_PROVIDER_QUALIFICATION",
            "protocol_id": "provider-qualification",
            "protocol_version": "1.0.0",
            "result": "PASS",
        }
    )
    return AcceptedQualificationAttestation(
        attestation_id=attestation_id,
        attestation_digest=(
            "sha256:" + sha256(attestation_json.encode("utf-8")).hexdigest()
        ),
        policy_id="sha256:" + "b" * 64,
        policy_version="1.0.0",
        trust_root_id="sha256:" + "c" * 64,
        result="PASS",
        source_sha=SOURCE_SHA,
        domain="PROVIDER",
        gate="PROVIDER_QUALIFICATION",
        package_id="AUTOTRADE_PROVIDER_QUALIFICATION",
        protocol_id="provider-qualification",
        protocol_version="1.0.0",
        requirement_id="provider-qualification:v1",
        requirement_ids=("provider-qualification:v1",),
        evidence_refs=(
            EvidenceArtifactRef(
                artifact_id=campaign_payload["campaign_artifact_id"],
                sha256=campaign_sha,
                media_type="application/json",
                evidence_kind="PROVIDER_QUALIFICATION_CAMPAIGN",
                source_sha=SOURCE_SHA,
            ),
        ),
        producer_id="provider-campaign",
        verifier_id="autotrade-verifier",
        runner_id="ci-runner",
        harness_version="1.0.0",
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:02:00Z",
        signed_at="2026-01-01T00:03:00Z",
        unresolved_limits=(),
        schema_version="1.0.0",
        verification_method="RSA_PKCS1V15_SHA256",
        release_artifact_id=None,
        release_artifact_sha256=None,
        attestation_json=attestation_json,
        signature_b64="AA==",
    )


def qualification(
    *,
    provider_environment: str = "TESTNET",
    artifact_id: str = "00000000-0000-0000-0000-000000000001",
    attestation_id: str = "00000000-0000-0000-0000-000000000101",
    unsupported=(),
):
    value = campaign(
        provider_environment=provider_environment,
        artifact_id=artifact_id,
        unsupported=unsupported,
    )
    return issue_accepted_provider_qualification(
        accepted_attestation=accepted(value, attestation_id=attestation_id),
        campaign_payload=value,
    )


def candidate(capability_snapshot, *, qid=None, raw=None):
    return ProviderCandidate(
        provider_id="BYBIT",
        product_family="SPOT",
        adapter_code_sha=SOURCE_SHA,
        qualification=raw if raw is not None else raw_qualification(),
        capability=capability_snapshot,
        qualification_id=qid,
        route_policy_id="bybit-v5-private",
        entity_policy_id="bybit-account-v1",
        network_policy_id="direct-tls-v1",
        account_class="STANDARD",
    )


def request():
    return ProviderRouteRequest(
        asset_class="CRYPTO_SPOT",
        environment="PAPER",
        instrument_version=INSTRUMENT,
        order_type="LIMIT",
        time_in_force="GTC",
        permission_scope="ORDER.WRITE",
    )


class ProviderSelectionAuthorityTests(unittest.TestCase):
    def test_shape_valid_raw_qualification_cannot_authorize_without_q_and_c(self):
        cap = capability()
        result = select_provider(
            request(),
            [candidate(cap, raw=raw_qualification())],
            at=NOW,
        )
        self.assertEqual(result.status, "NO_ELIGIBLE_PROVIDER")
        self.assertIn(
            "QUALIFICATION_AUTHORITY_REQUIRED",
            result.decisions[0].reasons,
        )
        self.assertIn(
            "CAPABILITY_AUTHORITY_REQUIRED",
            result.decisions[0].reasons,
        )

    def test_exact_current_q_and_c_publish_only_authority_identities(self):
        cap = capability()
        registry = CapabilityRegistry()
        registry.add(cap)
        q = qualification()
        reader = object.__new__(ProviderQualificationCurrentReader)

        with patch.object(
            selection_module,
            "_current_qualification",
            side_effect=[q, q],
        ):
            result = select_provider(
                request(),
                [candidate(cap, qid=q.qualification_id)],
                at=NOW,
                qualification_reader=reader,
                capability_registry=registry,
            )

        self.assertEqual(result.status, "SELECTED_UNAMBIGUOUS")
        self.assertIs(type(result.selected), SelectedProviderAuthority)
        self.assertEqual(result.selected.qualification_id, q.qualification_id)
        self.assertEqual(result.selected.capability_snapshot_id, cap.snapshot_id)
        self.assertFalse(hasattr(result.selected, "qualification"))

    def test_signed_q_unsupported_feature_wins_over_permissive_raw_value(self):
        cap = capability()
        registry = CapabilityRegistry()
        registry.add(cap)
        q = qualification(unsupported=("ORDER_TYPE:LIMIT",))
        reader = object.__new__(ProviderQualificationCurrentReader)

        with patch.object(
            selection_module,
            "_current_qualification",
            return_value=q,
        ):
            result = select_provider(
                request(),
                [candidate(cap, qid=q.qualification_id, raw=raw_qualification())],
                at=NOW,
                qualification_reader=reader,
                capability_registry=registry,
            )

        self.assertEqual(result.status, "NO_ELIGIBLE_PROVIDER")
        self.assertIn(
            "QUALIFICATION_EXPLICITLY_UNSUPPORTED",
            result.decisions[0].reasons,
        )

    def test_q_and_capability_provider_domains_cannot_alias(self):
        cap = capability(provider_environment="DEMO")
        registry = CapabilityRegistry()
        registry.add(cap)
        q = qualification(provider_environment="TESTNET")
        reader = object.__new__(ProviderQualificationCurrentReader)

        with patch.object(
            selection_module,
            "_current_qualification",
            return_value=q,
        ):
            result = select_provider(
                request(),
                [candidate(cap)],
                at=NOW,
                qualification_reader=reader,
                capability_registry=registry,
            )

        self.assertEqual(result.status, "NO_ELIGIBLE_PROVIDER")
        self.assertIn(
            "QUALIFICATION_CAPABILITY_DOMAIN_MISMATCH",
            result.decisions[0].reasons,
        )

    def test_q_change_during_bounded_selection_fails_closed(self):
        cap = capability()
        registry = CapabilityRegistry()
        registry.add(cap)
        q1 = qualification()
        q2 = qualification(
            artifact_id="00000000-0000-0000-0000-000000000002",
            attestation_id="00000000-0000-0000-0000-000000000102",
        )
        reader = object.__new__(ProviderQualificationCurrentReader)

        with patch.object(
            selection_module,
            "_current_qualification",
            side_effect=[q1, q2],
        ):
            result = select_provider(
                request(),
                [candidate(cap)],
                at=NOW,
                qualification_reader=reader,
                capability_registry=registry,
            )

        self.assertEqual(result.status, "NO_ELIGIBLE_PROVIDER")
        self.assertIn(
            "QUALIFICATION_REGISTRY_CHANGED_DURING_SELECTION",
            result.decisions[0].reasons,
        )

    def test_capability_change_during_bounded_selection_fails_closed(self):
        cap1 = capability(snapshot_suffix="a")
        cap2 = capability(snapshot_suffix="b", observed_offset_seconds=1)
        registry = CapabilityRegistry()
        registry.add(cap1)
        registry.add(cap2)
        q = qualification()
        reader = object.__new__(ProviderQualificationCurrentReader)

        with patch.object(
            selection_module,
            "_current_qualification",
            side_effect=[q, q],
        ), patch.object(
            CapabilityRegistry,
            "require_verified",
            side_effect=[cap1, cap2],
        ):
            result = select_provider(
                request(),
                [candidate(cap1)],
                at=NOW,
                qualification_reader=reader,
                capability_registry=registry,
            )

        self.assertEqual(result.status, "NO_ELIGIBLE_PROVIDER")
        self.assertIn(
            "CAPABILITY_REGISTRY_CHANGED_DURING_SELECTION",
            result.decisions[0].reasons,
        )

    def test_final_barrier_revalidates_exact_selected_q1_and_c1(self):
        cap = capability()
        registry = CapabilityRegistry()
        registry.add(cap)
        q = qualification()
        reader = object.__new__(ProviderQualificationCurrentReader)

        with patch.object(
            selection_module,
            "_current_qualification",
            side_effect=[q, q],
        ):
            selected = select_provider(
                request(),
                [candidate(cap, qid=q.qualification_id)],
                at=NOW,
                qualification_reader=reader,
                capability_registry=registry,
            ).selected
        self.assertIs(type(selected), SelectedProviderAuthority)

        with patch.object(
            ProviderQualificationCurrentReader,
            "require_current_for_route",
            return_value=q,
        ):
            current_q, current_c = revalidate_selected_provider_authority(
                selected,
                qualification_reader=reader,
                capability_registry=registry,
                at=NOW,
            )
        self.assertEqual(current_q, q)
        self.assertEqual(current_c.snapshot_id, cap.snapshot_id)

    def test_final_barrier_rejects_q2_for_selected_q1(self):
        cap = capability()
        registry = CapabilityRegistry()
        registry.add(cap)
        q1 = qualification()
        q2 = qualification(
            artifact_id="00000000-0000-0000-0000-000000000002",
            attestation_id="00000000-0000-0000-0000-000000000102",
        )
        reader = object.__new__(ProviderQualificationCurrentReader)

        with patch.object(
            selection_module,
            "_current_qualification",
            side_effect=[q1, q1],
        ):
            selected = select_provider(
                request(),
                [candidate(cap, qid=q1.qualification_id)],
                at=NOW,
                qualification_reader=reader,
                capability_registry=registry,
            ).selected

        with patch.object(
            ProviderQualificationCurrentReader,
            "require_current_for_route",
            return_value=q2,
        ):
            with self.assertRaisesRegex(
                selection_module.ProviderSelectionError,
                "qualification identity changed before wire",
            ):
                revalidate_selected_provider_authority(
                    selected,
                    qualification_reader=reader,
                    capability_registry=registry,
                    at=NOW,
                )

    def test_final_barrier_rejects_c2_for_selected_c1(self):
        cap1 = capability(snapshot_suffix="a")
        cap2 = capability(snapshot_suffix="b", observed_offset_seconds=1)
        registry = CapabilityRegistry()
        registry.add(cap1)
        q = qualification()
        reader = object.__new__(ProviderQualificationCurrentReader)

        with patch.object(
            selection_module,
            "_current_qualification",
            side_effect=[q, q],
        ):
            selected = select_provider(
                request(),
                [candidate(cap1, qid=q.qualification_id)],
                at=NOW,
                qualification_reader=reader,
                capability_registry=registry,
            ).selected

        with patch.object(
            ProviderQualificationCurrentReader,
            "require_current_for_route",
            return_value=q,
        ), patch.object(
            CapabilityRegistry,
            "require_verified",
            return_value=cap2,
        ):
            with self.assertRaisesRegex(
                selection_module.ProviderSelectionError,
                "capability identity changed before wire",
            ):
                revalidate_selected_provider_authority(
                    selected,
                    qualification_reader=reader,
                    capability_registry=registry,
                    at=NOW,
                )

    def test_structural_or_proxy_authorities_are_rejected(self):
        class Proxy:
            def require_verified(self, **kwargs):
                raise AssertionError("must not be called")

        cap = capability()
        with self.assertRaisesRegex(TypeError, "exact CapabilityRegistry"):
            select_provider(
                request(),
                [candidate(cap)],
                at=NOW,
                capability_registry=Proxy(),
            )
        with self.assertRaisesRegex(
            TypeError,
            "exact ProviderQualificationCurrentReader",
        ):
            select_provider(
                request(),
                [candidate(cap)],
                at=NOW,
                qualification_reader=Proxy(),
            )


if __name__ == "__main__":
    unittest.main()
