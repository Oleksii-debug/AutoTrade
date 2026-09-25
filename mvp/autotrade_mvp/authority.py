"""Deterministic authority and confirmation gates for AutoTrade."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any, Callable, FrozenSet
from uuid import NAMESPACE_URL, UUID, uuid5

from .durable_reservations import DurableReservationBook
from .persistence import JournalStore, canonical_json, payload_digest
from .risk import RiskDecision, risk_decision_fingerprint, validate_bound_risk_decision


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


@dataclass(frozen=True, order=True)
class InstrumentVersionIdentity:
    """Immutable authority reference to one canonical instrument version."""

    instrument_id: str
    version: int

    def __post_init__(self) -> None:
        raw_id = _text(self.instrument_id, name="instrument_id")
        try:
            canonical_id = str(UUID(raw_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("instrument_id must be a UUID") from error
        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version < 1
        ):
            raise ValueError("instrument_version must be a positive integer")
        object.__setattr__(self, "instrument_id", canonical_id)


def _instrument_identity(value, *, name: str = "instrument") -> InstrumentVersionIdentity:
    if isinstance(value, InstrumentVersionIdentity):
        return value
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return InstrumentVersionIdentity(value[0], value[1])
    raise TypeError(
        f"{name} must be InstrumentVersionIdentity or an (instrument_id, version) pair"
    )


@dataclass(frozen=True)
class AuthorityPolicy:
    policy_id: str
    account_id: str
    environments: FrozenSet[str]
    instruments: FrozenSet[InstrumentVersionIdentity]
    actions: FrozenSet[str]
    max_notional: Decimal
    expires_at: str
    autonomous: bool
    valid_from: str = "1970-01-01T00:00:00Z"
    protection_only: bool = False
    version: int = 1

    def __post_init__(self) -> None:
        # The policy object itself is an authority boundary.  Callers can
        # instantiate dataclasses directly, so validation cannot live only in
        # create(); otherwise truthy non-booleans such as "false" could bypass
        # the confirmation requirement through policy.autonomous.
        if not isinstance(self.autonomous, bool) or not isinstance(
            self.protection_only, bool
        ):
            raise TypeError("autonomous and protection_only must be booleans")
        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version < 1
        ):
            raise ValueError("authority policy version must be a positive integer")
        if isinstance(self.environments, (str, bytes)):
            raise TypeError("environments must be a collection")
        if isinstance(self.instruments, (str, bytes)):
            raise TypeError("instruments must be a collection")
        if isinstance(self.actions, (str, bytes)):
            raise TypeError("actions must be a collection")

        normalized_environments = frozenset(
            _text(item, name="environment").upper() for item in self.environments
        )
        if (
            not normalized_environments
            or not normalized_environments <= {"SIMULATION", "PAPER", "LIVE"}
        ):
            raise ValueError("environments must contain supported values")
        normalized_instruments = frozenset(
            _instrument_identity(item) for item in self.instruments
        )
        normalized_actions = frozenset(
            _text(item, name="action").upper() for item in self.actions
        )
        if not normalized_instruments or not normalized_actions:
            raise ValueError("instruments and actions must be non-empty")

        notional = _decimal(self.max_notional, name="max_notional")
        if notional <= 0:
            raise ValueError("max_notional must be positive")
        valid_from = _text(self.valid_from, name="valid_from")
        expires_at = _text(self.expires_at, name="expires_at")
        if _instant(valid_from, name="valid_from") >= _instant(
            expires_at, name="expires_at"
        ):
            raise ValueError("valid_from must precede expires_at")

        object.__setattr__(self, "policy_id", _text(self.policy_id, name="policy_id"))
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        object.__setattr__(self, "environments", normalized_environments)
        object.__setattr__(self, "instruments", normalized_instruments)
        object.__setattr__(self, "actions", normalized_actions)
        object.__setattr__(self, "max_notional", notional)
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "expires_at", expires_at)

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
        valid_from: str = "1970-01-01T00:00:00Z",
        protection_only: bool = False,
        version: int = 1,
    ) -> "AuthorityPolicy":
        normalized_environments = frozenset(
            _text(item, name="environment").upper() for item in environments
        )
        if not normalized_environments or not normalized_environments <= {"SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("environments must contain supported values")
        normalized_instruments = frozenset(
            _instrument_identity(item) for item in instruments
        )
        normalized_actions = frozenset(
            _text(item, name="action").upper() for item in actions
        )
        if not normalized_instruments or not normalized_actions:
            raise ValueError("instruments and actions must be non-empty")
        notional = _decimal(max_notional, name="max_notional")
        if notional <= 0:
            raise ValueError("max_notional must be positive")
        valid_from_instant = _instant(valid_from, name="valid_from")
        expires_at_instant = _instant(expires_at, name="expires_at")
        if valid_from_instant >= expires_at_instant:
            raise ValueError("valid_from must precede expires_at")
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
            valid_from=valid_from,
            protection_only=protection_only,
            version=version,
        )


@dataclass(frozen=True)
class Confirmation:
    confirmation_id: str
    policy_id: str
    intent_hash: str
    account_id: str
    environment: str
    instrument_version: InstrumentVersionIdentity
    action: str
    notional: Decimal
    expires_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "confirmation_id", _text(self.confirmation_id, name="confirmation_id")
        )
        object.__setattr__(self, "policy_id", _text(self.policy_id, name="policy_id"))
        object.__setattr__(
            self, "intent_hash", _text(self.intent_hash, name="intent_hash")
        )
        object.__setattr__(
            self, "account_id", _text(self.account_id, name="account_id")
        )
        environment = _text(self.environment, name="environment").upper()
        if environment not in {"SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("confirmation environment is unsupported")
        object.__setattr__(self, "environment", environment)
        object.__setattr__(
            self,
            "instrument_version",
            _instrument_identity(self.instrument_version, name="instrument_version"),
        )
        object.__setattr__(self, "action", _text(self.action, name="action").upper())
        notional = _decimal(self.notional, name="notional")
        if notional < 0:
            raise ValueError("confirmation notional must be non-negative")
        object.__setattr__(self, "notional", notional)
        expires_at = _text(self.expires_at, name="expires_at")
        _instant(expires_at, name="confirmation.expires_at")
        object.__setattr__(self, "expires_at", expires_at)


@dataclass(frozen=True)
class AdmissionRecord:
    admission_id: str
    policy_id: str
    intent_hash: str
    account_id: str
    environment: str
    instrument_version: InstrumentVersionIdentity
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

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "admission_id", _text(self.admission_id, name="admission_id")
        )
        object.__setattr__(self, "policy_id", _text(self.policy_id, name="policy_id"))
        object.__setattr__(
            self, "intent_hash", _text(self.intent_hash, name="intent_hash")
        )
        object.__setattr__(
            self, "account_id", _text(self.account_id, name="account_id")
        )
        environment = _text(self.environment, name="environment").upper()
        if environment not in {"SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("admission environment is unsupported")
        object.__setattr__(self, "environment", environment)
        object.__setattr__(
            self,
            "instrument_version",
            _instrument_identity(self.instrument_version, name="instrument_version"),
        )
        object.__setattr__(self, "action", _text(self.action, name="action").upper())
        notional = _decimal(self.notional, name="notional")
        if notional < 0:
            raise ValueError("admission notional must be non-negative")
        object.__setattr__(self, "notional", notional)
        if not isinstance(self.risk_reducing, bool):
            raise TypeError("admission risk_reducing must be boolean")
        if (
            not isinstance(self.state_version, int)
            or isinstance(self.state_version, bool)
            or self.state_version < 0
        ):
            raise ValueError("admission state_version is invalid")
        if (
            not isinstance(self.authority_epoch, int)
            or isinstance(self.authority_epoch, bool)
            or self.authority_epoch < 0
        ):
            raise ValueError("admission authority_epoch is invalid")
        outcome = _text(self.outcome, name="outcome").upper()
        if outcome not in {"ADMITTED", "REJECTED"}:
            raise ValueError("admission outcome is invalid")
        object.__setattr__(self, "outcome", outcome)
        admitted_at = _text(self.admitted_at, name="admitted_at")
        _instant(admitted_at, name="admitted_at")
        object.__setattr__(self, "admitted_at", admitted_at)
        if self.confirmation_id is not None:
            confirmation_id = _text(self.confirmation_id, name="confirmation_id")
            if outcome != "ADMITTED":
                raise ValueError("rejected admission cannot consume confirmation")
            object.__setattr__(self, "confirmation_id", confirmation_id)
        object.__setattr__(self, "reason", _text(self.reason, name="reason"))
        fingerprint = _text(
            self.request_fingerprint, name="request_fingerprint"
        ).lower()
        if len(fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in fingerprint
        ):
            raise ValueError("request_fingerprint must be a SHA-256 hex digest")
        object.__setattr__(self, "request_fingerprint", fingerprint)


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
        # Last authority aggregate version this process has actually replayed
        # or committed. This is deliberately separate from authority epoch:
        # confirmations/admissions advance the journal even when they do not
        # change the policy/revocation epoch.
        self._journal_version = 0
        if self.store is not None:
            self._restore_journal()

    @staticmethod
    def _instrument_payload(value: InstrumentVersionIdentity) -> dict[str, Any]:
        return {"instrument_id": value.instrument_id, "version": value.version}

    @classmethod
    def _policy_payload(cls, policy: AuthorityPolicy) -> dict[str, Any]:
        return {
            "policy_id": policy.policy_id,
            "account_id": policy.account_id,
            "environments": sorted(policy.environments),
            "instruments": [cls._instrument_payload(item) for item in sorted(policy.instruments)],
            "actions": sorted(policy.actions),
            "max_notional": str(policy.max_notional),
            "expires_at": policy.expires_at,
            "autonomous": policy.autonomous,
            "valid_from": policy.valid_from,
            "protection_only": policy.protection_only,
            "version": policy.version,
        }

    @classmethod
    def _confirmation_payload(cls, confirmation: Confirmation) -> dict[str, Any]:
        return {
            "confirmation_id": confirmation.confirmation_id,
            "policy_id": confirmation.policy_id,
            "intent_hash": confirmation.intent_hash,
            "account_id": confirmation.account_id,
            "environment": confirmation.environment,
            "instrument": cls._instrument_payload(confirmation.instrument_version),
            "action": confirmation.action,
            "notional": str(confirmation.notional),
            "expires_at": confirmation.expires_at,
        }

    @classmethod
    def _admission_payload(cls, record: AdmissionRecord) -> dict[str, Any]:
        return {
            "admission_id": record.admission_id,
            "policy_id": record.policy_id,
            "intent_hash": record.intent_hash,
            "account_id": record.account_id,
            "environment": record.environment,
            "instrument": cls._instrument_payload(record.instrument_version),
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

        # Compare-and-append from this process's observed journal position.
        # Reading "next version" from the shared DB here would let a stale
        # AuthorityService silently append after another process and make
        # decisions from obsolete confirmation/revocation state.
        durable_next = self.store.next_aggregate_version(
            "authority_state", "canonical"
        )
        durable_version = durable_next - 1
        if durable_version != self._journal_version:
            raise AuthorityConflict(
                "durable authority journal advanced; reload required"
            )

        event_id = _authority_event_id(event_type, key)
        existing = self.store.get_event(event_id)
        if existing is not None:
            if existing["event_type"] != event_type or existing["payload"] != payload:
                raise AuthorityConflict("durable authority event conflicts with existing content")
            # If our observed version matches durable state, this event was
            # already part of our replay. Callers should have handled the
            # corresponding in-memory idempotency path before reaching here.
            return
        envelope = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": "authority_state",
            "aggregate_id": "canonical",
            "aggregate_version": str(self._journal_version + 1),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": committed_at,
        }
        try:
            result = self.store.append_event(envelope)
        except ValueError as error:
            # A concurrent writer may have won after the version check but
            # before our append. Never reinterpret that race as idempotency:
            # the caller must reload and re-evaluate authority state.
            existing = self.store.get_event(event_id)
            if (
                existing is not None
                and existing["event_type"] == event_type
                and existing["payload"] == payload
                and self.store.next_aggregate_version(
                    "authority_state", "canonical"
                ) - 1 == self._journal_version
            ):
                return
            raise AuthorityConflict(
                "durable authority journal changed concurrently; reload required"
            ) from error
        if result.inserted:
            self._journal_version += 1

    def _restore_journal(self) -> None:
        assert self.store is not None
        for event in self.store.load_events("authority_state", "canonical"):
            payload = event["payload"]
            event_type = event["event_type"]
            event_version = int(event["aggregate_version"])
            if event_version != self._journal_version + 1:
                raise AuthorityConflict(
                    "durable authority journal version sequence is invalid"
                )
            if event_type == "AuthorityPolicyRegistered":
                instruments = payload.get("instruments")
                if not isinstance(instruments, list):
                    raise AuthorityConflict("durable policy instruments malformed")
                policy = AuthorityPolicy.create(
                    policy_id=payload["policy_id"],
                    account_id=payload["account_id"],
                    environments=payload["environments"],
                    instruments=[
                        InstrumentVersionIdentity(item["instrument_id"], item["version"])
                        for item in instruments
                        if isinstance(item, dict)
                    ],
                    actions=payload["actions"],
                    max_notional=payload["max_notional"],
                    expires_at=payload["expires_at"],
                    autonomous=payload["autonomous"],
                    valid_from=payload["valid_from"],
                    protection_only=payload["protection_only"],
                    version=payload.get("version", 1),
                )
                if len(policy.instruments) != len(instruments):
                    raise AuthorityConflict("durable policy instrument entry malformed")
                existing = self._policies.get(policy.policy_id)
                if existing is not None and existing != policy:
                    raise AuthorityConflict("durable policy history conflicts")
                if existing is None:
                    self._policies[policy.policy_id] = policy
                    self._epoch += 1
            elif event_type == "AuthorityPolicyRevoked":
                policy_id = _text(payload.get("policy_id"), name="policy_id")
                if policy_id not in self._policies:
                    raise AuthorityConflict("durable revocation references missing policy")
                reason = _text(payload.get("reason"), name="reason")
                revoked_at = _text(payload.get("revoked_at"), name="revoked_at")
                _instant(revoked_at, name="revoked_at")
                value = (reason, revoked_at)
                existing = self._revocations.get(policy_id)
                if existing is not None and existing != value:
                    raise AuthorityConflict("durable revocation history conflicts")
                if existing is None:
                    self._revocations[policy_id] = value
                    self._epoch += 1
            elif event_type == "AuthorityConfirmationAdded":
                instrument = payload.get("instrument")
                if not isinstance(instrument, dict):
                    raise AuthorityConflict("durable confirmation instrument malformed")
                confirmation = Confirmation(
                    confirmation_id=payload["confirmation_id"],
                    policy_id=payload["policy_id"],
                    intent_hash=payload["intent_hash"],
                    account_id=payload["account_id"],
                    environment=payload["environment"],
                    instrument_version=InstrumentVersionIdentity(
                        instrument["instrument_id"], instrument["version"]
                    ),
                    action=payload["action"],
                    notional=_decimal(payload["notional"], name="notional"),
                    expires_at=payload["expires_at"],
                )
                if confirmation.policy_id not in self._policies:
                    raise AuthorityConflict("durable confirmation references missing policy")
                existing = self._confirmations.get(confirmation.confirmation_id)
                if existing is not None and existing != confirmation:
                    raise AuthorityConflict("durable confirmation history conflicts")
                self._confirmations[confirmation.confirmation_id] = confirmation
            elif event_type == "AuthorityAdmissionRecorded":
                instrument = payload.get("instrument")
                if not isinstance(instrument, dict):
                    raise AuthorityConflict("durable admission instrument malformed")
                record = AdmissionRecord(
                    admission_id=payload["admission_id"],
                    policy_id=payload["policy_id"],
                    intent_hash=payload["intent_hash"],
                    account_id=payload["account_id"],
                    environment=payload["environment"],
                    instrument_version=InstrumentVersionIdentity(
                        instrument["instrument_id"], instrument["version"]
                    ),
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
                if record.policy_id not in self._policies:
                    raise AuthorityConflict("durable admission references missing policy")
                policy = self._policies[record.policy_id]
                if record.authority_epoch != self._epoch:
                    raise AuthorityConflict(
                        "durable admission authority epoch does not match replay state"
                    )
                if record.outcome == "ADMITTED":
                    active, _ = self._policy_active(policy, record.admitted_at)
                    scope_valid = (
                        active
                        and record.account_id == policy.account_id
                        and record.environment in policy.environments
                        and record.instrument_version in policy.instruments
                        and record.action in policy.actions
                        and record.notional <= policy.max_notional
                        and (not policy.protection_only or record.risk_reducing)
                    )
                    if not scope_valid:
                        raise AuthorityConflict(
                            "durable admitted record violates policy scope"
                        )
                    if not policy.autonomous and record.confirmation_id is None:
                        raise AuthorityConflict(
                            "durable admitted record is missing required confirmation"
                        )
                    request_payload = {
                        "policy_id": record.policy_id,
                        "intent_hash": record.intent_hash,
                        "account_id": record.account_id,
                        "environment": record.environment,
                        "instrument_id": record.instrument_version.instrument_id,
                        "instrument_version": record.instrument_version.version,
                        "action": record.action,
                        "notional": str(record.notional),
                        "state_version": record.state_version,
                        "risk_admitted": True,
                        "confirmation_id": record.confirmation_id,
                        "risk_reducing": record.risk_reducing,
                    }
                    expected_fingerprint = sha256(
                        json.dumps(
                            request_payload,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    if record.request_fingerprint != expected_fingerprint:
                        raise AuthorityConflict(
                            "durable admitted record fingerprint is inconsistent"
                        )
                if record.confirmation_id is not None:
                    if record.confirmation_id not in self._confirmations:
                        raise AuthorityConflict(
                            "durable admission references missing confirmation"
                        )
                    confirmation = self._confirmations[record.confirmation_id]
                    if (
                        confirmation.policy_id != record.policy_id
                        or confirmation.intent_hash != record.intent_hash
                        or confirmation.account_id != record.account_id
                        or confirmation.environment != record.environment
                        or confirmation.instrument_version != record.instrument_version
                        or confirmation.action != record.action
                        or confirmation.notional != record.notional
                        or _instant(record.admitted_at, name="admitted_at")
                        >= _instant(
                            confirmation.expires_at,
                            name="confirmation.expires_at",
                        )
                    ):
                        raise AuthorityConflict(
                            "durable admission does not match confirmation scope"
                        )
                    if record.confirmation_id in self._used_confirmations:
                        raise AuthorityConflict(
                            "durable confirmation was consumed by multiple admissions"
                        )
                existing = self._admissions.get(record.admission_id)
                if existing is not None and existing != record:
                    raise AuthorityConflict("durable admission history conflicts")
                self._admissions[record.admission_id] = record
                if record.outcome == "ADMITTED" and record.confirmation_id is not None:
                    self._used_confirmations.add(record.confirmation_id)
            else:
                raise AuthorityConflict(f"unknown durable authority event: {event_type}")
            self._journal_version = event_version

    @property
    def epoch(self) -> int:
        return self._epoch

    def register_policy(self, policy: AuthorityPolicy) -> bool:
        if not isinstance(policy, AuthorityPolicy):
            raise TypeError("policy must be AuthorityPolicy")
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
        instrument_id: str,
        instrument_version: int,
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
            instrument_version=InstrumentVersionIdentity(
                instrument_id, instrument_version
            ),
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
        if current < _instant(policy.valid_from, name="policy.valid_from"):
            return False, "policy_not_yet_active"
        revocation = self._revocations.get(policy.policy_id)
        if revocation is not None:
            _, revoked_at = revocation
            if current >= _instant(revoked_at, name="revoked_at"):
                return False, "policy_revoked"
        if current >= _instant(policy.expires_at, name="policy.expires_at"):
            return False, "policy_expired"
        return True, "active"

    def _admit_unverified(
        self,
        *,
        admission_id: str,
        policy_id: str,
        intent_hash: str,
        account_id: str,
        environment: str,
        instrument_id: str,
        instrument_version: int,
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
        identity = InstrumentVersionIdentity(instrument_id, instrument_version)
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
            "instrument_id": identity.instrument_id,
            "instrument_version": identity.version,
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
            (identity in policy.instruments, "instrument_version_out_of_scope"),
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
                    or confirmation.instrument_version != identity
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
            instrument_version=identity,
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

    def _durable_authority_state_current(self) -> bool:
        if self.store is None:
            return True
        durable_version = (
            self.store.next_aggregate_version("authority_state", "canonical") - 1
        )
        return durable_version == self._journal_version

    def dispatch_allowed(
        self,
        admission_id: str,
        *,
        intent_hash: str,
        account_id: str,
        environment: str,
        instrument_id: str,
        instrument_version: int,
        action: str,
        now: str,
    ) -> tuple[bool, str]:
        record = self._admissions.get(_text(admission_id, name="admission_id"))
        if record is None:
            return False, "admission_missing"
        if record.outcome != "ADMITTED":
            return False, "admission_not_admitted"
        if not self._durable_authority_state_current():
            return False, "authority_state_stale"
        if record.intent_hash != _text(intent_hash, name="intent_hash"):
            return False, "intent_hash_changed"
        scope = (
            _text(account_id, name="account_id"),
            _text(environment, name="environment").upper(),
            InstrumentVersionIdentity(instrument_id, instrument_version),
            _text(action, name="action").upper(),
        )
        recorded_scope = (
            record.account_id,
            record.environment,
            record.instrument_version,
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
        if not self._durable_authority_state_current():
            return False, "authority_state_stale"
        return True, "allowed"

    def dispatch_guard(
        self,
        admission_id: str,
        *,
        account_id: str,
        environment: str,
        instrument_id: str,
        instrument_version: int,
        action: str,
    ) -> Callable[[str, str], tuple[bool, str]]:
        """Bind one admitted versioned scope to the dispatcher's final barrier."""
        aid = _text(admission_id, name="admission_id")
        account = _text(account_id, name="account_id")
        env = _text(environment, name="environment").upper()
        identity = InstrumentVersionIdentity(instrument_id, instrument_version)
        normalized_action = _text(action, name="action").upper()

        def check(intent_hash: str, now: str) -> tuple[bool, str]:
            return self.dispatch_allowed(
                aid,
                intent_hash=intent_hash,
                account_id=account,
                environment=env,
                instrument_id=identity.instrument_id,
                instrument_version=identity.version,
                action=normalized_action,
                now=now,
            )

        return check


    def export_state(self) -> dict:
        """Return a canonical JSON-compatible snapshot of authority state.

        This is persistence data, not a cryptographic trust boundary.  The
        durable journal is responsible for integrity and ordering; restore()
        revalidates the financial authority identities and references.
        """
        def instrument(value: InstrumentVersionIdentity) -> dict:
            return {"instrument_id": value.instrument_id, "version": value.version}

        policies = []
        for policy_id in sorted(self._policies):
            policy = self._policies[policy_id]
            policies.append(
                {
                    "policy_id": policy.policy_id,
                    "account_id": policy.account_id,
                    "environments": sorted(policy.environments),
                    "instruments": [
                        instrument(item) for item in sorted(policy.instruments)
                    ],
                    "actions": sorted(policy.actions),
                    "max_notional": str(policy.max_notional),
                    "expires_at": policy.expires_at,
                    "autonomous": policy.autonomous,
                    "valid_from": policy.valid_from,
                    "protection_only": policy.protection_only,
                    "version": policy.version,
                }
            )

        confirmations = []
        for confirmation_id in sorted(self._confirmations):
            confirmation = self._confirmations[confirmation_id]
            confirmations.append(
                {
                    "confirmation_id": confirmation.confirmation_id,
                    "policy_id": confirmation.policy_id,
                    "intent_hash": confirmation.intent_hash,
                    "account_id": confirmation.account_id,
                    "environment": confirmation.environment,
                    "instrument": instrument(confirmation.instrument_version),
                    "action": confirmation.action,
                    "notional": str(confirmation.notional),
                    "expires_at": confirmation.expires_at,
                }
            )

        admissions = []
        for admission_id in sorted(self._admissions):
            record = self._admissions[admission_id]
            admissions.append(
                {
                    "admission_id": record.admission_id,
                    "policy_id": record.policy_id,
                    "intent_hash": record.intent_hash,
                    "account_id": record.account_id,
                    "environment": record.environment,
                    "instrument": instrument(record.instrument_version),
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
            )

        return {
            "schema_version": 1,
            "epoch": self._epoch,
            "policies": policies,
            "revocations": [
                {
                    "policy_id": policy_id,
                    "reason": value[0],
                    "revoked_at": value[1],
                }
                for policy_id, value in sorted(self._revocations.items())
            ],
            "confirmations": confirmations,
            "used_confirmations": sorted(self._used_confirmations),
            "admissions": admissions,
        }

    @classmethod
    def restore(cls, state: dict) -> "AuthorityService":
        """Rebuild authority state from one durable snapshot, fail closed."""
        if not isinstance(state, dict) or state.get("schema_version") != 1:
            raise ValueError("unsupported authority state schema")
        service = cls()

        policies = state.get("policies")
        revocations = state.get("revocations")
        confirmations = state.get("confirmations")
        admissions = state.get("admissions")
        used_confirmations = state.get("used_confirmations")
        if not all(
            isinstance(value, list)
            for value in (
                policies,
                revocations,
                confirmations,
                admissions,
                used_confirmations,
            )
        ):
            raise ValueError("authority state collections must be lists")

        seen_policy_ids: set[str] = set()
        for item in policies:
            if not isinstance(item, dict):
                raise ValueError("policy snapshot entry must be an object")
            instruments = item.get("instruments")
            if not isinstance(instruments, list):
                raise ValueError("policy instruments must be a list")
            policy = AuthorityPolicy.create(
                policy_id=item.get("policy_id"),
                account_id=item.get("account_id"),
                environments=item.get("environments"),
                instruments=[
                    InstrumentVersionIdentity(
                        entry.get("instrument_id"), entry.get("version")
                    )
                    for entry in instruments
                    if isinstance(entry, dict)
                ],
                actions=item.get("actions"),
                max_notional=item.get("max_notional"),
                expires_at=item.get("expires_at"),
                autonomous=item.get("autonomous"),
                valid_from=item.get("valid_from"),
                protection_only=item.get("protection_only"),
                version=item.get("version", 1),
            )
            if policy.policy_id in seen_policy_ids:
                raise AuthorityConflict("duplicate policy in authority snapshot")
            seen_policy_ids.add(policy.policy_id)
            service.register_policy(policy)

        for item in revocations:
            if not isinstance(item, dict):
                raise ValueError("revocation snapshot entry must be an object")
            service.revoke_policy(
                item.get("policy_id"),
                reason=item.get("reason"),
                revoked_at=item.get("revoked_at"),
            )

        seen_confirmation_ids: set[str] = set()
        for item in confirmations:
            if not isinstance(item, dict) or not isinstance(item.get("instrument"), dict):
                raise ValueError("confirmation snapshot entry is invalid")
            instrument = item["instrument"]
            cid = item.get("confirmation_id")
            if cid in seen_confirmation_ids:
                raise AuthorityConflict("duplicate confirmation in authority snapshot")
            seen_confirmation_ids.add(cid)
            service.add_confirmation(
                confirmation_id=cid,
                policy_id=item.get("policy_id"),
                intent_hash=item.get("intent_hash"),
                account_id=item.get("account_id"),
                environment=item.get("environment"),
                instrument_id=instrument.get("instrument_id"),
                instrument_version=instrument.get("version"),
                action=item.get("action"),
                notional=item.get("notional"),
                expires_at=item.get("expires_at"),
            )

        restored_admissions: dict[str, AdmissionRecord] = {}
        derived_used: set[str] = set()
        for item in admissions:
            if not isinstance(item, dict) or not isinstance(item.get("instrument"), dict):
                raise ValueError("admission snapshot entry is invalid")
            admission_id = _text(item.get("admission_id"), name="admission_id")
            if admission_id in restored_admissions:
                raise AuthorityConflict("duplicate admission in authority snapshot")
            policy_id = _text(item.get("policy_id"), name="policy_id")
            if policy_id not in service._policies:
                raise ValueError("admission references missing policy")
            instrument = item["instrument"]
            identity = InstrumentVersionIdentity(
                instrument.get("instrument_id"), instrument.get("version")
            )
            amount = _decimal(item.get("notional"), name="notional")
            if amount < 0:
                raise ValueError("admission notional must be non-negative")
            state_version = item.get("state_version")
            authority_epoch = item.get("authority_epoch")
            risk_reducing = item.get("risk_reducing")
            if (
                not isinstance(state_version, int)
                or isinstance(state_version, bool)
                or state_version < 0
            ):
                raise ValueError("admission state_version is invalid")
            if (
                not isinstance(authority_epoch, int)
                or isinstance(authority_epoch, bool)
                or authority_epoch < 0
                or authority_epoch > service._epoch
            ):
                raise ValueError("admission authority_epoch is invalid")
            if not isinstance(risk_reducing, bool):
                raise TypeError("admission risk_reducing must be boolean")
            outcome = _text(item.get("outcome"), name="outcome").upper()
            if outcome not in {"ADMITTED", "REJECTED"}:
                raise ValueError("admission outcome is invalid")
            admitted_at = _text(item.get("admitted_at"), name="admitted_at")
            _instant(admitted_at, name="admitted_at")
            confirmation_id = item.get("confirmation_id")
            if confirmation_id is not None:
                confirmation_id = _text(
                    confirmation_id, name="confirmation_id"
                )
                if confirmation_id not in service._confirmations:
                    raise ValueError("admission references missing confirmation")
                if outcome != "ADMITTED":
                    raise ValueError("rejected admission cannot consume confirmation")
                derived_used.add(confirmation_id)
            record = AdmissionRecord(
                admission_id=admission_id,
                policy_id=policy_id,
                intent_hash=_text(item.get("intent_hash"), name="intent_hash"),
                account_id=_text(item.get("account_id"), name="account_id"),
                environment=_text(item.get("environment"), name="environment").upper(),
                instrument_version=identity,
                action=_text(item.get("action"), name="action").upper(),
                notional=amount,
                risk_reducing=risk_reducing,
                state_version=state_version,
                authority_epoch=authority_epoch,
                outcome=outcome,
                admitted_at=admitted_at,
                confirmation_id=confirmation_id,
                reason=_text(item.get("reason"), name="reason"),
                request_fingerprint=_text(
                    item.get("request_fingerprint"), name="request_fingerprint"
                ),
            )
            restored_admissions[admission_id] = record

        normalized_used = {
            _text(value, name="used_confirmation")
            for value in used_confirmations
        }
        if normalized_used != derived_used:
            raise ValueError("used confirmation set does not match admitted records")
        if not normalized_used <= set(service._confirmations):
            raise ValueError("used confirmation references missing confirmation")

        epoch = state.get("epoch")
        if (
            not isinstance(epoch, int)
            or isinstance(epoch, bool)
            or epoch != service._epoch
        ):
            raise ValueError("authority epoch does not match durable mutations")
        service._admissions = restored_admissions
        service._used_confirmations = normalized_used
        return service
