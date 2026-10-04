from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from mvp.autotrade_mvp.capabilities import (
    CapabilitySnapshot,
    CapabilityClaim,
    CapabilityError,
    CapabilityRegistry,
    EvidenceVerification,
    derive_capability_snapshot as _derive_capability_snapshot,
)

from mvp.tests.capability_test_support import fresh_test_admission


NOW = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)
SNAPSHOT_1 = "11111111-1111-4111-8111-111111111111"
SNAPSHOT_2 = "22222222-2222-4222-8222-222222222222"


def _trusted_test_evidence(_claim: CapabilityClaim) -> EvidenceVerification:
    return EvidenceVerification(valid=True)


def derive_capability_snapshot(**kwargs):
    kwargs.setdefault("evidence_verifier", _trusted_test_evidence)
    return fresh_test_admission(_derive_capability_snapshot(**kwargs))


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
    provider_id="simulated",
    account_id="paper-account",
    entity_id="entity-1",
    environment="PAPER",
    provider_environment=None,
) -> CapabilityClaim:
    return CapabilityClaim(
        source=source,
        provider_id=provider_id,
        account_id=account_id,
        entity_id=entity_id,
        environment=environment,
        provider_environment=provider_environment,
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
            "observed_at": observed_at.astimezone(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
        },
    )


def complete_claims(**overrides):
    return tuple(
        claim(source, **overrides)
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )


