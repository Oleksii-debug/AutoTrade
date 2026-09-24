"""Deterministic authority and confirmation gates for AutoTrade."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import FrozenSet


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _instant(value: str, *, name: str) -> datetime:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class AuthorityPolicy:
    policy_id: str
    account_id: str
    environments: FrozenSet[str]
    instruments: FrozenSet[str]
    actions: FrozenSet[str]
    max_notional: Decimal
    expires_at: str
    autonomous: bool
    protection_only: bool = False

    @classmethod
    def create(
        cls,
        *,
        policy_id: str,
        account_id: str,
        environments,
        instruments,
        actions,
        max_notional,
        expires_at: str,
        autonomous: bool,
        protection_only: bool = False,
    ) -> "AuthorityPolicy":
        normalized_environments = frozenset(str(item).upper() for item in environments)
        if not normalized_environments or not normalized_environments <= {"SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("environments must contain supported values")
        normalized_instruments = frozenset(_text(str(item), name="instrument") for item in instruments)
        normalized_actions = frozenset(str(item).upper() for item in actions)
        if not normalized_instruments or not normalized_actions:
            raise ValueError("instruments and actions must be non-empty")
        notional = _decimal(max_notional, name="max_notional")
        if notional <= 0:
            raise ValueError("max_notional must be positive")
        _instant(expires_at, name="expires_at")
        return cls(
            policy_id=_text(policy_id, name="policy_id"),
            account_id=_text(account_id, name="account_id"),
            environments=normalized_environments,
            instruments=normalized_instruments,
            actions=normalized_actions,
            max_notional=notional,
            expires_at=expires_at,
            autonomous=bool(autonomous),
            protection_only=bool(protection_only),
        )


@dataclass(frozen=True)
class Confirmation:
    confirmation_id: str
    policy_id: str
    intent_hash: str
    expires_at: str


@dataclass(frozen=True)
class AdmissionRecord:
    admission_id: str
    policy_id: str
    intent_hash: str
    state_version: int
    authority_epoch: int
    outcome: str
    admitted_at: str
    confirmation_id: str | None
    reason: str


class AuthorityConflict(ValueError):
    """Raised when immutable authority identity is reused inconsistently."""


class AuthorityService:
    def __init__(self):
        self._policies: dict[str, AuthorityPolicy] = {}
        self._revocations: dict[str, tuple[str, str]] = {}
        self._confirmations: dict[str, Confirmation] = {}
        self._used_confirmations: set[str] = set()
        self._admissions: dict[str, AdmissionRecord] = {}
        self._epoch = 0

    @property
    def epoch(self) -> int:
        return self._epoch

    def register_policy(self, policy: AuthorityPolicy) -> bool:
        existing = self._policies.get(policy.policy_id)
        if existing is not None:
            if existing != policy:
                raise AuthorityConflict("policy_id already has different content")
            return False
        self._policies[policy.policy_id] = policy
        self._epoch += 1
        return True

    def revoke_policy(self, policy_id: str, *, reason: str, revoked_at: str) -> bool:
        pid = _text(policy_id, name="policy_id")
        if pid not in self._policies:
            raise KeyError(pid)
        normalized = (_text(reason, name="reason"), revoked_at)
        _instant(revoked_at, name="revoked_at")
        existing = self._revocations.get(pid)
        if existing is not None:
            if existing != normalized:
                raise AuthorityConflict("policy revocation already recorded differently")
            return False
        self._revocations[pid] = normalized
        self._epoch += 1
        return True

    def add_confirmation(
        self,
        *,
        confirmation_id: str,
        policy_id: str,
        intent_hash: str,
        expires_at: str,
    ) -> bool:
        cid = _text(confirmation_id, name="confirmation_id")
        pid = _text(policy_id, name="policy_id")
        if pid not in self._policies:
            raise KeyError(pid)
        confirmation = Confirmation(
            confirmation_id=cid,
            policy_id=pid,
            intent_hash=_text(intent_hash, name="intent_hash"),
            expires_at=expires_at,
        )
        _instant(expires_at, name="expires_at")
        existing = self._confirmations.get(cid)
        if existing is not None:
            if existing != confirmation:
                raise AuthorityConflict("confirmation_id already has different content")
            return False
        self._confirmations[cid] = confirmation
        return True

    def _policy_active(self, policy: AuthorityPolicy, now: str) -> tuple[bool, str]:
        current = _instant(now, name="now")
        if policy.policy_id in self._revocations:
            return False, "policy_revoked"
        if current >= _instant(policy.expires_at, name="policy.expires_at"):
            return False, "policy_expired"
        return True, "active"

    def admit(
        self,
        *,
        admission_id: str,
        policy_id: str,
        intent_hash: str,
        account_id: str,
        environment: str,
        instrument: str,
        action: str,
        notional,
        state_version: int,
        risk_admitted: bool,
        now: str,
        confirmation_id: str | None = None,
        risk_reducing: bool = False,
    ) -> AdmissionRecord:
        aid = _text(admission_id, name="admission_id")
        pid = _text(policy_id, name="policy_id")
        ihash = _text(intent_hash, name="intent_hash")
        if not isinstance(state_version, int) or isinstance(state_version, bool) or state_version < 0:
            raise ValueError("state_version must be a non-negative integer")
        policy = self._policies.get(pid)
        if policy is None:
            raise KeyError(pid)
        amount = _decimal(notional, name="notional")
        if amount < 0:
            raise ValueError("notional must be non-negative")

        active, reason = self._policy_active(policy, now)
        checks = [
            (active, reason),
            (risk_admitted, "risk_rejected"),
            (account_id == policy.account_id, "account_out_of_scope"),
            (environment.upper() in policy.environments, "environment_out_of_scope"),
            (instrument in policy.instruments, "instrument_out_of_scope"),
            (action.upper() in policy.actions, "action_out_of_scope"),
            (amount <= policy.max_notional, "notional_out_of_scope"),
            (not policy.protection_only or risk_reducing, "protection_policy_requires_risk_reduction"),
        ]
        outcome = "ADMITTED"
        failure_reason = "admitted"
        for passed, failed_reason in checks:
            if not passed:
                outcome = "REJECTED"
                failure_reason = failed_reason
                break

        used_confirmation: str | None = None
        if outcome == "ADMITTED" and not policy.autonomous:
            if confirmation_id is None:
                outcome, failure_reason = "REJECTED", "confirmation_required"
            else:
                confirmation = self._confirmations.get(confirmation_id)
                if confirmation is None:
                    outcome, failure_reason = "REJECTED", "confirmation_missing"
                elif confirmation.confirmation_id in self._used_confirmations:
                    outcome, failure_reason = "REJECTED", "confirmation_already_used"
                elif confirmation.policy_id != pid:
                    outcome, failure_reason = "REJECTED", "confirmation_policy_mismatch"
                elif confirmation.intent_hash != ihash:
                    outcome, failure_reason = "REJECTED", "confirmation_intent_mismatch"
                elif _instant(now, name="now") >= _instant(confirmation.expires_at, name="confirmation.expires_at"):
                    outcome, failure_reason = "REJECTED", "confirmation_expired"
                else:
                    used_confirmation = confirmation.confirmation_id

        record = AdmissionRecord(
            admission_id=aid,
            policy_id=pid,
            intent_hash=ihash,
            state_version=state_version,
            authority_epoch=self._epoch,
            outcome=outcome,
            admitted_at=now,
            confirmation_id=used_confirmation,
            reason=failure_reason,
        )
        existing = self._admissions.get(aid)
        if existing is not None:
            if existing != record:
                raise AuthorityConflict("admission_id already has different content")
            return existing
        self._admissions[aid] = record
        if outcome == "ADMITTED" and used_confirmation is not None:
            self._used_confirmations.add(used_confirmation)
        return record

    def dispatch_allowed(
        self,
        admission_id: str,
        *,
        intent_hash: str,
        now: str,
    ) -> tuple[bool, str]:
        record = self._admissions.get(_text(admission_id, name="admission_id"))
        if record is None:
            return False, "admission_missing"
        if record.outcome != "ADMITTED":
            return False, "admission_not_admitted"
        if record.intent_hash != _text(intent_hash, name="intent_hash"):
            return False, "intent_hash_changed"
        policy = self._policies[record.policy_id]
        active, reason = self._policy_active(policy, now)
        if not active:
            return False, reason
        if record.confirmation_id is not None:
            confirmation = self._confirmations[record.confirmation_id]
            if _instant(now, name="now") >= _instant(confirmation.expires_at, name="confirmation.expires_at"):
                return False, "confirmation_expired"
        if record.authority_epoch != self._epoch:
            return False, "authority_epoch_changed"
        return True, "allowed"
