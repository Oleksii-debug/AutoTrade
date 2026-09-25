"""Canonical capability fixtures for provider and risk tests.

VERIFIED snapshots must exercise the same evidence-derivation boundary as runtime
code.  These helpers deliberately use an explicit trusted test verifier rather
than constructing VERIFIED CapabilitySnapshot objects directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Iterable
from uuid import NAMESPACE_URL, uuid4, uuid5

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    CapabilitySnapshot,
    EvidenceVerification,
    derive_capability_snapshot,
)


_CANONICAL_SOURCES = ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")


def _trusted_test_evidence(_claim: CapabilityClaim) -> EvidenceVerification:
    return EvidenceVerification(valid=True)


def capability_snapshot(
    *,
    snapshot_id: str | None = None,
    provider_id: str,
    account_id: str,
    entity_id: str,
    environment: str,
    instrument_version: str,
    observed_at: datetime,
    expires_at: datetime,
    supported_order_types: Iterable[str],
    time_in_force: Iterable[str],
    permission_scopes: Iterable[str],
    position_mode: str,
    native_protection: Iterable[str] = (),
    rate_limit_policy_id: str,
    data_entitlements: Iterable[str] = (),
    source_uri: str = "https://example.invalid/autotrade-test-capability",
    status: str = "VERIFIED",
) -> CapabilitySnapshot:
    """Build a capability fixture without bypassing VERIFIED evidence derivation."""

    sid = snapshot_id or str(uuid4())
    order_types = frozenset(supported_order_types)
    tif = frozenset(time_in_force)
    scopes = frozenset(permission_scopes)
    protection = frozenset(native_protection)
    entitlements = frozenset(data_entitlements)

    if status != "VERIFIED":
        return CapabilitySnapshot(
            snapshot_id=sid,
            provider_id=provider_id,
            account_id=account_id,
            entity_id=entity_id,
            environment=environment,
            instrument_version=instrument_version,
            observed_at=observed_at,
            expires_at=expires_at,
            supported_order_types=order_types,
            time_in_force=tif,
            permission_scopes=scopes,
            position_mode=position_mode,
            native_protection=protection,
            rate_limit_policy_id=rate_limit_policy_id,
            data_entitlements=entitlements,
            evidence=(),
            status=status,
            sources=frozenset(),
        )

    observed_utc = observed_at.astimezone(timezone.utc)
    observed_text = observed_utc.isoformat().replace("+00:00", "Z")
    claims = []
    for source in _CANONICAL_SOURCES:
        identity = f"{sid}:{source}"
        artifact_id = str(uuid5(NAMESPACE_URL, identity))
        digest = "sha256:" + sha256(identity.encode("utf-8")).hexdigest()
        claims.append(
            CapabilityClaim(
                source=source,
                provider_id=provider_id,
                account_id=account_id,
                entity_id=entity_id,
                environment=environment,
                instrument_version=instrument_version,
                observed_at=observed_at,
                expires_at=expires_at,
                supported_order_types=order_types,
                time_in_force=tif,
                permission_scopes=scopes,
                position_mode=position_mode,
                native_protection=protection,
                rate_limit_policy_id=rate_limit_policy_id,
                data_entitlements=entitlements,
                evidence_ref={
                    "artifact_id": artifact_id,
                    "sha256": digest,
                    "observed_at": observed_text,
                    "source_uri": f"{source_uri}#{source.lower()}",
                },
            )
        )

    return derive_capability_snapshot(
        snapshot_id=sid,
        claims=claims,
        observed_at=observed_at,
        evidence_verifier=_trusted_test_evidence,
    )
