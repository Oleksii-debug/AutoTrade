from dataclasses import replace
from datetime import datetime, timedelta, timezone
import tempfile
import unittest

from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp.capabilities import (
    ArtifactCapabilityEvidenceVerifier,
    CapabilityClaim,
    CapabilityError,
    CapabilityEvidenceCheck,
    CapabilityRegistry,
    derive_capability_snapshot,
)


NOW = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)
SNAPSHOT_1 = "11111111-1111-4111-8111-111111111111"
SNAPSHOT_2 = "22222222-2222-4222-8222-222222222222"


def claim(
    source: str,
    *,
    order_types=("LIMIT", "MARKET"),
    tif=("DAY", "GTC"),
    scopes=("ORDER.READ", "ORDER.WRITE"),
    position_mode="NET",
    rate_policy="sim-v1",
    observed_at=NOW - timedelta(minutes=1),
    expires_at=NOW + timedelta(minutes=10),
    instrument_version="instrument-v1",
) -> CapabilityClaim:
    return CapabilityClaim(
        source=source,
        provider_id="simulated",
        account_id="paper-account",
        entity_id="entity-1",
        environment="PAPER",
        instrument_version=instrument_version,
        observed_at=observed_at,
        expires_at=expires_at,
        supported_order_types=frozenset(order_types),
        time_in_force=frozenset(tif),
        permission_scopes=frozenset(scopes),
        position_mode=position_mode,
        native_protection=frozenset({"STOP_LOSS", "TAKE_PROFIT"}),
        rate_limit_policy_id=rate_policy,
        data_entitlements=frozenset({"QUOTE", "TRADE"}),
        evidence_ref={
            "artifact_id": f"33333333-3333-4333-8333-33333333333{len(source) % 10}",
            "sha256": "sha256:" + "a" * 64,
            "observed_at": "2026-09-24T15:59:00Z",
        },
    )


def complete_claims(**overrides):
    return tuple(
        claim(source, **overrides)
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )


class _UnitEvidenceVerifier:
    def verify(self, claim):
        return CapabilityEvidenceCheck("VALID", "non-artifact unit fixture")


UNIT_EVIDENCE_VERIFIER = _UnitEvidenceVerifier()


def derive(**kwargs):
    return derive_capability_snapshot(
        evidence_verifier=UNIT_EVIDENCE_VERIFIER,
        **kwargs,
    )


_ARTIFACT_IDS = {
    "DOCUMENTED": "40000000-0000-4000-8000-000000000001",
    "API": "40000000-0000-4000-8000-000000000002",
    "ACCOUNT": "40000000-0000-4000-8000-000000000003",
    "INSTRUMENT": "40000000-0000-4000-8000-000000000004",
}
_PRODUCERS = {
    "DOCUMENTED": "PROVIDER_DOCUMENTATION",
    "API": "PROVIDER_API",
    "ACCOUNT": "ACCOUNT_CAPABILITY",
    "INSTRUMENT": "INSTRUMENT_CAPABILITY",
}


def publish_evidenced_claim(store, source, *, metadata_overrides=None):
    base = claim(source)
    observed = base.observed_at.isoformat().replace("+00:00", "Z")
    metadata = {
        "capability_evidence_version": 1,
        "evidence_type": "CAPABILITY_CLAIM",
        "producer_kind": _PRODUCERS[source],
        "capability_source": source,
        "provider_id": base.provider_id,
        "account_id": base.account_id,
        "entity_id": base.entity_id,
        "environment": base.environment,
        "instrument_version": base.instrument_version,
        "observed_at": observed,
    }
    metadata.update(metadata_overrides or {})
    manifest = store.publish_bytes(
        artifact_id=_ARTIFACT_IDS[source],
        data=("capability-evidence:" + source).encode("utf-8"),
        media_type="application/vnd.autotrade.capability-evidence+json",
        rights={"storage": True, "export": False},
        metadata=metadata,
    )
    return replace(
        base,
        evidence_ref={
            "artifact_id": manifest["artifact_id"],
            "sha256": manifest["sha256"],
            "observed_at": observed,
        },
    )


