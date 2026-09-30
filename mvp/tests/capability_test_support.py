"""Test-only issuance helpers for capability unit/contract fixtures.

Production code must never import this module. It exists so tests can exercise
post-issuance consumers while the public pure derivation path remains
non-admitting by construction.
"""

from mvp.autotrade_mvp.capabilities import (
    CapabilitySnapshot,
    CapabilityRegistry,
    _DERIVED_SNAPSHOT_TOKEN,
    _FRESH_ADMISSION_TOKEN,
    _capability_content_sha256,
)


def fresh_test_admission(snapshot: CapabilitySnapshot) -> CapabilitySnapshot:
    if type(snapshot) is not CapabilitySnapshot:
        raise TypeError("snapshot must be exact CapabilitySnapshot")
    if snapshot.status != "VERIFIED":
        return snapshot
    return CapabilitySnapshot(
        snapshot_id=snapshot.snapshot_id,
        provider_id=snapshot.provider_id,
        account_id=snapshot.account_id,
        entity_id=snapshot.entity_id,
        environment=snapshot.environment,
        provider_environment=snapshot.provider_environment,
        instrument_version=snapshot.instrument_version,
        observed_at=snapshot.observed_at,
        expires_at=snapshot.expires_at,
        supported_order_types=snapshot.supported_order_types,
        time_in_force=snapshot.time_in_force,
        permission_scopes=snapshot.permission_scopes,
        position_mode=snapshot.position_mode,
        native_protection=snapshot.native_protection,
        rate_limit_policy_id=snapshot.rate_limit_policy_id,
        data_entitlements=snapshot.data_entitlements,
        evidence=snapshot.evidence,
        status=snapshot.status,
        sources=snapshot.sources,
        _verification_token=_DERIVED_SNAPSHOT_TOKEN,
        _admission_token=_FRESH_ADMISSION_TOKEN,
    )


def register_fresh_test_snapshot(registry, snapshot: CapabilitySnapshot) -> bool:
    """Test-only stand-in for future canonical issuer-owned registry admission."""

    if type(snapshot) is not CapabilitySnapshot:
        raise TypeError("snapshot must be exact CapabilitySnapshot")
    from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry

    if type(registry) not in {CapabilityRegistry, DurableCapabilityRegistry}:
        raise TypeError("registry must be an exact canonical capability registry")
    inserted = registry.add(snapshot)
    if snapshot.status == "VERIFIED":
        registry._fresh_content_sha256.add(_capability_content_sha256(snapshot))
    return inserted
