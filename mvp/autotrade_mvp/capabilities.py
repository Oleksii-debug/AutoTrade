"""Fail-closed capability evidence intersection for the AutoTrade MVP."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from types import MappingProxyType
from typing import Iterable, Mapping
from urllib.parse import urlsplit
from uuid import UUID


class CapabilityError(ValueError):
    """Raised when capability evidence cannot safely admit an action."""


SOURCES = frozenset({"DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT"})
ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})
STATUSES = frozenset({"VERIFIED", "UNKNOWN", "CONFLICTED", "EXPIRED"})
_CAPABILITY_SNAPSHOT_AUTHORITY = object()


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapabilityError(f"{field} is required")
    return value.strip()


def _instant(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CapabilityError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _set(values: Iterable[str], field: str) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise CapabilityError(f"{field} must be a collection")
    return frozenset(_text(value, field) for value in values)


def _freeze_evidence(value: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CapabilityError("evidence_ref must be an object")
    required = {"artifact_id", "sha256", "observed_at"}
    allowed = required | {"source_uri", "rights_id"}
    keys = set(value)
    if required - keys:
        raise CapabilityError("evidence_ref is missing required fields")
    if keys - allowed:
        raise CapabilityError("evidence_ref contains unknown fields")
    artifact_id = _text(value["artifact_id"], "artifact_id")
    try:
        UUID(artifact_id)
    except (ValueError, TypeError, AttributeError) as error:
        raise CapabilityError("evidence artifact_id must be a UUID") from error
    digest = _text(value["sha256"], "sha256")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise CapabilityError("evidence sha256 must be a canonical SHA-256 digest")
    observed_at = _text(value["observed_at"], "observed_at")
    if not observed_at.endswith("Z"):
        raise CapabilityError("evidence observed_at must be UTC and end in Z")
    try:
        parsed = datetime.fromisoformat(observed_at[:-1] + "+00:00")
    except ValueError as error:
        raise CapabilityError("evidence observed_at must be an ISO date-time") from error
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise CapabilityError("evidence observed_at must be UTC")
    normalized: dict[str, object] = {
        "artifact_id": artifact_id,
        "sha256": digest,
        "observed_at": observed_at,
    }
    if "source_uri" in value:
        source_uri = _text(value["source_uri"], "source_uri")
        if not urlsplit(source_uri).scheme:
            raise CapabilityError("evidence source_uri must be an absolute URI")
        normalized["source_uri"] = source_uri
    if "rights_id" in value:
        normalized["rights_id"] = _text(value["rights_id"], "rights_id")
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class CapabilityClaim:
    source: str
    provider_id: str
    account_id: str
    entity_id: str
    environment: str
    instrument_version: str
    observed_at: datetime
    expires_at: datetime
    supported_order_types: frozenset[str]
    time_in_force: frozenset[str]
    permission_scopes: frozenset[str]
    position_mode: str
    native_protection: frozenset[str]
    rate_limit_policy_id: str
    data_entitlements: frozenset[str]
    evidence_ref: Mapping[str, object]

    def __post_init__(self) -> None:
        source = _text(self.source, "source").upper()
        if source not in SOURCES:
            raise CapabilityError("source is unsupported")
        object.__setattr__(self, "source", source)
        for field in ("provider_id", "account_id", "entity_id", "instrument_version"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        environment = _text(self.environment, "environment").upper()
        if environment not in ENVIRONMENTS:
            raise CapabilityError("environment is unsupported")
        object.__setattr__(self, "environment", environment)
        observed = _instant(self.observed_at, "observed_at")
        expires = _instant(self.expires_at, "expires_at")
        if expires <= observed:
            raise CapabilityError("expires_at must be after observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "expires_at", expires)
        for field in (
            "supported_order_types",
            "time_in_force",
            "permission_scopes",
            "native_protection",
            "data_entitlements",
        ):
            object.__setattr__(self, field, _set(getattr(self, field), field))
        object.__setattr__(self, "position_mode", _text(self.position_mode, "position_mode"))
        object.__setattr__(
            self,
            "rate_limit_policy_id",
            _text(self.rate_limit_policy_id, "rate_limit_policy_id"),
        )
        evidence = _freeze_evidence(self.evidence_ref)
        evidence_observed = _instant(
            datetime.fromisoformat(
                str(evidence["observed_at"])[:-1] + "+00:00"
            ),
            "evidence observed_at",
        )
        if evidence_observed > observed:
            raise CapabilityError(
                "evidence observed_at cannot be later than claim observed_at"
            )
        object.__setattr__(self, "evidence_ref", evidence)


@dataclass(frozen=True)
class CapabilitySnapshot:
    snapshot_id: str
    provider_id: str
    account_id: str
    entity_id: str
    environment: str
    instrument_version: str
    observed_at: datetime
    expires_at: datetime
    supported_order_types: frozenset[str]
    time_in_force: frozenset[str]
    permission_scopes: frozenset[str]
    position_mode: str
    native_protection: frozenset[str]
    rate_limit_policy_id: str
    data_entitlements: frozenset[str]
    evidence: tuple[Mapping[str, object], ...]
    status: str
    sources: frozenset[str]
    _authority_marker: object = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self._authority_marker is not _CAPABILITY_SNAPSHOT_AUTHORITY:
            raise CapabilityError(
                "CapabilitySnapshot can only be created by derive_capability_snapshot"
            )
        try:
            UUID(self.snapshot_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise CapabilityError("snapshot_id must be a UUID") from error
        if self.status not in STATUSES:
            raise CapabilityError("status is unsupported")
        object.__setattr__(self, "evidence", tuple(_freeze_evidence(item) for item in self.evidence))

    @property
    def identity(self) -> tuple[str, str, str, str, str]:
        return (
            self.provider_id,
            self.account_id,
            self.entity_id,
            self.environment,
            self.instrument_version,
        )

    def admits(
        self,
        *,
        at: datetime,
        order_type: str,
        time_in_force: str,
        permission_scope: str,
    ) -> bool:
        point = _instant(at, "at")
        return (
            self.status == "VERIFIED"
            and self.observed_at <= point < self.expires_at
            and _text(order_type, "order_type") in self.supported_order_types
            and _text(time_in_force, "time_in_force") in self.time_in_force
            and _text(permission_scope, "permission_scope") in self.permission_scopes
        )

    def to_contract_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "entity_id": self.entity_id,
            "environment": self.environment,
            "instrument_version": self.instrument_version,
            "observed_at": self.observed_at.isoformat().replace("+00:00", "Z"),
            "expires_at": self.expires_at.isoformat().replace("+00:00", "Z"),
            "supported_order_types": sorted(self.supported_order_types),
            "time_in_force": sorted(self.time_in_force),
            "permission_scopes": sorted(self.permission_scopes),
            "position_mode": self.position_mode,
            "native_protection": sorted(self.native_protection),
            "rate_limit_policy_id": self.rate_limit_policy_id,
            "data_entitlements": sorted(self.data_entitlements),
            "evidence": [dict(item) for item in self.evidence],
            "status": self.status,
        }


def _intersection(claims: tuple[CapabilityClaim, ...], field: str) -> frozenset[str]:
    values = [set(getattr(claim, field)) for claim in claims]
    if not values:
        return frozenset()
    result = values[0]
    for value in values[1:]:
        result &= value
    return frozenset(result)


def derive_capability_snapshot(
    *,
    snapshot_id: str,
    claims: Iterable[CapabilityClaim],
    observed_at: datetime,
    required_sources: frozenset[str] = SOURCES,
) -> CapabilitySnapshot:
    point = _instant(observed_at, "observed_at")
    records = tuple(claims)
    if not records:
        raise CapabilityError("at least one capability claim is required")
    required = frozenset(_text(source, "required_source").upper() for source in required_sources)
    if required != SOURCES:
        raise CapabilityError("all canonical capability sources are required for verification")

    if any(not isinstance(claim, CapabilityClaim) for claim in records):
        raise TypeError("claims must contain CapabilityClaim values")

    first = records[0]
    identity = (
        first.provider_id,
        first.account_id,
        first.entity_id,
        first.environment,
        first.instrument_version,
    )
    for claim in records[1:]:
        other = (
            claim.provider_id,
            claim.account_id,
            claim.entity_id,
            claim.environment,
            claim.instrument_version,
        )
        if other != identity:
            raise CapabilityError("capability claims describe different identities")

    all_sources = frozenset(claim.source for claim in records)
    missing_sources = required - all_sources
    future_evidence = any(claim.observed_at > point for claim in records)
    live = tuple(claim for claim in records if claim.observed_at <= point < claim.expires_at)
    live_sources = frozenset(claim.source for claim in live)
    expired_sources = frozenset(
        source
        for source in required - live_sources
        if any(claim.source == source and claim.expires_at <= point for claim in records)
    )

    order_types = _intersection(live, "supported_order_types")
    tif = _intersection(live, "time_in_force")
    scopes = _intersection(live, "permission_scopes")
    protection = _intersection(live, "native_protection")
    entitlements = _intersection(live, "data_entitlements")
    position_modes = {claim.position_mode for claim in live}
    rate_policies = {claim.rate_limit_policy_id for claim in live}

    set_conflict = bool(live) and (not order_types or not tif or not scopes)
    scalar_conflict = len(position_modes) > 1 or len(rate_policies) > 1

    if missing_sources:
        status = "UNKNOWN"
    elif future_evidence:
        status = "CONFLICTED"
    elif expired_sources:
        status = "EXPIRED"
    elif set_conflict or scalar_conflict:
        status = "CONFLICTED"
    else:
        status = "VERIFIED"

    expires_at = min((claim.expires_at for claim in live), default=point)
    return CapabilitySnapshot(
        snapshot_id=snapshot_id,
        provider_id=identity[0],
        account_id=identity[1],
        entity_id=identity[2],
        environment=identity[3],
        instrument_version=identity[4],
        observed_at=point,
        expires_at=expires_at,
        supported_order_types=order_types,
        time_in_force=tif,
        permission_scopes=scopes,
        position_mode=next(iter(position_modes)) if len(position_modes) == 1 else "CONFLICTED",
        native_protection=protection,
        rate_limit_policy_id=next(iter(rate_policies)) if len(rate_policies) == 1 else "CONFLICTED",
        data_entitlements=entitlements,
        evidence=tuple(claim.evidence_ref for claim in records),
        status=status,
        sources=live_sources,
        _authority_marker=_CAPABILITY_SNAPSHOT_AUTHORITY,
    )


class CapabilityRegistry:
    """Append-only snapshots resolved by exact account/instrument identity."""

    def __init__(self) -> None:
        self._by_id: dict[str, CapabilitySnapshot] = {}
        self._by_identity: dict[tuple[str, str, str, str, str], list[CapabilitySnapshot]] = {}

    def add(self, snapshot: CapabilitySnapshot) -> None:
        existing = self._by_id.get(snapshot.snapshot_id)
        if existing is not None:
            if existing != snapshot:
                raise CapabilityError("snapshot_id was reused with different content")
            return
        history = self._by_identity.setdefault(snapshot.identity, [])
        if history and snapshot.observed_at <= history[-1].observed_at:
            raise CapabilityError("snapshot observed_at must advance for the same identity")
        self._by_id[snapshot.snapshot_id] = snapshot
        history.append(snapshot)

    def latest(
        self,
        *,
        provider_id: str,
        account_id: str,
        entity_id: str,
        environment: str,
        instrument_version: str,
        at: datetime,
    ) -> CapabilitySnapshot:
        point = _instant(at, "at")
        identity = (
            _text(provider_id, "provider_id"),
            _text(account_id, "account_id"),
            _text(entity_id, "entity_id"),
            _text(environment, "environment").upper(),
            _text(instrument_version, "instrument_version"),
        )
        history = self._by_identity.get(identity, ())
        candidates = [snapshot for snapshot in history if snapshot.observed_at <= point]
        if not candidates:
            raise CapabilityError("no capability snapshot exists for the requested identity and time")
        return candidates[-1]

    def require_verified(self, **kwargs) -> CapabilitySnapshot:
        snapshot = self.latest(**kwargs)
        point = _instant(kwargs["at"], "at")
        if snapshot.status != "VERIFIED":
            raise CapabilityError(f"capability status is {snapshot.status}")
        if point >= snapshot.expires_at:
            raise CapabilityError("capability snapshot is expired")
        return snapshot
