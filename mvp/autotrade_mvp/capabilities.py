"""Fail-closed capability evidence intersection for the AutoTrade MVP."""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
import hashlib
import re
from types import MappingProxyType
from typing import Callable, Iterable, Mapping
from urllib.parse import urlsplit
from uuid import UUID


class CapabilityError(ValueError):
    """Raised when capability evidence cannot safely admit an action."""


SOURCES = frozenset({"DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT"})
ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})
STATUSES = frozenset({"VERIFIED", "UNKNOWN", "CONFLICTED", "EXPIRED"})
_DERIVED_SNAPSHOT_TOKEN = object()


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
        evidence_observed = datetime.fromisoformat(
            str(evidence["observed_at"])[:-1] + "+00:00"
        ).astimezone(timezone.utc)
        if evidence_observed != observed:
            raise CapabilityError(
                "evidence observed_at must match claim observed_at"
            )
        object.__setattr__(self, "evidence_ref", evidence)


@dataclass(frozen=True)
class EvidenceVerification:
    """Result of resolving one capability claim to immutable evidence."""

    valid: bool
    conflicted: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        if type(self.valid) is not bool or type(self.conflicted) is not bool:
            raise CapabilityError("evidence verification flags must be boolean")
        if not isinstance(self.reason, str):
            raise CapabilityError("evidence verification reason must be text")
        if self.valid and self.conflicted:
            raise CapabilityError("evidence cannot be both valid and conflicted")
        if not self.valid and not self.reason.strip():
            raise CapabilityError("invalid evidence verification requires a reason")


_CAPABILITY_PRODUCER_TYPES = {
    "DOCUMENTED": "PROVIDER_DOCUMENTATION",
    "API": "PROVIDER_API",
    "ACCOUNT": "ACCOUNT_CAPABILITY",
    "INSTRUMENT": "INSTRUMENT_CAPABILITY",
}


