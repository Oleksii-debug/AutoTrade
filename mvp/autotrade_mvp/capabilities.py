"""Evidence-intersection capability foundation for AutoTrade accounts and instruments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


REQUIRED_KINDS = ("DOCUMENTATION", "API", "ACCOUNT", "INSTRUMENT")
CLAIM_VALUES = {"ALLOW", "DENY", "UNKNOWN"}
CAPABILITY_STATES = {"VERIFIED", "DENIED", "UNKNOWN", "CONFLICT"}


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _aware(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


@dataclass(frozen=True)
class CapabilityClaim:
    kind: str
    value: str
    observed_at: datetime
    expires_at: datetime
    evidence_ref: str

    def __post_init__(self) -> None:
        kind = _text(self.kind, name="kind").upper()
        value = _text(self.value, name="value").upper()
        if kind not in REQUIRED_KINDS:
            raise ValueError(f"Unsupported capability claim kind: {kind}")
        if value not in CLAIM_VALUES:
            raise ValueError(f"Unsupported capability claim value: {value}")
        observed = _aware(self.observed_at, name="observed_at")
        expires = _aware(self.expires_at, name="expires_at")
        if expires <= observed:
            raise ValueError("expires_at must be later than observed_at")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, name="evidence_ref"))


@dataclass(frozen=True)
class CapabilitySnapshot:
    provider_id: str
    account_id: str
    instrument_id: str
    action: str
    evaluated_at: datetime
    status: str
    valid_until: datetime | None
    reason_codes: tuple[str, ...]
    claims: tuple[CapabilityClaim, ...]


def evaluate_capability(
    *,
    provider_id: str,
    account_id: str,
    instrument_id: str,
    action: str,
    evaluated_at: datetime,
    claims: Iterable[CapabilityClaim],
) -> CapabilitySnapshot:
    provider = _text(provider_id, name="provider_id")
    account = _text(account_id, name="account_id")
    instrument = _text(instrument_id, name="instrument_id")
    normalized_action = _text(action, name="action").upper()
    now = _aware(evaluated_at, name="evaluated_at")
    evidence = tuple(claims)
    if not evidence:
        return CapabilitySnapshot(
            provider,
            account,
            instrument,
            normalized_action,
            now,
            "UNKNOWN",
            None,
            ("CAPABILITY.NO_EVIDENCE",),
            (),
        )

    grouped: dict[str, list[CapabilityClaim]] = {kind: [] for kind in REQUIRED_KINDS}
    future: list[CapabilityClaim] = []
    expired_by_kind: dict[str, list[CapabilityClaim]] = {kind: [] for kind in REQUIRED_KINDS}
    for claim in evidence:
        if not isinstance(claim, CapabilityClaim):
            raise TypeError("claims must contain CapabilityClaim values")
        if claim.observed_at > now:
            future.append(claim)
        elif claim.expires_at <= now:
            expired_by_kind[claim.kind].append(claim)
        else:
            grouped[claim.kind].append(claim)

    reasons: list[str] = []
    if future:
        reasons.append("EVIDENCE.FUTURE")
    conflicts = False
    denied = False
    unknown = False
    live_claims: list[CapabilityClaim] = []

    for kind in REQUIRED_KINDS:
        values = grouped[kind]
        live_claims.extend(values)
        if not values:
            unknown = True
            if expired_by_kind[kind]:
                reasons.append(f"CAPABILITY.EXPIRED_{kind}")
            else:
                reasons.append(f"CAPABILITY.MISSING_{kind}")
            continue
        distinct = {claim.value for claim in values}
        if "ALLOW" in distinct and "DENY" in distinct:
            conflicts = True
            reasons.append(f"CAPABILITY.CONFLICT_{kind}")
            continue
        if "DENY" in distinct:
            denied = True
            reasons.append(f"CAPABILITY.DENIED_{kind}")
        if "UNKNOWN" in distinct or distinct == {"UNKNOWN"}:
            unknown = True
            reasons.append(f"CAPABILITY.UNKNOWN_{kind}")

    if future:
        conflicts = True
    if conflicts:
        status = "CONFLICT"
    elif denied:
        status = "DENIED"
    elif unknown:
        status = "UNKNOWN"
    else:
        status = "VERIFIED"

    valid_until = (
        min(claim.expires_at for claim in live_claims)
        if status == "VERIFIED" and live_claims
        else None
    )
    return CapabilitySnapshot(
        provider_id=provider,
        account_id=account,
        instrument_id=instrument,
        action=normalized_action,
        evaluated_at=now,
        status=status,
        valid_until=valid_until,
        reason_codes=tuple(dict.fromkeys(reasons)),
        claims=evidence,
    )


def require_verified(snapshot: CapabilitySnapshot, *, action: str, at: datetime) -> None:
    now = _aware(at, name="at")
    requested = _text(action, name="action").upper()
    if snapshot.action != requested:
        raise PermissionError("Capability snapshot does not match requested action")
    if snapshot.status != "VERIFIED":
        raise PermissionError(f"Capability is {snapshot.status}")
    if snapshot.valid_until is None or now >= snapshot.valid_until:
        raise PermissionError("Capability evidence is expired")
