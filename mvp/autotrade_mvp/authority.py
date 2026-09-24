"""Deterministic authority and confirmation gates for AutoTrade."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any, Callable, FrozenSet
from uuid import NAMESPACE_URL, uuid5

from .persistence import JournalStore, payload_digest


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
        normalized_environments = frozenset(
            _text(item, name="environment").upper() for item in environments
        )
        if not normalized_environments or not normalized_environments <= {"SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("environments must contain supported values")
        normalized_instruments = frozenset(
            _text(item, name="instrument") for item in instruments
        )
        normalized_actions = frozenset(
            _text(item, name="action").upper() for item in actions
        )
        if not normalized_instruments or not normalized_actions:
            raise ValueError("instruments and actions must be non-empty")
        notional = _decimal(max_notional, name="max_notional")
        if notional <= 0:
            raise ValueError("max_notional must be positive")
        _instant(expires_at, name="expires_at")
        if not isinstance(autonomous, bool) or not isinstance(protection_only, bool):
            raise TypeError("autonomous and protection_only must be booleans")
        return cls(
            policy_id=_text(policy_id, name="policy_id"),
            account_id=_text(account_id, name="account_id"),
            environments=normalized_environments,
            instruments=normalized_instruments,
            actions=normalized_actions,
            max_notional=notional,
            expires_at=expires_at,
            autonomous=autonomous,
            protection_only=protection_only,
        )


@dataclass(frozen=True)
class Confirmation:
    confirmation_id: str
    policy_id: str
    intent_hash: str
    account_id: str
    environment: str
    instrument: str
    action: str
    notional: Decimal
    expires_at: str


@dataclass(frozen=True)
class AdmissionRecord:
    admission_id: str
    policy_id: str
    intent_hash: str
    account_id: str
    environment: str
    instrument: str
    action: str
    notional: Decimal
    risk_reducing: bool
    state_version: int
    authority_epoch: int
    outcome: str
    admitted_at: str
    confirmation_id: str | None
    reason: str
    request_fingerprint: str


class AuthorityConflict(ValueError):
    """Raised when immutable authority identity is reused inconsistently."""


def _authority_event_id(event_type: str, key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"https://events.autotrade.local/authority/{event_type}/{key}"))


class AuthorityService:
    def __init__(self, store: JournalStore | None = None):
        self.store = store
        self._policies: dict[str, AuthorityPolicy] = {}
        self._revocations: dict[str, tuple[str, str]] = {}
        self._confirmations: dict[str, Confirmation] = {}
        self._used_confirmations: set[str] = set()
        self._admissions: dict[str, AdmissionRecord] = {}
        self._epoch = 0
        if self.store is not None:
            self._restore()

    @staticmethod
    def _policy_payload(policy: AuthorityPolicy) -> dict[str, Any]:
        return {
            "policy_id": policy.policy_id,
            "account_id": policy.account_id,
            "environments": sorted(policy.environments),
            "instruments": sorted(policy.instruments),
            "actions": sorted(policy.actions),
            "max_notional": str(policy.max_notional),
            "expires_at": policy.expires_at,
            "autonomous": policy.autonomous,
            "protection_only": policy.protection_only,
        }

    @staticmethod
    def _confirmation_payload(confirmation: Confirmation) -> dict[str, Any]:
        return {
            "confirmation_id": confirmation.confirmation_id,
            "policy_id": confirmation.policy_id,
            "intent_hash": confirmation.intent_hash,
            "account_id": confirmation.account_id,
            "environment": confirmation.environment,
            "instrument": confirmation.instrument,
            "action": confirmation.action,
            "notional": str(confirmation.notional),
            "expires_at": confirmation.expires_at,
        }

    @staticmethod
    def _admission_payload(record: AdmissionRecord) -> dict[str, Any]:
        return {
            "admission_id": record.admission_id,
            "policy_id": record.policy_id,
            "intent_hash": record.intent_hash,
            "account_id": record.account_id,
            "environment": record.environment,
            "instrument": record.instrument,
            "action": record.action,
            "notional": str(record.notional),
            "risk_reducing": record.risk_reducing,
            "state_version": record.state_version,
            "authority_epoch": record.authority_epoch,
            "outcome": record.outcome,
            "admitted_at": record.admitted_at,
            "confirmation_id": record.confirmation_id,
            "reason": record.reason,
            "request_fingerprint": record.request_fingerprint,
        }

    def _persist(self, event_type: str, key: str, payload: dict[str, Any], *, committed_at: str) -> None:
        if self.store is None:
            return
        event_id = _authority_event_id(event_type, key)
        existing = self.store.get_event(event_id)
        if existing is not None:
            if existing["event_type"] != event_type or existing["payload"] != payload:
                raise AuthorityConflict("durable authority event conflicts with existing content")
            return
        envelope = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": "authority_state",
            "aggregate_id": "canonical",
            "aggregate_version": str(self.store.next_aggregate_version("authority_state", "canonical")),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": committed_at,
        }
        try:
            self.store.append_event(envelope)
        except ValueError:
            existing = self.store.get_event(event_id)
            if existing is not None and existing["event_type"] == event_type and existing["payload"] == payload:
                return
            raise

    def _restore(self) -> None:
        assert self.store is not None
        for event in self.store.load_events("authority_state", "canonical"):
            payload = event["payload"]
            event_type = event["event_type"]
            if event_type == "AuthorityPolicyRegistered":
                policy = AuthorityPolicy.create(
                    policy_id=payload["policy_id"],
                    account_id=payload["account_id"],
                    environments=payload["environments"],
                    instruments=payload["instruments"],
                    actions=payload["actions"],
                    max_notional=payload["max_notional"],
                    expires_at=payload["expires_at"],
                    autonomous=payload["autonomous"],
                    protection_only=payload["protection_only"],
                )
                existing = self._policies.get(policy.policy_id)
                if existing is not None and existing != policy:
                    raise AuthorityConflict("durable policy history conflicts")
                if existing is None:
                    self._policies[policy.policy_id] = policy
                    self._epoch += 1
            elif event_type == "AuthorityPolicyRevoked":
                value = (payload["reason"], payload["revoked_at"])
                existing = self._revocations.get(payload["policy_id"])
                if existing is not None and existing != value:
                    raise AuthorityConflict("durable revocation history conflicts")
                if existing is None:
                    self._revocations[payload["policy_id"]] = value
                    self._epoch += 1
            elif event_type == "AuthorityConfirmationAdded":
                confirmation = Confirmation(
                    confirmation_id=payload["confirmation_id"],
                    policy_id=payload["policy_id"],
                    intent_hash=payload["intent_hash"],
                    account_id=payload["account_id"],
                    environment=payload["environment"],
                    instrument=payload["instrument"],
                    action=payload["action"],
                    notional=_decimal(payload["notional"], name="notional"),
                    expires_at=payload["expires_at"],
                )
                existing = self._confirmations.get(confirmation.confirmation_id)
                if existing is not None and existing != confirmation:
                    raise AuthorityConflict("durable confirmation history conflicts")
                self._confirmations[confirmation.confirmation_id] = confirmation
            elif event_type == "AuthorityAdmissionRecorded":
                record = AdmissionRecord(
                    admission_id=payload["admission_id"],
                    policy_id=payload["policy_id"],
                    intent_hash=payload["intent_hash"],
                    account_id=payload["account_id"],
                    environment=payload["environment"],
                    instrument=payload["instrument"],
                    action=payload["action"],
                    notional=_decimal(payload["notional"], name="notional"),
                    risk_reducing=payload["risk_reducing"],
                    state_version=payload["state_version"],
                    authority_epoch=payload["authority_epoch"],
                    outcome=payload["outcome"],
                    admitted_at=payload["admitted_at"],
                    confirmation_id=payload["confirmation_id"],
                    reason=payload["reason"],
                    request_fingerprint=payload["request_fingerprint"],
                )
                existing = self._admissions.get(record.admission_id)
                if existing is not None and existing != record:
                    raise AuthorityConflict("durable admission history conflicts")
                self._admissions[record.admission_id] = record
                if record.outcome == "ADMITTED" and record.confirmation_id is not None:
                    self._used_confirmations.add(record.confirmation_id)
            else:
                raise AuthorityConflict(f"unknown durable authority event: {event_type}")

    @property
    def epoch(self) -> int:
        return self._epoch

    def register_policy(self, policy: AuthorityPolicy) -> bool:
        existing = self._policies.get(policy.policy_id)
        if existing is not None:
            if existing != policy:
                raise AuthorityConflict("policy_id already has different content")
            return False
        self._persist(
            "AuthorityPolicyRegistered",
            policy.policy_id,
            self._policy_payload(policy),
            committed_at=datetime.now(timezone.utc).isoformat(),
        )
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
        self._persist(
            "AuthorityPolicyRevoked",
            pid,
            {"policy_id": pid, "reason": normalized[0], "revoked_at": normalized[1]},
            committed_at=normalized[1],
        )
        self._revocations[pid] = normalized
        self._epoch += 1
        return True

    def add_confirmation(
        self,
        *,
        confirmation_id: str,
        policy_id: str,
        intent_hash: str,
        account_id: str,
        environment: str,
        instrument: str,
        action: str,
        notional,
        expires_at: str,
    ) -> bool:
        cid = _text(confirmation_id, name="confirmation_id")
        pid = _text(policy_id, name="policy_id")
        if pid not in self._policies:
            raise KeyError(pid)
        confirmation_notional = _decimal(notional, name="notional")
        if confirmation_notional < 0:
            raise ValueError("notional must be non-negative")
        confirmation = Confirmation(
            confirmation_id=cid,
            policy_id=pid,
            intent_hash=_text(intent_hash, name="intent_hash"),
            account_id=_text(account_id, name="account_id"),
            environment=_text(environment, name="environment").upper(),
            instrument=_text(instrument, name="instrument"),
            action=_text(action, name="action").upper(),
            notional=confirmation_notional,
            expires_at=expires_at,
        )
        _instant(expires_at, name="expires_at")
        existing = self._confirmations.get(cid)
        if existing is not None:
            if existing != confirmation:
                raise AuthorityConflict("confirmation_id already has different content")
            return False
        self._persist(
            "AuthorityConfirmationAdded",
            cid,
            self._confirmation_payload(confirmation),
            committed_at=datetime.now(timezone.utc).isoformat(),
        )
        self._confirmations[cid] = confirmation
        return True

    def _policy_active(self, policy: AuthorityPolicy, now: str) -> tuple[bool, str]:
        current = _instant(now, name="now")
        revocation = self._revocations.get(policy.policy_id)
        if revocation is not None:
            _, revoked_at = revocation
            if current >= _instant(revoked_at, name="revoked_at"):
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
        account = _text(account_id, name="account_id")
        env = _text(environment, name="environment").upper()
        symbol = _text(instrument, name="instrument")
        normalized_action = _text(action, name="action").upper()
        if not isinstance(state_version, int) or isinstance(state_version, bool) or state_version < 0:
            raise ValueError("state_version must be a non-negative integer")
        if not isinstance(risk_admitted, bool) or not isinstance(risk_reducing, bool):
            raise TypeError("risk_admitted and risk_reducing must be booleans")
        policy = self._policies.get(pid)
        if policy is None:
            raise KeyError(pid)
        amount = _decimal(notional, name="notional")
        if amount < 0:
            raise ValueError("notional must be non-negative")
        request_payload = {
            "policy_id": pid,
            "intent_hash": ihash,
            "account_id": account,
            "environment": env,
            "instrument": symbol,
            "action": normalized_action,
            "notional": str(amount),
            "state_version": state_version,
            "risk_admitted": risk_admitted,
            "confirmation_id": confirmation_id,
            "risk_reducing": risk_reducing,
        }
        request_fingerprint = sha256(
            json.dumps(request_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        existing_admission = self._admissions.get(aid)
        if existing_admission is not None:
            if existing_admission.request_fingerprint != request_fingerprint:
                raise AuthorityConflict("admission_id already has different request content")
            return existing_admission

        active, reason = self._policy_active(policy, now)
        checks = [
            (active, reason),
            (risk_admitted, "risk_rejected"),
            (account == policy.account_id, "account_out_of_scope"),
            (env in policy.environments, "environment_out_of_scope"),
            (symbol in policy.instruments, "instrument_out_of_scope"),
            (normalized_action in policy.actions, "action_out_of_scope"),
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
                elif (
                    confirmation.account_id != account
                    or confirmation.environment != env
                    or confirmation.instrument != symbol
                    or confirmation.action != normalized_action
                    or confirmation.notional != amount
                ):
                    outcome, failure_reason = "REJECTED", "confirmation_scope_mismatch"
                elif _instant(now, name="now") >= _instant(confirmation.expires_at, name="confirmation.expires_at"):
                    outcome, failure_reason = "REJECTED", "confirmation_expired"
                else:
                    used_confirmation = confirmation.confirmation_id

        record = AdmissionRecord(
            admission_id=aid,
            policy_id=pid,
            intent_hash=ihash,
            account_id=account,
            environment=env,
            instrument=symbol,
            action=normalized_action,
            notional=amount,
            risk_reducing=risk_reducing,
            state_version=state_version,
            authority_epoch=self._epoch,
            outcome=outcome,
            admitted_at=now,
            confirmation_id=used_confirmation,
            reason=failure_reason,
            request_fingerprint=request_fingerprint,
        )
        self._persist(
            "AuthorityAdmissionRecorded",
            aid,
            self._admission_payload(record),
            committed_at=now,
        )
        self._admissions[aid] = record
        if outcome == "ADMITTED" and used_confirmation is not None:
            self._used_confirmations.add(used_confirmation)
        return record

    def dispatch_allowed(
        self,
        admission_id: str,
        *,
        intent_hash: str,
        account_id: str,
        environment: str,
        instrument: str,
        action: str,
        now: str,
    ) -> tuple[bool, str]:
        record = self._admissions.get(_text(admission_id, name="admission_id"))
        if record is None:
            return False, "admission_missing"
        if record.outcome != "ADMITTED":
            return False, "admission_not_admitted"
        if record.intent_hash != _text(intent_hash, name="intent_hash"):
            return False, "intent_hash_changed"
        scope = (
            _text(account_id, name="account_id"),
            _text(environment, name="environment").upper(),
            _text(instrument, name="instrument"),
            _text(action, name="action").upper(),
        )
        recorded_scope = (
            record.account_id,
            record.environment,
            record.instrument,
            record.action,
        )
        if scope != recorded_scope:
            return False, "admission_scope_changed"
        policy = self._policies[record.policy_id]
        active, reason = self._policy_active(policy, now)
        if not active:
            return False, reason
        if record.confirmation_id is not None:
            confirmation = self._confirmations[record.confirmation_id]
            if _instant(now, name="now") >= _instant(confirmation.expires_at, name="confirmation.expires_at"):
                return False, "confirmation_expired"
        return True, "allowed"

    def dispatch_guard(
        self,
        admission_id: str,
        *,
        account_id: str,
        environment: str,
        instrument: str,
        action: str,
    ) -> Callable[[str, str], tuple[bool, str]]:
        """Bind one admitted scope to GuardedDispatcher's two-phase barrier."""
        aid = _text(admission_id, name="admission_id")
        account = _text(account_id, name="account_id")
        env = _text(environment, name="environment").upper()
        symbol = _text(instrument, name="instrument")
        normalized_action = _text(action, name="action").upper()

        def check(intent_hash: str, now: str) -> tuple[bool, str]:
            return self.dispatch_allowed(
                aid,
                intent_hash=intent_hash,
                account_id=account,
                environment=env,
                instrument=symbol,
                action=normalized_action,
                now=now,
            )

        return check