class CapabilityFoundationTests(unittest.TestCase):
    def test_verified_snapshot_is_exact_intersection(self):
        claims = (
            claim("DOCUMENTED", order_types=("LIMIT", "MARKET"), tif=("DAY", "GTC")),
            claim("API", order_types=("LIMIT", "MARKET"), tif=("DAY",)),
            claim("ACCOUNT", order_types=("LIMIT",), tif=("DAY",)),
            claim("INSTRUMENT", order_types=("LIMIT", "STOP"), tif=("DAY", "IOC")),
        )
        snapshot = derive(
            snapshot_id=SNAPSHOT_1,
            claims=claims,
            observed_at=NOW,
        )
        self.assertEqual(snapshot.status, "VERIFIED")
        self.assertEqual(snapshot.supported_order_types, frozenset({"LIMIT"}))
        self.assertEqual(snapshot.time_in_force, frozenset({"DAY"}))
        self.assertTrue(
            snapshot.admits(
                at=NOW,
                order_type="LIMIT",
                time_in_force="DAY",
                permission_scope="ORDER.WRITE",
            )
        )
        self.assertFalse(
            snapshot.admits(
                at=NOW,
                order_type="MARKET",
                time_in_force="DAY",
                permission_scope="ORDER.WRITE",
            )
        )

    def test_missing_required_source_is_unknown(self):
        snapshot = derive(
            snapshot_id=SNAPSHOT_1,
            claims=(
                claim("DOCUMENTED"),
                claim("API"),
                claim("ACCOUNT"),
            ),
            observed_at=NOW,
        )
        self.assertEqual(snapshot.status, "UNKNOWN")
        self.assertFalse(
            snapshot.admits(
                at=NOW,
                order_type="LIMIT",
                time_in_force="DAY",
                permission_scope="ORDER.WRITE",
            )
        )

    def test_expired_evidence_fails_closed(self):
        claims = list(complete_claims())
        claims[-1] = claim(
            "INSTRUMENT",
            observed_at=NOW - timedelta(minutes=20),
            expires_at=NOW - timedelta(seconds=1),
        )
        snapshot = derive(
            snapshot_id=SNAPSHOT_1,
            claims=claims,
            observed_at=NOW,
        )
        self.assertEqual(snapshot.status, "EXPIRED")

    def test_conflicting_order_filter_and_position_mode_fail_closed(self):
        order_conflict = (
            claim("DOCUMENTED", order_types=("LIMIT",)),
            claim("API", order_types=("MARKET",)),
            claim("ACCOUNT", order_types=("LIMIT", "MARKET")),
            claim("INSTRUMENT", order_types=("LIMIT", "MARKET")),
        )
        first = derive(
            snapshot_id=SNAPSHOT_1,
            claims=order_conflict,
            observed_at=NOW,
        )
        self.assertEqual(first.status, "CONFLICTED")

        mode_conflict = list(complete_claims())
        mode_conflict[-1] = claim("INSTRUMENT", position_mode="HEDGE")
        second = derive(
            snapshot_id=SNAPSHOT_2,
            claims=mode_conflict,
            observed_at=NOW,
        )
        self.assertEqual(second.status, "CONFLICTED")

    def test_identity_mismatch_is_rejected_not_intersected(self):
        claims = list(complete_claims())
        claims[-1] = claim("INSTRUMENT", instrument_version="instrument-v2")
        with self.assertRaisesRegex(CapabilityError, "different identities"):
            derive(
                snapshot_id=SNAPSHOT_1,
                claims=claims,
                observed_at=NOW,
            )

    def test_registry_uses_latest_snapshot_and_expiry(self):
        registry = CapabilityRegistry()
        first = derive(
            snapshot_id=SNAPSHOT_1,
            claims=complete_claims(expires_at=NOW + timedelta(minutes=2)),
            observed_at=NOW,
        )
        second_at = NOW + timedelta(minutes=1)
        second = derive(
            snapshot_id=SNAPSHOT_2,
            claims=complete_claims(
                observed_at=NOW,
                expires_at=NOW + timedelta(minutes=4),
            ),
            observed_at=second_at,
        )
        registry.add(first)
        registry.add(second)

        self.assertEqual(
            registry.require_verified(
                provider_id="simulated",
                account_id="paper-account",
                entity_id="entity-1",
                environment="paper",
                instrument_version="instrument-v1",
                at=NOW + timedelta(seconds=30),
            ).snapshot_id,
            SNAPSHOT_1,
        )
        self.assertEqual(
            registry.require_verified(
                provider_id="simulated",
                account_id="paper-account",
                entity_id="entity-1",
                environment="PAPER",
                instrument_version="instrument-v1",
                at=NOW + timedelta(minutes=3),
            ).snapshot_id,
            SNAPSHOT_2,
        )
        with self.assertRaisesRegex(CapabilityError, "expired"):
            registry.require_verified(
                provider_id="simulated",
                account_id="paper-account",
                entity_id="entity-1",
                environment="PAPER",
                instrument_version="instrument-v1",
                at=NOW + timedelta(minutes=5),
            )

    def test_snapshot_id_is_immutable(self):
        snapshot = derive(
            snapshot_id=SNAPSHOT_1,
            claims=complete_claims(),
            observed_at=NOW,
        )
        registry = CapabilityRegistry()
        registry.add(snapshot)
        registry.add(snapshot)

        with self.assertRaisesRegex(CapabilityError, "snapshot_id"):
            registry.add(replace(snapshot, status="UNKNOWN"))

    def test_expired_historical_claim_does_not_poison_newer_live_claim(self):
        claims = list(complete_claims())
        claims.append(
            claim(
                "ACCOUNT",
                observed_at=NOW - timedelta(minutes=20),
                expires_at=NOW - timedelta(minutes=10),
            )
        )
        snapshot = derive(
            snapshot_id=SNAPSHOT_1,
            claims=claims,
            observed_at=NOW,
        )
        self.assertEqual(snapshot.status, "VERIFIED")
        self.assertEqual(
            snapshot.sources,
            frozenset({"DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT"}),
        )

    def test_overlapping_live_claims_from_same_source_are_intersected(self):
        claims = list(complete_claims())
        claims.append(claim("ACCOUNT", order_types=("LIMIT",)))
        snapshot = derive(
            snapshot_id=SNAPSHOT_1,
            claims=claims,
            observed_at=NOW,
        )
        self.assertEqual(snapshot.status, "VERIFIED")
        self.assertEqual(snapshot.supported_order_types, frozenset({"LIMIT"}))

    def test_conflicting_live_refresh_fails_closed(self):
        claims = [
            claim("DOCUMENTED", order_types=("LIMIT", "MARKET")),
            claim("API", order_types=("LIMIT", "MARKET")),
            claim("ACCOUNT", order_types=("LIMIT",)),
            claim("ACCOUNT", order_types=("MARKET",)),
            claim("INSTRUMENT", order_types=("LIMIT", "MARKET")),
        ]
        snapshot = derive(
            snapshot_id=SNAPSHOT_1,
            claims=claims,
            observed_at=NOW,
        )
        self.assertEqual(snapshot.status, "CONFLICTED")

    def test_collection_fields_reject_string_values(self):
        original = claim("API")
        with self.assertRaisesRegex(CapabilityError, "must be a collection"):
            CapabilityClaim(
                source=original.source,
                provider_id=original.provider_id,
                account_id=original.account_id,
                entity_id=original.entity_id,
                environment=original.environment,
                instrument_version=original.instrument_version,
                observed_at=original.observed_at,
                expires_at=original.expires_at,
                supported_order_types="LIMIT",
                time_in_force=original.time_in_force,
                permission_scopes=original.permission_scopes,
                position_mode=original.position_mode,
                native_protection=original.native_protection,
                rate_limit_policy_id=original.rate_limit_policy_id,
                data_entitlements=original.data_entitlements,
                evidence_ref=original.evidence_ref,
            )

    def test_future_dated_evidence_is_conflicted_not_expired(self):
        claims = list(complete_claims())
        claims[-1] = claim(
            "INSTRUMENT",
            observed_at=NOW + timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=10),
        )
        snapshot = derive(
            snapshot_id=SNAPSHOT_1,
            claims=claims,
            observed_at=NOW,
        )
        self.assertEqual(snapshot.status, "CONFLICTED")

    def test_evidence_reference_is_strict_and_immutable(self):
        snapshot = derive(
            snapshot_id=SNAPSHOT_1,
            claims=complete_claims(),
            observed_at=NOW,
        )
        with self.assertRaises(TypeError):
            snapshot.evidence[0]["sha256"] = "sha256:" + "b" * 64
        original = claim("API")
        with self.assertRaisesRegex(CapabilityError, "sha256"):
            CapabilityClaim(
                source=original.source,
                provider_id=original.provider_id,
                account_id=original.account_id,
                entity_id=original.entity_id,
                environment=original.environment,
                instrument_version=original.instrument_version,
                observed_at=original.observed_at,
                expires_at=original.expires_at,
                supported_order_types=original.supported_order_types,
                time_in_force=original.time_in_force,
                permission_scopes=original.permission_scopes,
                position_mode=original.position_mode,
                native_protection=original.native_protection,
                rate_limit_policy_id=original.rate_limit_policy_id,
                data_entitlements=original.data_entitlements,
                evidence_ref={
                    "artifact_id": "33333333-3333-4333-8333-333333333333",
                    "sha256": "bad",
                    "observed_at": "2026-09-24T15:59:00Z",
                },
            )

    def test_contract_projection_contains_current_fail_closed_status(self):
        snapshot = derive(
            snapshot_id=SNAPSHOT_1,
            claims=complete_claims(),
            observed_at=NOW,
        )
        payload = snapshot.to_contract_dict()
        self.assertEqual(payload["status"], "VERIFIED")
        self.assertEqual(payload["environment"], "PAPER")
        self.assertEqual(payload["supported_order_types"], ["LIMIT", "MARKET"])
        self.assertEqual(payload["observed_at"], "2026-09-24T16:00:00Z")
        self.assertEqual(len(payload["evidence"]), 4)


    def test_verified_requires_an_evidence_verifier(self):
        snapshot = derive_capability_snapshot(
            snapshot_id=SNAPSHOT_1,
            claims=complete_claims(),
            observed_at=NOW,
        )
        self.assertEqual(snapshot.status, "UNKNOWN")
        self.assertFalse(
            snapshot.admits(
                at=NOW,
                order_type="LIMIT",
                time_in_force="DAY",
                permission_scope="ORDER.WRITE",
            )
        )

    def test_syntactically_valid_but_missing_artifacts_cannot_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            verifier = ArtifactCapabilityEvidenceVerifier(ArtifactStore(directory))
            snapshot = derive_capability_snapshot(
                snapshot_id=SNAPSHOT_1,
                claims=complete_claims(),
                observed_at=NOW,
                evidence_verifier=verifier,
            )
        self.assertEqual(snapshot.status, "UNKNOWN")

    def test_artifact_digest_mismatch_is_conflicted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            claims = [publish_evidenced_claim(store, source) for source in (
                "DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT"
            )]
            bad = claims[-1]
            claims[-1] = replace(
                bad,
                evidence_ref={
                    **dict(bad.evidence_ref),
                    "sha256": "sha256:" + "f" * 64,
                },
            )
            snapshot = derive_capability_snapshot(
                snapshot_id=SNAPSHOT_1,
                claims=claims,
                observed_at=NOW,
                evidence_verifier=ArtifactCapabilityEvidenceVerifier(store),
            )
        self.assertEqual(snapshot.status, "CONFLICTED")

    def test_wrong_identity_or_producer_artifact_is_conflicted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            claims = [
                publish_evidenced_claim(store, "DOCUMENTED"),
                publish_evidenced_claim(store, "API"),
                publish_evidenced_claim(
                    store,
                    "ACCOUNT",
                    metadata_overrides={"account_id": "other-account"},
                ),
                publish_evidenced_claim(store, "INSTRUMENT"),
            ]
            snapshot = derive_capability_snapshot(
                snapshot_id=SNAPSHOT_1,
                claims=claims,
                observed_at=NOW,
                evidence_verifier=ArtifactCapabilityEvidenceVerifier(store),
            )
        self.assertEqual(snapshot.status, "CONFLICTED")

    def test_exact_immutable_artifacts_all_sources_are_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            claims = tuple(
                publish_evidenced_claim(store, source)
                for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
            )
            snapshot = derive_capability_snapshot(
                snapshot_id=SNAPSHOT_1,
                claims=claims,
                observed_at=NOW,
                evidence_verifier=ArtifactCapabilityEvidenceVerifier(store),
            )
        self.assertEqual(snapshot.status, "VERIFIED")
        self.assertEqual(len(snapshot.evidence), 4)


if __name__ == "__main__":
    unittest.main()
