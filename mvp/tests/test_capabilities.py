from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    CapabilityError,
    CapabilityRegistry,
    EvidenceVerification,
    derive_capability_snapshot as _derive_capability_snapshot,
)


NOW = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)
SNAPSHOT_1 = "11111111-1111-4111-8111-111111111111"
SNAPSHOT_2 = "22222222-2222-4222-8222-222222222222"


def _trusted_test_evidence(_claim: CapabilityClaim) -> EvidenceVerification:
    return EvidenceVerification(valid=True)


def derive_capability_snapshot(**kwargs):
    kwargs.setdefault("evidence_verifier", _trusted_test_evidence)
    return _derive_capability_snapshot(**kwargs)


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


class CapabilityFoundationTests(unittest.TestCase):
    def test_self_asserted_evidence_without_verifier_is_unknown(self):
        snapshot = _derive_capability_snapshot(
            snapshot_id=SNAPSHOT_1,
            claims=complete_claims(),
            observed_at=NOW,
        )
        self.assertEqual(snapshot.status, "UNKNOWN")
        self.assertEqual(snapshot.sources, frozenset())

    def test_verified_snapshot_is_exact_intersection(self):
        claims = (
            claim("DOCUMENTED", order_types=("LIMIT", "MARKET"), tif=("DAY", "GTC")),
            claim("API", order_types=("LIMIT", "MARKET"), tif=("DAY",)),
            claim("ACCOUNT", order_types=("LIMIT",), tif=("DAY",)),
            claim("INSTRUMENT", order_types=("LIMIT", "STOP"), tif=("DAY", "IOC")),
        )
        snapshot = derive_capability_snapshot(
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
        snapshot = derive_capability_snapshot(
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
        snapshot = derive_capability_snapshot(
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
        first = derive_capability_snapshot(
            snapshot_id=SNAPSHOT_1,
            claims=order_conflict,
            observed_at=NOW,
        )
        self.assertEqual(first.status, "CONFLICTED")

        mode_conflict = list(complete_claims())
        mode_conflict[-1] = claim("INSTRUMENT", position_mode="HEDGE")
        second = derive_capability_snapshot(
            snapshot_id=SNAPSHOT_2,
            claims=mode_conflict,
            observed_at=NOW,
        )
        self.assertEqual(second.status, "CONFLICTED")

    def test_identity_mismatch_is_rejected_not_intersected(self):
        claims = list(complete_claims())
        claims[-1] = claim("INSTRUMENT", instrument_version="instrument-v2")
        with self.assertRaisesRegex(CapabilityError, "different identities"):
            derive_capability_snapshot(
                snapshot_id=SNAPSHOT_1,
                claims=claims,
                observed_at=NOW,
            )

    def test_registry_uses_latest_snapshot_and_expiry(self):
        registry = CapabilityRegistry()
        first = derive_capability_snapshot(
            snapshot_id=SNAPSHOT_1,
            claims=complete_claims(expires_at=NOW + timedelta(minutes=2)),
            observed_at=NOW,
        )
        second_at = NOW + timedelta(minutes=1)
        second = derive_capability_snapshot(
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
        snapshot = derive_capability_snapshot(
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
        snapshot = derive_capability_snapshot(
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
        snapshot = derive_capability_snapshot(
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
        snapshot = derive_capability_snapshot(
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
        snapshot = derive_capability_snapshot(
            snapshot_id=SNAPSHOT_1,
            claims=claims,
            observed_at=NOW,
        )
        self.assertEqual(snapshot.status, "CONFLICTED")

    def test_evidence_reference_is_strict_and_immutable(self):
        snapshot = derive_capability_snapshot(
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
        snapshot = derive_capability_snapshot(
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


if __name__ == "__main__":
    unittest.main()