class CapabilityFoundationTests(unittest.TestCase):
    def test_direct_verified_snapshot_cannot_bypass_evidence_derivation(self):
        with self.assertRaisesRegex(
            CapabilityError,
            "canonical evidence derivation",
        ):
            CapabilitySnapshot(
                snapshot_id=SNAPSHOT_1,
                provider_id="provider",
                account_id="account",
                entity_id="entity",
                environment="PAPER",
                instrument_version="instrument-v1",
                observed_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
                supported_order_types=frozenset({"LIMIT"}),
                time_in_force=frozenset({"DAY"}),
                permission_scopes=frozenset({"ORDER.WRITE"}),
                position_mode="NET",
                native_protection=frozenset(),
                rate_limit_policy_id="rate-v1",
                data_entitlements=frozenset(),
                evidence=(),
                status="VERIFIED",
                sources=frozenset(),
            )

    def test_capability_evidence_identity_aliases_fail_closed(self):
        base = claim("API")
        canonical_time = base.observed_at.isoformat().replace("+00:00", "Z")
        canonical = {
            "artifact_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "sha256": "sha256:" + ("a" * 64),
            "observed_at": canonical_time,
        }
        accepted = replace(base, evidence_ref=canonical)
        self.assertEqual(
            accepted.evidence_ref["artifact_id"],
            canonical["artifact_id"],
        )

        invalid_refs = (
            {**canonical, "artifact_id": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"},
            {**canonical, "artifact_id": canonical["artifact_id"].replace("-", "")},
            {**canonical, "artifact_id": " " + canonical["artifact_id"]},
            {**canonical, "sha256": canonical["sha256"] + " "},
            {**canonical, "observed_at": "2026-09-24T15:59:00.000000Z"},
            {**canonical, "observed_at": canonical_time + " "},
        )
        for evidence_ref in invalid_refs:
            with self.subTest(evidence_ref=evidence_ref), self.assertRaisesRegex(
                CapabilityError,
                "canonical",
            ):
                replace(base, evidence_ref=evidence_ref)

    def test_self_asserted_evidence_without_verifier_is_unknown(self):
        snapshot = _derive_capability_snapshot(
            snapshot_id=SNAPSHOT_1,
            claims=complete_claims(),
            observed_at=NOW,
        )
        self.assertEqual(snapshot.status, "UNKNOWN")
        self.assertEqual(snapshot.sources, frozenset())

    def test_unverified_live_refresh_cannot_preserve_older_source_authority(self):
        older_account = claim(
            "ACCOUNT",
            scopes=("ORDER.READ", "ORDER.WRITE"),
            observed_at=NOW - timedelta(minutes=5),
            expires_at=NOW + timedelta(minutes=10),
        )
        newer_account = claim(
            "ACCOUNT",
            scopes=("ORDER.READ",),
            observed_at=NOW - timedelta(seconds=30),
            expires_at=NOW + timedelta(minutes=10),
        )
        claims = (
            claim("DOCUMENTED"),
            claim("API"),
            older_account,
            newer_account,
            claim("INSTRUMENT"),
        )

        def verifier(item):
            if item is newer_account:
                return EvidenceVerification(
                    valid=False,
                    reason="newer capability evidence is unavailable",
                )
            return EvidenceVerification(valid=True)

        snapshot = _derive_capability_snapshot(
            snapshot_id=SNAPSHOT_1,
            claims=claims,
            observed_at=NOW,
            evidence_verifier=verifier,
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

    def test_bybit_claim_never_infers_testnet_or_demo_from_paper(self):
        with self.assertRaisesRegex(CapabilityError, "explicit provider_environment"):
            claim("API", provider_id="BYBIT")
        with self.assertRaisesRegex(CapabilityError, "does not match runtime"):
            claim("API", provider_id="BYBIT", environment="LIVE", provider_environment="TESTNET")

    def test_mixed_provider_environments_are_distinct_claim_identities(self):
        claims = list(complete_claims(provider_id="BYBIT", provider_environment="TESTNET"))
        claims[-1] = claim("INSTRUMENT", provider_id="BYBIT", provider_environment="DEMO")
        with self.assertRaisesRegex(CapabilityError, "different identities"):
            derive_capability_snapshot(snapshot_id=SNAPSHOT_1, claims=claims, observed_at=NOW)

    def test_bybit_testnet_and_demo_coexist_without_cross_resolution(self):
        registry = CapabilityRegistry()
        testnet = derive_capability_snapshot(
            snapshot_id=SNAPSHOT_1,
            claims=complete_claims(provider_id="BYBIT", provider_environment="TESTNET"),
            observed_at=NOW,
        )
        demo = derive_capability_snapshot(
            snapshot_id=SNAPSHOT_2,
            claims=complete_claims(provider_id="BYBIT", provider_environment="DEMO"),
            observed_at=NOW,
        )
        registry.add(testnet)
        registry.add(demo)
        common = dict(
            provider_id="BYBIT", account_id="paper-account", entity_id="entity-1",
            environment="PAPER", instrument_version="instrument-v1", at=NOW,
        )
        self.assertEqual(registry.require_verified(**common, provider_environment="TESTNET").snapshot_id, SNAPSHOT_1)
        self.assertEqual(registry.require_verified(**common, provider_environment="DEMO").snapshot_id, SNAPSHOT_2)
        with self.assertRaisesRegex(CapabilityError, "explicit provider_environment"):
            registry.latest(**common)

    def test_contract_projection_preserves_provider_environment(self):
        snapshot = derive_capability_snapshot(
            snapshot_id=SNAPSHOT_1,
            claims=complete_claims(provider_id="BYBIT", provider_environment="TESTNET"),
            observed_at=NOW,
        )
        self.assertEqual(snapshot.provider_environment, "TESTNET")
        self.assertEqual(snapshot.to_contract_dict()["provider_environment"], "TESTNET")

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


    def test_snapshot_constructor_fail_closed_on_invalid_identity_and_time(self):
        snapshot = derive_capability_snapshot(
            snapshot_id=SNAPSHOT_1,
            claims=complete_claims(),
            observed_at=NOW,
        )
        with self.assertRaisesRegex(CapabilityError, "provider_id is required"):
            replace(snapshot, provider_id="   ")
        with self.assertRaisesRegex(CapabilityError, "environment is unsupported"):
            replace(snapshot, environment="SANDBOX")
        with self.assertRaisesRegex(CapabilityError, "timezone-aware"):
            replace(snapshot, observed_at=NOW.replace(tzinfo=None))
        with self.assertRaisesRegex(CapabilityError, "cannot be before"):
            replace(snapshot, expires_at=NOW - timedelta(seconds=1))

    def test_snapshot_constructor_normalizes_collections_and_rejects_bad_sources(self):
        snapshot = derive_capability_snapshot(
            snapshot_id=SNAPSHOT_1,
            claims=complete_claims(),
            observed_at=NOW,
        )
        unverified = replace(snapshot, status="UNKNOWN")
        rebuilt = replace(
            unverified,
            supported_order_types=[" LIMIT ", "MARKET"],
            sources={" documented ", "api", "ACCOUNT", "instrument"},
        )
        self.assertEqual(rebuilt.supported_order_types, frozenset({"LIMIT", "MARKET"}))
        self.assertEqual(
            rebuilt.sources,
            frozenset({"DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT"}),
        )
        with self.assertRaisesRegex(CapabilityError, "unsupported source"):
            replace(unverified, sources={"DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT", "MODEL"})
        with self.assertRaisesRegex(CapabilityError, "must be a collection"):
            replace(unverified, permission_scopes="ORDER.WRITE")

    def test_public_derivation_cannot_mint_fresh_admission_from_caller_verifier(self):
        snapshot = _derive_capability_snapshot(
            snapshot_id=SNAPSHOT_1,
            claims=complete_claims(),
            observed_at=NOW,
            evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
        )
        self.assertEqual(snapshot.status, "VERIFIED")
        self.assertFalse(
            snapshot.admits(
                at=NOW,
                order_type="LIMIT",
                time_in_force="DAY",
                permission_scope="ORDER.WRITE",
            )
        )
        registry = CapabilityRegistry()
        registry.add(snapshot)
        with self.assertRaisesRegex(CapabilityError, "fresh admission"):
            registry.require_verified(
                provider_id="simulated",
                account_id="paper-account",
                entity_id="entity-1",
                environment="PAPER",
                instrument_version="instrument-v1",
                at=NOW,
            )

    def test_registry_rejects_snapshot_subclass_authority(self):
        class ForgedSnapshot(CapabilitySnapshot):
            pass

        canonical = derive_capability_snapshot(
            snapshot_id=SNAPSHOT_1,
            claims=complete_claims(),
            observed_at=NOW,
        )
        forged = ForgedSnapshot(
            snapshot_id=SNAPSHOT_2,
            provider_id=canonical.provider_id,
            account_id=canonical.account_id,
            entity_id=canonical.entity_id,
            environment=canonical.environment,
            instrument_version=canonical.instrument_version,
            observed_at=canonical.observed_at,
            expires_at=canonical.expires_at,
            supported_order_types=canonical.supported_order_types,
            time_in_force=canonical.time_in_force,
            permission_scopes=canonical.permission_scopes,
            position_mode=canonical.position_mode,
            native_protection=canonical.native_protection,
            rate_limit_policy_id=canonical.rate_limit_policy_id,
            data_entitlements=canonical.data_entitlements,
            evidence=canonical.evidence,
            status="UNKNOWN",
            sources=canonical.sources,
        )
        registry = CapabilityRegistry()
        with self.assertRaisesRegex(TypeError, "exact CapabilitySnapshot"):
            registry.add(forged)

    def test_claim_graph_is_detached_before_verifier_can_mutate_callers(self):
        originals = list(complete_claims())
        original_by_source = {item.source: item for item in originals}
        verified_claims = []

        def hostile_verifier(detached):
            verified_claims.append(detached)
            original = original_by_source[detached.source]
            object.__setattr__(original, "provider_id", "mutated-provider")
            object.__setattr__(
                original,
                "supported_order_types",
                frozenset({"MARKET"}),
            )
            object.__setattr__(
                original,
                "permission_scopes",
                frozenset({"ORDER.READ"}),
            )
            object.__setattr__(
                original,
                "expires_at",
                NOW - timedelta(seconds=1),
            )
            object.__setattr__(
                original,
                "evidence_ref",
                {
                    "artifact_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "sha256": "sha256:" + "f" * 64,
                    "observed_at": "2026-09-24T15:59:00Z",
                },
            )
            return EvidenceVerification(valid=True)

        snapshot = _derive_capability_snapshot(
            snapshot_id=SNAPSHOT_1,
            claims=tuple(originals),
            observed_at=NOW,
            evidence_verifier=hostile_verifier,
        )

        self.assertEqual(snapshot.status, "VERIFIED")
        self.assertEqual(snapshot.provider_id, "simulated")
        self.assertEqual(
            snapshot.supported_order_types,
            frozenset({"LIMIT", "MARKET"}),
        )
        self.assertIn("ORDER.WRITE", snapshot.permission_scopes)
        self.assertEqual(snapshot.expires_at, NOW + timedelta(minutes=10))
        self.assertEqual(
            {item["sha256"] for item in snapshot.evidence},
            {"sha256:" + "a" * 64},
        )
        self.assertEqual(len(verified_claims), 4)
        self.assertTrue(
            all(
                detached is not original_by_source[detached.source]
                for detached in verified_claims
            )
        )

    def test_verifier_cannot_mutate_detached_snapshot_material(self):
        originals = tuple(complete_claims())

        def hostile_verifier(item):
            object.__setattr__(item, "provider_id", "mutated-provider")
            object.__setattr__(item, "supported_order_types", frozenset({"MARKET"}))
            object.__setattr__(item, "permission_scopes", frozenset({"ORDER.READ"}))
            object.__setattr__(item, "expires_at", NOW - timedelta(seconds=1))
            return EvidenceVerification(valid=True)

        snapshot = _derive_capability_snapshot(
            snapshot_id=SNAPSHOT_1,
            claims=originals,
            observed_at=NOW,
            evidence_verifier=hostile_verifier,
        )

        self.assertEqual(snapshot.status, "VERIFIED")
        self.assertEqual(snapshot.provider_id, "simulated")
        self.assertEqual(snapshot.supported_order_types, frozenset({"LIMIT", "MARKET"}))
        self.assertIn("ORDER.WRITE", snapshot.permission_scopes)
        self.assertEqual(snapshot.expires_at, NOW + timedelta(minutes=10))

    def test_evidence_verification_subclass_is_not_authority(self):
        class ForgedVerification(EvidenceVerification):
            pass

        with self.assertRaisesRegex(TypeError, "exact EvidenceVerification"):
            _derive_capability_snapshot(
                snapshot_id=SNAPSHOT_1,
                claims=complete_claims(),
                observed_at=NOW,
                evidence_verifier=lambda _claim: ForgedVerification(valid=True),
            )

    def test_capability_claim_subclass_is_rejected_before_verifier(self):
        class ForgedClaim(CapabilityClaim):
            pass

        base = claim("DOCUMENTED")
        forged = ForgedClaim(
            source=base.source,
            provider_id=base.provider_id,
            account_id=base.account_id,
            entity_id=base.entity_id,
            environment=base.environment,
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
        calls = []

        def verifier(item):
            calls.append(item)
            return EvidenceVerification(valid=True)

        with self.assertRaisesRegex(TypeError, "exact CapabilityClaim"):
            _derive_capability_snapshot(
                snapshot_id=SNAPSHOT_1,
                claims=(forged,),
                observed_at=NOW,
                evidence_verifier=verifier,
            )
        self.assertEqual(calls, [])

    def test_old_evidence_cannot_be_relabelled_as_fresh_claim(self):
        original = claim(
            "ACCOUNT",
            observed_at=NOW - timedelta(minutes=20),
            expires_at=NOW + timedelta(minutes=10),
        )
        with self.assertRaisesRegex(
            CapabilityError, "must match claim observed_at"
        ):
            CapabilityClaim(
                source=original.source,
                provider_id=original.provider_id,
                account_id=original.account_id,
                entity_id=original.entity_id,
                environment=original.environment,
                instrument_version=original.instrument_version,
                observed_at=NOW,
                expires_at=NOW + timedelta(minutes=10),
                supported_order_types=original.supported_order_types,
                time_in_force=original.time_in_force,
                permission_scopes=original.permission_scopes,
                position_mode=original.position_mode,
                native_protection=original.native_protection,
                rate_limit_policy_id=original.rate_limit_policy_id,
                data_entitlements=original.data_entitlements,
                evidence_ref=original.evidence_ref,
            )


if __name__ == "__main__":
    unittest.main()
