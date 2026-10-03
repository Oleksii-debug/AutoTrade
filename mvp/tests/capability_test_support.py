"""Test-only capability issuance helpers.

Production code must never import this module. It lets tests exercise
post-issuance consumers while public capability derivation remains descriptive
and non-admitting by construction.
"""

from mvp.autotrade_mvp.capabilities import (
    CapabilitySnapshot,
    _DERIVED_SNAPSHOT_TOKEN,
    _FRESH_ADMISSION_TOKEN,
)


def fresh_test_admission(snapshot: CapabilitySnapshot) -> CapabilitySnapshot:
    """Return an admitted copy for test fixtures only."""

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
