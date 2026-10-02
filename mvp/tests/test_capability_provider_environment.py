from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    CapabilityError,
    CapabilityRegistry,
    CapabilitySnapshot,
    EvidenceVerification,
    artifact_store_evidence_verifier,
    derive_capability_snapshot,
)


NOW = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def evidence(source: str, *, payload: bytes = b"capability") -> dict[str, object]:
    return {
        "artifact_id": str(uuid4()),
        "sha256": "sha256:" + sha256(payload).hexdigest(),
        "observed_at": NOW.isoformat().replace("+00:00", "Z"),
        "issuer_ref": source.lower() + ":sha256:" + "a" * 64,
        "issuer_sha256": "sha256:" + "b" * 64,
    }


def claim(
    source: str,
    *,
    provider_id: str = "SIMULATED",
    environment: str = "PAPER",
    provider_environment: str | None = None,
    payload: bytes = b"capability",
) -> CapabilityClaim:
    return CapabilityClaim(
        source=source,
        provider_id=provider_id,
        account_id="paper-account",
        entity_id="entity-1",
        environment=environment,
        instrument_version="instrument-v1",
        observed_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        supported_order_types=frozenset({"LIMIT"}),
        time_in_force=frozenset({"GTC"}),
        permission_scopes=frozenset({"ORDER.READ"}),
        position_mode="NET",
        native_protection=frozenset({"STOP"}),
        rate_limit_policy_id="rate-v1",
        data_entitlements=frozenset({"QUOTE"}),
        evidence_ref=evidence(source, payload=payload),
        provider_environment=provider_environment,
    )