def artifact_store_evidence_verifier(
    store: object,
) -> Callable[[CapabilityClaim], EvidenceVerification]:
    """Bind capability claims to the canonical immutable artifact store.

    The adapter relies only on the existing store public load_manifest and
    read_bytes methods so capability authority does not create a second
    evidence repository.
    """

    def verify(claim: CapabilityClaim) -> EvidenceVerification:
        artifact_id = str(claim.evidence_ref["artifact_id"])
        expected_digest = str(claim.evidence_ref["sha256"])
        try:
            load_manifest = getattr(store, "load_manifest")
            read_bytes = getattr(store, "read_bytes")
            manifest = load_manifest(artifact_id)
            payload = read_bytes(artifact_id)
        except FileNotFoundError:
            return EvidenceVerification(
                valid=False,
                reason="evidence artifact is missing",
            )
        except Exception:
            return EvidenceVerification(
                valid=False,
                conflicted=True,
                reason="evidence artifact exists but is unreadable or corrupt",
            )

        if type(manifest) is not dict or not isinstance(payload, bytes):
            return EvidenceVerification(
                valid=False,
                conflicted=True,
                reason="evidence artifact has an unsupported representation",
            )

        actual_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        if (
            manifest.get("artifact_id") != artifact_id
            or manifest.get("sha256") != expected_digest
            or actual_digest != expected_digest
        ):
            return EvidenceVerification(
                valid=False,
                conflicted=True,
                reason="evidence artifact identity or digest does not match the claim",
            )

        metadata = manifest.get("metadata")
        if type(metadata) is not dict:
            return EvidenceVerification(
                valid=False,
                conflicted=True,
                reason="evidence artifact metadata is missing",
            )

        expected_metadata = {
            "artifact_kind": "CAPABILITY_EVIDENCE",
            "schema_version": 1,
            "capability_source": claim.source,
            "producer_type": _CAPABILITY_PRODUCER_TYPES[claim.source],
            "provider_id": claim.provider_id,
            "account_id": claim.account_id,
            "entity_id": claim.entity_id,
            "environment": claim.environment,
            "instrument_version": claim.instrument_version,
            "observed_at": claim.evidence_ref["observed_at"],
        }
        if any(metadata.get(key) != value for key, value in expected_metadata.items()):
            return EvidenceVerification(
                valid=False,
                conflicted=True,
                reason="evidence artifact semantics or financial identity do not match the claim",
            )
        if not isinstance(metadata.get("producer_id"), str) or not metadata["producer_id"].strip():
            return EvidenceVerification(
                valid=False,
                conflicted=True,
                reason="evidence artifact producer identity is missing",
            )
        if (
            not isinstance(metadata.get("evidence_version"), str)
            or not metadata["evidence_version"].strip()
        ):
            return EvidenceVerification(
                valid=False,
                conflicted=True,
                reason="evidence artifact version is missing",
            )

        source_uri = claim.evidence_ref.get("source_uri")
        if source_uri is not None:
            source_refs = manifest.get("source_refs")
            if not isinstance(source_refs, list) or source_uri not in source_refs:
                return EvidenceVerification(
                    valid=False,
                    conflicted=True,
                    reason="evidence artifact provenance does not contain the claimed source URI",
                )

        rights_id = claim.evidence_ref.get("rights_id")
        if rights_id is not None:
            rights = manifest.get("rights")
            if type(rights) is not dict or rights.get("rights_id") != rights_id:
                return EvidenceVerification(
                    valid=False,
                    conflicted=True,
                    reason="evidence artifact rights identity does not match the claim",
                )

        return EvidenceVerification(valid=True)

    return verify


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
    _verification_token: InitVar[object | None] = None

    def __post_init__(self, _verification_token: object | None) -> None:
        try:
            UUID(self.snapshot_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise CapabilityError("snapshot_id must be a UUID") from error
        for field in ("provider_id", "account_id", "entity_id", "instrument_version"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        environment = _text(self.environment, "environment").upper()
        if environment not in ENVIRONMENTS:
            raise CapabilityError("environment is unsupported")
        object.__setattr__(self, "environment", environment)
        observed = _instant(self.observed_at, "observed_at")
        expires = _instant(self.expires_at, "expires_at")
        if expires < observed:
            raise CapabilityError("expires_at cannot be before observed_at")
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
        status = _text(self.status, "status").upper()
        if status not in STATUSES:
            raise CapabilityError("status is unsupported")
        object.__setattr__(self, "status", status)
        sources = frozenset(_text(source, "source").upper() for source in self.sources)
        if not sources.issubset(SOURCES):
            raise CapabilityError("snapshot sources contain unsupported source")
        object.__setattr__(self, "sources", sources)
        evidence = tuple(_freeze_evidence(item) for item in self.evidence)
        object.__setattr__(self, "evidence", evidence)
        if status == "VERIFIED":
            if _verification_token is not _DERIVED_SNAPSHOT_TOKEN:
                raise CapabilityError(
                    "VERIFIED capability snapshots must come from canonical evidence derivation"
                )
            if sources != SOURCES:
                raise CapabilityError(
                    "VERIFIED capability snapshots require all canonical sources"
                )
            if len(evidence) < len(SOURCES):
                raise CapabilityError(
                    "VERIFIED capability snapshots require evidence for every canonical source"
                )
            if (
                not self.supported_order_types
                or not self.time_in_force
                or not self.permission_scopes
            ):
                raise CapabilityError(
                    "VERIFIED capability snapshots require executable capability intersections"
                )

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
    evidence_verifier: Callable[[CapabilityClaim], EvidenceVerification] | None = None,
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

    evidence_results: tuple[EvidenceVerification, ...]
    if evidence_verifier is None:
        evidence_results = tuple(
            EvidenceVerification(
                valid=False,
                reason="immutable evidence verifier is required",
            )
            for _ in live
        )
    else:
        verified_results: list[EvidenceVerification] = []
        for claim in live:
            try:
                result = evidence_verifier(claim)
            except Exception:
                result = EvidenceVerification(
                    valid=False,
                    reason="immutable evidence verification failed",
                )
            if not isinstance(result, EvidenceVerification):
                raise TypeError("evidence_verifier must return EvidenceVerification")
            verified_results.append(result)
        evidence_results = tuple(verified_results)

    verified_live = tuple(
        claim
        for claim, result in zip(live, evidence_results, strict=True)
        if result.valid
    )
    verified_sources = frozenset(claim.source for claim in verified_live)
    evidence_missing_sources = required - verified_sources
    evidence_conflict = any(result.conflicted for result in evidence_results)
    evidence_incomplete = any(not result.valid for result in evidence_results)

    order_types = _intersection(verified_live, "supported_order_types")
    tif = _intersection(verified_live, "time_in_force")
    scopes = _intersection(verified_live, "permission_scopes")
    protection = _intersection(verified_live, "native_protection")
    entitlements = _intersection(verified_live, "data_entitlements")
    position_modes = {claim.position_mode for claim in verified_live}
    rate_policies = {claim.rate_limit_policy_id for claim in verified_live}

    set_conflict = bool(verified_live) and (not order_types or not tif or not scopes)
    scalar_conflict = len(position_modes) > 1 or len(rate_policies) > 1

    if missing_sources:
        status = "UNKNOWN"
    elif future_evidence:
        status = "CONFLICTED"
    elif expired_sources:
        status = "EXPIRED"
    elif evidence_conflict:
        status = "CONFLICTED"
    elif evidence_incomplete:
        status = "UNKNOWN"
    elif evidence_missing_sources:
        status = "UNKNOWN"
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
        evidence=tuple(claim.evidence_ref for claim in verified_live),
        status=status,
        sources=verified_sources,
        _verification_token=_DERIVED_SNAPSHOT_TOKEN,
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
