"""Exact intent-bound authority and confirmation foundation for AutoTrade."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _aware(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _validate_canonical(value: Any, *, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        raise TypeError(f"binary floating point is forbidden in intent at {path}")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_canonical(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError(f"intent object keys must be strings at {path}")
        for key, item in value.items():
            _validate_canonical(item, path=f"{path}.{key}")
        return
    raise TypeError(f"unsupported intent value at {path}: {type(value).__name__}")


def canonical_intent_hash(intent: Mapping[str, Any]) -> str:
    if not isinstance(intent, Mapping):
        raise TypeError("intent must be an object")
    payload = dict(intent)
    _validate_canonical(payload)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _scope(values: Sequence[str], *, name: str) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(_text(value, name=name) for value in values))
    if not normalized:
        raise ValueError(f"{name} scope must not be empty")
    return normalized


@dataclass(frozen=True)
class AuthorityPolicy:
    policy_id: str
    version: int
    actor_id: str
    environment: str
    allowed_actions: tuple[str, ...]
    provider_ids: tuple[str, ...]
    account_ids: tuple[str, ...]
    instrument_ids: tuple[str, ...]
    valid_from: datetime
    valid_until: datetime
    autonomous: bool
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class Confirmation:
    confirmation_id: str
    actor_id: str
    policy_id: str
    policy_version: int
    intent_hash: str
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class AuthorityDecision:
    allowed: bool
    intent_hash: str
    policy_id: str
    policy_version: int
    barrier_expires_at: datetime | None
    reason_codes: tuple[str, ...]


def build_policy(
    *,
    policy_id: str,
    version: int,
    actor_id: str,
    environment: str,
    allowed_actions: Sequence[str],
    provider_ids: Sequence[str],
    account_ids: Sequence[str],
    instrument_ids: Sequence[str],
    valid_from: datetime,
    valid_until: datetime,
    autonomous: bool,
    revoked_at: datetime | None = None,
) -> AuthorityPolicy:
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("version must be a positive integer")
    start = _aware(valid_from, name="valid_from")
    end = _aware(valid_until, name="valid_until")
    if end <= start:
        raise ValueError("valid_until must be later than valid_from")
    revoked = None if revoked_at is None else _aware(revoked_at, name="revoked_at")
    if revoked is not None and revoked < start:
        raise ValueError("revoked_at cannot precede valid_from")
    return AuthorityPolicy(
        policy_id=_text(policy_id, name="policy_id"),
        version=version,
        actor_id=_text(actor_id, name="actor_id"),
        environment=_text(environment, name="environment").upper(),
        allowed_actions=tuple(action.upper() for action in _scope(allowed_actions, name="allowed_action")),
        provider_ids=_scope(provider_ids, name="provider_id"),
        account_ids=_scope(account_ids, name="account_id"),
        instrument_ids=_scope(instrument_ids, name="instrument_id"),
        valid_from=start,
        valid_until=end,
        autonomous=bool(autonomous),
        revoked_at=revoked,
    )


def issue_confirmation(
    *,
    confirmation_id: str,
    actor_id: str,
    policy: AuthorityPolicy,
    intent: Mapping[str, Any],
    issued_at: datetime,
    expires_at: datetime,
) -> Confirmation:
    issued = _aware(issued_at, name="issued_at")
    expiry = _aware(expires_at, name="expires_at")
    if expiry <= issued:
        raise ValueError("expires_at must be later than issued_at")
    if issued < policy.valid_from or issued >= policy.valid_until:
        raise ValueError("confirmation must be issued while policy is current")
    if policy.revoked_at is not None and issued >= policy.revoked_at:
        raise PermissionError("revoked policy cannot issue confirmation")
    actor = _text(actor_id, name="actor_id")
    if actor != policy.actor_id:
        raise PermissionError("confirmation actor does not own the policy")
    return Confirmation(
        confirmation_id=_text(confirmation_id, name="confirmation_id"),
        actor_id=actor,
        policy_id=policy.policy_id,
        policy_version=policy.version,
        intent_hash=canonical_intent_hash(intent),
        issued_at=issued,
        expires_at=min(expiry, policy.valid_until),
    )


def evaluate_authority(
    *,
    policy: AuthorityPolicy,
    intent: Mapping[str, Any],
    action: str,
    environment: str,
    provider_id: str,
    account_id: str,
    instrument_id: str,
    at: datetime,
    confirmation: Confirmation | None = None,
) -> AuthorityDecision:
    now = _aware(at, name="at")
    digest = canonical_intent_hash(intent)
    reasons: list[str] = []

    if now < policy.valid_from or now >= policy.valid_until:
        reasons.append("AUTH.POLICY_EXPIRED")
    if policy.revoked_at is not None and now >= policy.revoked_at:
        reasons.append("AUTH.POLICY_REVOKED")
    if _text(environment, name="environment").upper() != policy.environment:
        reasons.append("AUTH.ENVIRONMENT_MISMATCH")
    if _text(action, name="action").upper() not in policy.allowed_actions:
        reasons.append("AUTH.ACTION_OUT_OF_SCOPE")
    if _text(provider_id, name="provider_id") not in policy.provider_ids:
        reasons.append("AUTH.PROVIDER_OUT_OF_SCOPE")
    if _text(account_id, name="account_id") not in policy.account_ids:
        reasons.append("AUTH.ACCOUNT_OUT_OF_SCOPE")
    if _text(instrument_id, name="instrument_id") not in policy.instrument_ids:
        reasons.append("AUTH.INSTRUMENT_OUT_OF_SCOPE")

    barrier = policy.valid_until
    if not policy.autonomous:
        if confirmation is None:
            reasons.append("AUTH.CONFIRMATION_REQUIRED")
        else:
            barrier = min(barrier, confirmation.expires_at)
            if confirmation.actor_id != policy.actor_id:
                reasons.append("AUTH.CONFIRMATION_ACTOR_MISMATCH")
            if confirmation.policy_id != policy.policy_id or confirmation.policy_version != policy.version:
                reasons.append("AUTH.CONFIRMATION_POLICY_MISMATCH")
            if confirmation.intent_hash != digest:
                reasons.append("AUTH.CONFIRMATION_INTENT_MISMATCH")
            if now < confirmation.issued_at or now >= confirmation.expires_at:
                reasons.append("AUTH.CONFIRMATION_EXPIRED")
            if confirmation.revoked_at is not None and now >= confirmation.revoked_at:
                reasons.append("AUTH.CONFIRMATION_REVOKED")

    unique = tuple(dict.fromkeys(reasons))
    return AuthorityDecision(
        allowed=not unique,
        intent_hash=digest,
        policy_id=policy.policy_id,
        policy_version=policy.version,
        barrier_expires_at=barrier if not unique else None,
        reason_codes=unique,
    )


def dispatch_barrier(
    *,
    admission: AuthorityDecision,
    current_policy: AuthorityPolicy,
    intent: Mapping[str, Any],
    action: str,
    environment: str,
    provider_id: str,
    account_id: str,
    instrument_id: str,
    at: datetime,
    confirmation: Confirmation | None = None,
) -> AuthorityDecision:
    current = evaluate_authority(
        policy=current_policy,
        intent=intent,
        action=action,
        environment=environment,
        provider_id=provider_id,
        account_id=account_id,
        instrument_id=instrument_id,
        at=at,
        confirmation=confirmation,
    )
    reasons = list(current.reason_codes)
    if admission.policy_id != current.policy_id or admission.policy_version != current.policy_version:
        reasons.append("AUTH.POLICY_CHANGED_AFTER_ADMISSION")
    if admission.intent_hash != current.intent_hash:
        reasons.append("AUTH.INTENT_CHANGED_AFTER_ADMISSION")
    if not admission.allowed:
        reasons.append("AUTH.ADMISSION_WAS_NOT_ALLOWED")
    if admission.barrier_expires_at is None or at >= admission.barrier_expires_at:
        reasons.append("AUTH.ADMISSION_EXPIRED")
    unique = tuple(dict.fromkeys(reasons))
    return AuthorityDecision(
        allowed=not unique,
        intent_hash=current.intent_hash,
        policy_id=current.policy_id,
        policy_version=current.policy_version,
        barrier_expires_at=current.barrier_expires_at if not unique else None,
        reason_codes=unique,
    )


def require_admission(authority: AuthorityDecision, *, risk_verdict: str) -> None:
    if not authority.allowed:
        raise PermissionError("Authority decision rejected the intent")
    if risk_verdict != "ALLOW":
        raise PermissionError("Risk decision rejected the intent")