def snapshot(*, provider_environment: str):
    claims = tuple(
        claim(
            source,
            provider_id="BYBIT",
            provider_environment=provider_environment,
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=NOW,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


class CapabilityProviderEnvironmentTests(unittest.TestCase):
    def test_non_bybit_default_is_runtime_domain_for_compatibility(self):
        item = claim("DOCUMENTED")
        self.assertEqual(item.provider_environment, "PAPER")

    def test_bybit_paper_requires_explicit_provider_environment(self):
        with self.assertRaisesRegex(
            CapabilityError,
            "BYBIT requires explicit provider_environment",
        ):
            claim("DOCUMENTED", provider_id="BYBIT")

    def test_bybit_testnet_and_demo_are_distinct_capability_identities(self):
        testnet = snapshot(provider_environment="TESTNET")
        demo = snapshot(provider_environment="DEMO")
        self.assertNotEqual(testnet.identity, demo.identity)
        self.assertEqual(testnet.provider_environment, "TESTNET")
        self.assertEqual(demo.provider_environment, "DEMO")
        self.assertEqual(
            testnet.to_contract_dict()["provider_environment"],
            "TESTNET",
        )

        registry = CapabilityRegistry()
        registry.add(testnet)
        registry.add(demo)
        self.assertEqual(
            registry.latest(
                provider_id="BYBIT",
                account_id="paper-account",
                entity_id="entity-1",
                environment="PAPER",
                provider_environment="TESTNET",
                instrument_version="instrument-v1",
                at=NOW,
            ),
            testnet,
        )
        self.assertEqual(
            registry.latest(
                provider_id="BYBIT",
                account_id="paper-account",
                entity_id="entity-1",
                environment="PAPER",
                provider_environment="DEMO",
                instrument_version="instrument-v1",
                at=NOW,
            ),
            demo,
        )

    def test_mixed_bybit_provider_domains_cannot_form_one_snapshot(self):
        claims = [
            claim(
                source,
                provider_id="BYBIT",
                provider_environment="TESTNET",
            )
            for source in ("DOCUMENTED", "API", "ACCOUNT")
        ]
        claims.append(
            claim(
                "INSTRUMENT",
                provider_id="BYBIT",
                provider_environment="DEMO",
            )
        )
        with self.assertRaisesRegex(
            CapabilityError,
            "claims describe different identities",
        ):
            derive_capability_snapshot(
                snapshot_id=str(uuid4()),
                claims=claims,
                observed_at=NOW,
                evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
            )

    def test_artifact_metadata_binds_exact_provider_environment(self):
        payload = b"provider-capability"
        item = claim(
            "DOCUMENTED",
            provider_id="BYBIT",
            provider_environment="TESTNET",
            payload=payload,
        )
        evidence_ref = item.evidence_ref

        class Store:
            def __init__(self, provider_environment: str):
                self.provider_environment = provider_environment

            def load_manifest(self, artifact_id):
                return {
                    "artifact_id": artifact_id,
                    "sha256": evidence_ref["sha256"],
                    "metadata": {
                        "artifact_kind": "CAPABILITY_EVIDENCE",
                        "schema_version": 1,
                        "capability_source": "DOCUMENTED",
                        "producer_type": "PROVIDER_DOCUMENTATION",
                        "provider_id": "BYBIT",
                        "account_id": "paper-account",
                        "entity_id": "entity-1",
                        "environment": "PAPER",
                        "provider_environment": self.provider_environment,
                        "instrument_version": "instrument-v1",
                        "observed_at": evidence_ref["observed_at"],
                        "producer_id": "fixture",
                        "evidence_version": "1",
                        "issuer_ref": evidence_ref["issuer_ref"],
                        "issuer_sha256": evidence_ref["issuer_sha256"],
                    },
                    "source_refs": [],
                    "rights": {},
                }

            def read_bytes(self, _artifact_id):
                return payload

        issuers = {
            "DOCUMENTED": (
                lambda _claim, _issuer_ref, _issuer_sha256:
                EvidenceVerification(valid=True)
            )
        }
        valid = artifact_store_evidence_verifier(
            Store("TESTNET"),
            issuer_verifiers=issuers,
        )(item)
        self.assertTrue(valid.valid)

        wrong_domain = artifact_store_evidence_verifier(
            Store("DEMO"),
            issuer_verifiers=issuers,
        )(item)
        self.assertFalse(wrong_domain.valid)
        self.assertTrue(wrong_domain.conflicted)


    def test_claim_subclass_is_rejected_before_evidence_callback(self):
        base = claim("DOCUMENTED")

        class ClaimSubclass(CapabilityClaim):
            pass

        impostor = ClaimSubclass(
            source=base.source,
            provider_id=base.provider_id,
            account_id=base.account_id,
            entity_id=base.entity_id,
            environment=base.environment,
            provider_environment=base.provider_environment,
            instrument_version=base.instrument_version,
            observed_at=base.observed_at,
            expires_at=base.expires_at,
            supported_order_types=base.supported_order_types,
            time_in_force=base.time_in_force,
            permission_scopes=base.permission_scopes,
            position_mode=base.position_mode,
            native_protection=base.native_protection,
            rate_limit_policy_id=base.rate_limit_policy_id,
            data_entitlements=base.data_entitlements,
            evidence_ref=base.evidence_ref,
        )
        callbacks = []
        with self.assertRaisesRegex(TypeError, "CapabilityClaim"):
            derive_capability_snapshot(
                snapshot_id=str(uuid4()),
                claims=(impostor,),
                observed_at=NOW,
                evidence_verifier=lambda candidate: (
                    callbacks.append(candidate) or EvidenceVerification(valid=True)
                ),
            )
        self.assertEqual(callbacks, [])

    def test_registry_rejects_snapshot_subclass_even_when_fields_match(self):
        base = snapshot(provider_environment="TESTNET")

        class SnapshotSubclass(CapabilitySnapshot):
            def __post_init__(self, *args, **kwargs):
                pass

        impostor = SnapshotSubclass(
            snapshot_id=base.snapshot_id,
            provider_id=base.provider_id,
            account_id=base.account_id,
            entity_id=base.entity_id,
            environment=base.environment,
            provider_environment=base.provider_environment,
            instrument_version=base.instrument_version,
            observed_at=base.observed_at,
            expires_at=base.expires_at,
            supported_order_types=base.supported_order_types,
            time_in_force=base.time_in_force,
            permission_scopes=base.permission_scopes,
            position_mode=base.position_mode,
            native_protection=base.native_protection,
            rate_limit_policy_id=base.rate_limit_policy_id,
            data_entitlements=base.data_entitlements,
            evidence=base.evidence,
            status=base.status,
            sources=base.sources,
        )
        registry = CapabilityRegistry()
        with self.assertRaisesRegex(TypeError, "exact CapabilitySnapshot"):
            registry.add(impostor)

    def test_artifact_verifier_rejects_claim_subclass_before_store_access(self):
        base = claim("DOCUMENTED")

        class ClaimSubclass(CapabilityClaim):
            pass

        impostor = ClaimSubclass(
            source=base.source,
            provider_id=base.provider_id,
            account_id=base.account_id,
            entity_id=base.entity_id,
            environment=base.environment,
            provider_environment=base.provider_environment,
            instrument_version=base.instrument_version,
            observed_at=base.observed_at,
            expires_at=base.expires_at,
            supported_order_types=base.supported_order_types,
            time_in_force=base.time_in_force,
            permission_scopes=base.permission_scopes,
            position_mode=base.position_mode,
            native_protection=base.native_protection,
            rate_limit_policy_id=base.rate_limit_policy_id,
            data_entitlements=base.data_entitlements,
            evidence_ref=base.evidence_ref,
        )

        class Store:
            def load_manifest(self, _artifact_id):
                raise AssertionError("store must not be reached")

            def read_bytes(self, _artifact_id):
                raise AssertionError("store must not be reached")

        verifier = artifact_store_evidence_verifier(Store())
        with self.assertRaisesRegex(TypeError, "exact CapabilityClaim"):
            verifier(impostor)


if __name__ == "__main__":
    unittest.main()
