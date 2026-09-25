"""Deterministic authority and confirmation gates for AutoTrade."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Any, Callable, FrozenSet, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from research.autotrade_research.artifacts.store import ArtifactStore

from .allocation import (
    EvidenceBoundObjectiveAllocationResult,
    ImmutableAllocationEvidence,
    revalidate_evidence_bound_allocation,
)
from .durable_reservations import DurableReservationBook
from .persistence import JournalStore, canonical_json, payload_digest
from .reconciliation_journal import load_account_resource_availability_evidence
from .securities_borrow import (
    BorrowAvailabilityEvidence,
    DurableBorrowRecallProjection,
    borrow_resource_key,
    incremental_short_borrow_quantity,
)
from .risk import (
    RiskContext,
    _canonical_decimal_text,
    RiskDecision,
    RiskIntent,
    RiskPolicy,
    evaluate_bound_risk,
    normalize_reservation_requirements,
    reservation_requirements_payload,
    risk_decision_fingerprint,
    validate_bound_risk_decision,
)


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
    intent_id: str | None = None
    risk_decision_id: str | None = None
    reservation_id: str | None = None
    capability_snapshot_id: str | None = None
    risk_valid_until: str | None = None
    policy_version: int | None = None
    financial_command_id: str | None = None

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

        evidence_values = (
            self.intent_id,
            self.risk_decision_id,
            self.reservation_id,
            self.capability_snapshot_id,
            self.risk_valid_until,
            self.policy_version,
            self.financial_command_id,
        )
        if any(value is not None for value in evidence_values):
            if outcome == "ADMITTED" and any(value is None for value in evidence_values):
                raise ValueError(
                    "admitted financial record requires complete risk/reservation evidence"
                )
            for field_name, value in (
                ("intent_id", self.intent_id),
                ("risk_decision_id", self.risk_decision_id),
                ("reservation_id", self.reservation_id),
                ("capability_snapshot_id", self.capability_snapshot_id),
                ("risk_valid_until", self.risk_valid_until),
                ("financial_command_id", self.financial_command_id),
            ):
                if value is not None:
                    object.__setattr__(
                        self, field_name, _text(value, name=field_name)
                    )
            if self.risk_valid_until is not None:
                _instant(self.risk_valid_until, name="risk_valid_until")
            if self.policy_version is not None and (
                not isinstance(self.policy_version, int)
                or isinstance(self.policy_version, bool)
                or self.policy_version < 1
            ):
                raise ValueError("admission policy_version must be positive")


class AuthorityConflict(ValueError):
    """Raised when immutable authority identity is reused inconsistently."""


@dataclass(frozen=True)
class AllocationAuthoritySnapshot:
    """Canonical facts used to revalidate an allocation at admission.

    The snapshot is produced by an AuthorityService-owned resolver, never by
    the admission caller. It adapts existing canonical evidence/account stores;
    it is not a second financial authority or persistence layer.
    """

    resolved_evidence: Mapping[str, ImmutableAllocationEvidence]
    provider_id: str
    account_id: str
    policy_version: str
    instrument_versions: Mapping[str, str]
    financial_instruments: Mapping[str, InstrumentVersionIdentity]
    capability_snapshot_ids: Mapping[str, str]
    account_snapshot_id: str
    reconciliation_run_id: str
    account_state_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.resolved_evidence, Mapping):
            raise TypeError("resolved_evidence must be a mapping")
        evidence: dict[str, ImmutableAllocationEvidence] = {}
        for raw_id, item in self.resolved_evidence.items():
            evidence_id = _text(raw_id, name="allocation evidence id")
            if not isinstance(item, ImmutableAllocationEvidence):
                raise TypeError(
                    "resolved allocation evidence values must be ImmutableAllocationEvidence"
                )
            if evidence_id != item.evidence_id:
                raise ValueError(
                    "resolved allocation evidence key must match evidence_id"
                )
            if evidence_id in evidence:
                raise ValueError("resolved allocation evidence ids must be unique")
            evidence[evidence_id] = item

        def normalized_scope(
            raw: Mapping[str, str],
            *,
            name: str,
        ) -> Mapping[str, str]:
            if not isinstance(raw, Mapping):
                raise TypeError(f"{name} must be a mapping")
            result: dict[str, str] = {}
            for raw_symbol, raw_identity in raw.items():
                symbol = _text(raw_symbol, name=f"{name} symbol")
                if symbol in result:
                    raise ValueError(f"{name} symbols must be unique")
                result[symbol] = _text(
                    raw_identity,
                    name=f"{name} identity",
                )
            return MappingProxyType(dict(sorted(result.items())))

        if (
            not isinstance(self.account_state_version, int)
            or isinstance(self.account_state_version, bool)
            or self.account_state_version < 0
        ):
            raise ValueError(
                "allocation account_state_version must be a non-negative integer"
            )
        object.__setattr__(
            self,
            "resolved_evidence",
            MappingProxyType(dict(sorted(evidence.items()))),
        )
        object.__setattr__(
            self,
            "provider_id",
            _text(self.provider_id, name="allocation provider_id"),
        )
        object.__setattr__(
            self,
            "account_id",
            _text(self.account_id, name="allocation account_id"),
        )
        object.__setattr__(
            self,
            "policy_version",
            _text(self.policy_version, name="allocation policy_version"),
        )
        object.__setattr__(
            self,
            "instrument_versions",
            normalized_scope(
                self.instrument_versions,
                name="allocation instrument_versions",
            ),
        )
        if not isinstance(self.financial_instruments, Mapping):
            raise TypeError("financial_instruments must be a mapping")
        normalized_financial_instruments: dict[str, InstrumentVersionIdentity] = {}
        for raw_symbol, raw_identity in self.financial_instruments.items():
            symbol = _text(
                raw_symbol,
                name="allocation financial_instruments symbol",
            )
            if symbol in normalized_financial_instruments:
                raise ValueError(
                    "allocation financial_instruments symbols must be unique"
                )
            normalized_financial_instruments[symbol] = _instrument_identity(
                raw_identity,
                name=f"allocation financial instrument {symbol}",
            )
        object.__setattr__(
            self,
            "financial_instruments",
            MappingProxyType(dict(sorted(normalized_financial_instruments.items()))),
        )
        object.__setattr__(
            self,
            "capability_snapshot_ids",
            normalized_scope(
                self.capability_snapshot_ids,
                name="allocation capability_snapshot_ids",
            ),
        )
        object.__setattr__(
            self,
            "account_snapshot_id",
            _text(
                self.account_snapshot_id,
                name="allocation account_snapshot_id",
            ),
        )
        object.__setattr__(
            self,
            "reconciliation_run_id",
            _text(
                self.reconciliation_run_id,
                name="allocation reconciliation_run_id",
            ),
        )


def _authority_event_id(event_type: str, key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"https://events.autotrade.local/authority/{event_type}/{key}"))


class AuthorityService:
    def __init__(
        self,
        store: JournalStore | None = None,
        *,
        evidence_artifact_store: ArtifactStore | None = None,
        allocation_authority_resolver: Callable[
            [EvidenceBoundObjectiveAllocationResult],
            AllocationAuthoritySnapshot,
        ]
        | None = None,
    ):
        self.store = store
        if (
            evidence_artifact_store is not None
            and not isinstance(evidence_artifact_store, ArtifactStore)
        ):
            raise TypeError("evidence_artifact_store must be ArtifactStore")
        self.evidence_artifact_store = evidence_artifact_store
        if (
            allocation_authority_resolver is not None
            and not callable(allocation_authority_resolver)
        ):
            raise TypeError("allocation_authority_resolver must be callable")
        self.allocation_authority_resolver = allocation_authority_resolver
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
            "max_notional": _canonical_decimal_text(policy.max_notional),
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
            "notional": _canonical_decimal_text(confirmation.notional),
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
            "notional": _canonical_decimal_text(record.notional),
            "risk_reducing": record.risk_reducing,
            "state_version": record.state_version,
            "authority_epoch": record.authority_epoch,
            "outcome": record.outcome,
            "admitted_at": record.admitted_at,
            "confirmation_id": record.confirmation_id,
            "reason": record.reason,
            "request_fingerprint": record.request_fingerprint,
            "intent_id": record.intent_id,
            "risk_decision_id": record.risk_decision_id,
            "reservation_id": record.reservation_id,
            "capability_snapshot_id": record.capability_snapshot_id,
            "risk_valid_until": record.risk_valid_until,
            "policy_version": record.policy_version,
            "financial_command_id": record.financial_command_id,
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
                    intent_id=payload.get("intent_id"),
                    risk_decision_id=payload.get("risk_decision_id"),
                    reservation_id=payload.get("reservation_id"),
                    capability_snapshot_id=payload.get("capability_snapshot_id"),
                    risk_valid_until=payload.get("risk_valid_until"),
                    policy_version=payload.get("policy_version"),
                    financial_command_id=payload.get("financial_command_id"),
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
                    if record.risk_decision_id is None:
                        request_payload = {
                            "policy_id": record.policy_id,
                            "intent_hash": record.intent_hash,
                            "account_id": record.account_id,
                            "environment": record.environment,
                            "instrument_id": record.instrument_version.instrument_id,
                            "instrument_version": record.instrument_version.version,
                            "action": record.action,
                            "notional": _canonical_decimal_text(record.notional),
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
                    else:
                        self._validate_durable_financial_evidence(record, policy)
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

    def _validate_durable_financial_evidence(
        self,
        record: AdmissionRecord,
        policy: AuthorityPolicy,
        *,
        require_transaction_cut: bool = False,
    ) -> None:
        if self.store is None:
            raise AuthorityConflict(
                "durable financial evidence requires a JournalStore"
            )
        required = (
            record.intent_id,
            record.risk_decision_id,
            record.reservation_id,
            record.capability_snapshot_id,
            record.risk_valid_until,
            record.policy_version,
            record.financial_command_id,
        )
        if any(value is None for value in required):
            raise AuthorityConflict(
                "durable admitted financial record has incomplete evidence"
            )
        if record.policy_version != policy.version:
            raise AuthorityConflict(
                "durable admitted record policy version is stale"
            )

        risk_events = self.store.load_events(
            "risk_decision", record.risk_decision_id
        )
        if len(risk_events) != 1 or risk_events[0]["event_type"] != "RiskDecisionRecorded":
            raise AuthorityConflict(
                "durable admission references missing risk decision evidence"
            )
        risk_event = risk_events[0]
        risk_payload = risk_event["payload"]
        if not isinstance(risk_payload, dict):
            raise AuthorityConflict("durable risk decision payload is malformed")
        journal_sequence_cut = risk_payload.get("journal_sequence_cut")
        if journal_sequence_cut is None:
            if require_transaction_cut:
                raise AuthorityConflict(
                    "durable financial admission lacks transaction journal cut"
                )
        else:
            if type(journal_sequence_cut) is not int or journal_sequence_cut < 0:
                raise AuthorityConflict(
                    "durable financial admission journal cut is invalid"
                )
            risk_journal_sequence = risk_event.get("journal_sequence")
            if (
                type(risk_journal_sequence) is not int
                or risk_journal_sequence <= journal_sequence_cut
            ):
                raise AuthorityConflict(
                    "durable risk decision is not after its transaction journal cut"
                )
        risk_digest = record.risk_decision_id.removeprefix("risk:sha256:")
        if (
            risk_payload.get("decision_id") != record.risk_decision_id
            or risk_payload.get("fingerprint") != risk_digest
            or risk_payload.get("intent_hash") != record.intent_hash
            or risk_payload.get("state_version") != record.state_version
            or risk_payload.get("policy_version") != record.policy_version
            or risk_payload.get("capability_snapshot_id")
            != record.capability_snapshot_id
            or risk_payload.get("valid_until") != record.risk_valid_until
            or risk_payload.get("verdict") != "ALLOW"
        ):
            raise AuthorityConflict(
                "durable risk decision does not match admitted record"
            )
        if _instant(
            risk_payload.get("evaluated_at"), name="risk.evaluated_at"
        ) > _instant(record.admitted_at, name="admitted_at"):
            raise AuthorityConflict("risk decision was evaluated after admission")
        if _instant(record.admitted_at, name="admitted_at") >= _instant(
            record.risk_valid_until, name="risk_valid_until"
        ):
            raise AuthorityConflict("risk decision was expired at admission")

        reservation_book = DurableReservationBook(
            self.store,
            environment=record.environment,
            account_id=record.account_id,
        )
        try:
            reservation = reservation_book.get(record.reservation_id)
        except KeyError as error:
            raise AuthorityConflict(
                "durable admission references missing reservation"
            ) from error
        if reservation.intent_id != record.intent_id:
            raise AuthorityConflict(
                "durable reservation intent does not match admission"
            )

        reservation_events = self.store.load_events(
            "reservation_book", reservation_book.scope_id
        )
        matching_reservations = [
            event
            for event in reservation_events
            if (
                isinstance(event.get("payload"), dict)
                and event["payload"].get("operation") == "RESERVE"
                and isinstance(event["payload"].get("snapshot"), dict)
                and event["payload"]["snapshot"].get("reservation_id")
                == record.reservation_id
            )
        ]
        if len(matching_reservations) != 1:
            raise AuthorityConflict(
                "durable admission reservation creation evidence is ambiguous"
            )
        reservation_event = matching_reservations[0]
        reservation_request = reservation_event["payload"].get("request")
        if not isinstance(reservation_request, dict):
            raise AuthorityConflict(
                "durable reservation request evidence is malformed"
            )
        risk_requirements = risk_payload.get("reservation_requirements")
        if (
            not isinstance(risk_requirements, dict)
            or reservation_request.get("requirements") != risk_requirements
        ):
            raise AuthorityConflict(
                "risk decision reservation delta does not match durable reservation"
            )

        availability_evidence = risk_payload.get(
            "reservation_availability_evidence"
        )
        if not isinstance(availability_evidence, Mapping):
            raise AuthorityConflict(
                "durable admission lacks reservation availability evidence"
            )
        try:
            regenerated_availability = (
                load_account_resource_availability_evidence(
                    self.store,
                    checkpoint_event_id=_text(
                        availability_evidence.get("checkpoint_event_id"),
                        name="checkpoint_event_id",
                    ),
                    provider_id=_text(
                        availability_evidence.get("provider_id"),
                        name="provider_id",
                    ),
                    account_id=record.account_id,
                    environment=record.environment,
                    resources=tuple(sorted(risk_requirements)),
                    now=record.admitted_at,
                    max_age_seconds=availability_evidence.get(
                        "max_age_seconds"
                    ),
                    evidence_artifact_store=self.evidence_artifact_store,
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise AuthorityConflict(
                "durable reservation availability evidence is invalid"
            ) from error
        expected_availability_evidence = {
            **regenerated_availability,
            "max_age_seconds": _canonical_decimal_text(
                _decimal(
                    availability_evidence.get("max_age_seconds"),
                    name="reservation_max_age_seconds",
                )
            ),
        }
        scope_latest_event_id = _text(
            availability_evidence.get("scope_latest_checkpoint_event_id"),
            name="scope_latest_checkpoint_event_id",
        )
        scope_latest_aggregate_id = _text(
            availability_evidence.get("scope_latest_checkpoint_aggregate_id"),
            name="scope_latest_checkpoint_aggregate_id",
        )
        scope_latest_version = availability_evidence.get(
            "scope_latest_checkpoint_aggregate_version"
        )
        scope_latest_observed_at = _text(
            availability_evidence.get("scope_latest_checkpoint_observed_at"),
            name="scope_latest_checkpoint_observed_at",
        )
        scope_latest_journal_sequence = availability_evidence.get(
            "scope_latest_checkpoint_journal_sequence"
        )
        if scope_latest_journal_sequence is not None:
            if (
                type(scope_latest_journal_sequence) is not int
                or scope_latest_journal_sequence <= 0
            ):
                raise AuthorityConflict(
                    "durable latest reconciliation journal sequence is invalid"
                )
            scope_checkpoint = self.store.get_event(scope_latest_event_id)
            if (
                scope_checkpoint is None
                or scope_checkpoint.get("journal_sequence")
                != scope_latest_journal_sequence
            ):
                raise AuthorityConflict(
                    "durable latest reconciliation journal sequence is inconsistent"
                )
        if (
            scope_latest_event_id
            != regenerated_availability.get("checkpoint_event_id")
            or scope_latest_aggregate_id
            != regenerated_availability.get("checkpoint_aggregate_id")
            or type(scope_latest_version) is not int
            or scope_latest_version <= 0
            or scope_latest_version
            != regenerated_availability.get("checkpoint_aggregate_version")
            or _instant(
                scope_latest_observed_at,
                name="scope_latest_checkpoint_observed_at",
            )
            != _instant(
                regenerated_availability.get("observed_at"),
                name="checkpoint.observed_at",
            )
        ):
            raise AuthorityConflict(
                "durable latest reconciliation binding is inconsistent"
            )
        expected_availability_evidence.update(
            {
                "scope_latest_checkpoint_event_id": scope_latest_event_id,
                "scope_latest_checkpoint_aggregate_id": scope_latest_aggregate_id,
                "scope_latest_checkpoint_aggregate_version": scope_latest_version,
                "scope_latest_checkpoint_observed_at": scope_latest_observed_at,
            }
        )
        if scope_latest_journal_sequence is None:
            # Historical durable evidence created before the explicit v6 cursor
            # remains verifiable after migration; newly generated v6 evidence binds it.
            expected_availability_evidence.pop(
                "scope_latest_checkpoint_journal_sequence",
                None,
            )
        else:
            expected_availability_evidence[
                "scope_latest_checkpoint_journal_sequence"
            ] = scope_latest_journal_sequence
        borrow_resources = tuple(
            sorted(
                resource
                for resource in risk_requirements
                if resource.startswith("BORROW:")
            )
        )
        raw_adjustments = availability_evidence.get(
            "borrow_capacity_adjustments"
        )
        reservation_expected_available = expected_availability_evidence.get(
            "availability"
        )
        if not isinstance(reservation_expected_available, Mapping):
            raise AuthorityConflict(
                "regenerated reservation availability is malformed"
            )
        legacy_expected_availability_evidence: dict[str, Any] | None = None
        if borrow_resources:
            if (
                not isinstance(raw_adjustments, Mapping)
                or set(raw_adjustments) != set(borrow_resources)
            ):
                raise AuthorityConflict(
                    "durable borrow capacity adjustments are missing or ambiguous"
                )
            regenerated_available = regenerated_availability.get(
                "availability"
            )
            if not isinstance(regenerated_available, Mapping):
                raise AuthorityConflict(
                    "regenerated reservation availability is malformed"
                )
            adjusted_available = dict(regenerated_available)
            canonical_adjustments: dict[str, dict[str, str]] = {}
            for resource in borrow_resources:
                raw_adjustment = raw_adjustments.get(resource)
                if not isinstance(raw_adjustment, Mapping) or set(
                    raw_adjustment
                ) != {
                    "total_capacity",
                    "current_borrowed_quantity",
                    "reservable_capacity",
                    "required_increment",
                }:
                    raise AuthorityConflict(
                        "durable borrow capacity adjustment is malformed"
                    )
                total_capacity = _decimal(
                    raw_adjustment.get("total_capacity"),
                    name="borrow.total_capacity",
                )
                current_borrowed = _decimal(
                    raw_adjustment.get("current_borrowed_quantity"),
                    name="borrow.current_borrowed_quantity",
                )
                reservable_capacity = _decimal(
                    raw_adjustment.get("reservable_capacity"),
                    name="borrow.reservable_capacity",
                )
                required_increment = _decimal(
                    raw_adjustment.get("required_increment"),
                    name="borrow.required_increment",
                )
                provider_total = _decimal(
                    regenerated_available.get(resource),
                    name="borrow.provider_total_capacity",
                )
                required = _decimal(
                    risk_requirements.get(resource),
                    name="borrow.reservation_requirement",
                )
                if (
                    total_capacity < 0
                    or current_borrowed < 0
                    or reservable_capacity < 0
                    or required_increment < 0
                    or total_capacity != provider_total
                    or current_borrowed > total_capacity
                    or reservable_capacity
                    != total_capacity - current_borrowed
                    or required_increment != required
                    or required > reservable_capacity
                ):
                    raise AuthorityConflict(
                        "durable borrow capacity adjustment is inconsistent"
                    )
                adjusted_available[resource] = _canonical_decimal_text(reservable_capacity)
                canonical_adjustments[resource] = {
                    "total_capacity": _canonical_decimal_text(total_capacity),
                    "current_borrowed_quantity": _canonical_decimal_text(current_borrowed),
                    "reservable_capacity": _canonical_decimal_text(reservable_capacity),
                    "required_increment": _canonical_decimal_text(required_increment),
                }
            reservation_expected_available = adjusted_available
            legacy_expected_availability_evidence = {
                **expected_availability_evidence,
                "borrow_capacity_adjustments": canonical_adjustments,
            }
            expected_availability_evidence["availability"] = adjusted_available
            expected_availability_evidence[
                "borrow_capacity_adjustments"
            ] = canonical_adjustments
        elif raw_adjustments is not None:
            raise AuthorityConflict(
                "non-borrow admission carries borrow capacity adjustments"
            )

        durable_availability_evidence = dict(availability_evidence)
        if durable_availability_evidence != expected_availability_evidence:
            if (
                legacy_expected_availability_evidence is None
                or durable_availability_evidence
                != legacy_expected_availability_evidence
            ):
                raise AuthorityConflict(
                    "durable reservation availability evidence is inconsistent"
                )
        if reservation_request.get("available") != reservation_expected_available:
            raise AuthorityConflict(
                "durable reservation exceeds authoritative account availability"
            )
        reservation_version = risk_payload.get("reservation_version")
        if (
            not isinstance(reservation_version, int)
            or isinstance(reservation_version, bool)
            or reservation_version < 0
            or int(reservation_event["aggregate_version"]) != reservation_version + 1
        ):
            raise AuthorityConflict(
                "risk decision was not bound to the reservation journal cut"
            )

        request = {
            "command_id": record.financial_command_id,
            "admission_id": record.admission_id,
            "policy_id": record.policy_id,
            "policy_version": record.policy_version,
            "intent_id": record.intent_id,
            "intent_hash": record.intent_hash,
            "account_id": record.account_id,
            "environment": record.environment,
            "instrument_id": record.instrument_version.instrument_id,
            "instrument_version": record.instrument_version.version,
            "action": record.action,
            "notional": _canonical_decimal_text(record.notional),
            "current_state_version": record.state_version,
            "capability_snapshot_id": record.capability_snapshot_id,
            "risk_decision_id": record.risk_decision_id,
            "risk_decision_fingerprint": risk_payload.get("fingerprint"),
            "reservation_id": record.reservation_id,
            "reservation": reservation_event["payload"].get("request"),
            "reservation_availability_evidence": availability_evidence,
            "confirmation_id": record.confirmation_id,
            "risk_reducing": record.risk_reducing,
        }
        if journal_sequence_cut is not None:
            request["journal_sequence_cut"] = journal_sequence_cut
        if "allocation_evidence" in risk_payload:
            allocation_evidence = risk_payload.get("allocation_evidence")
            if not isinstance(allocation_evidence, Mapping):
                raise AuthorityConflict(
                    "durable allocation evidence binding is malformed"
                )
            request["allocation_evidence"] = dict(allocation_evidence)
        expected_request_fingerprint = sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if expected_request_fingerprint != record.request_fingerprint:
            raise AuthorityConflict(
                "durable financial admission request fingerprint is inconsistent"
            )

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
            "notional": _canonical_decimal_text(amount),
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

        if (
            outcome == "ADMITTED"
            and self.store is not None
            and env in {"PAPER", "LIVE"}
        ):
            raise AuthorityConflict(
                "unverified PAPER/LIVE admission cannot create durable dispatch authority"
            )

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

    def _allocation_binding_for_admission(
        self,
        *,
        allocation_result: EvidenceBoundObjectiveAllocationResult,
        existing: AdmissionRecord | None,
        risk_intent: RiskIntent,
        risk_context: RiskContext,
        reservation_book: DurableReservationBook,
        reservation_provider_id: str,
        account_id: str,
        environment: str,
        capability_snapshot_id: str,
        instrument_id: str,
        instrument_version: int,
        now: str,
    ) -> dict[str, Any]:
        """Re-resolve and bind one allocation immediately before Transaction A."""

        if not isinstance(
            allocation_result,
            EvidenceBoundObjectiveAllocationResult,
        ):
            raise TypeError(
                "allocation_result must be EvidenceBoundObjectiveAllocationResult"
            )

        if existing is not None:
            if existing.risk_decision_id is None or self.store is None:
                raise AuthorityConflict(
                    "existing allocation admission lacks durable risk evidence"
                )
            events = self.store.load_events(
                "risk_decision",
                existing.risk_decision_id,
            )
            if (
                len(events) != 1
                or not isinstance(events[0].get("payload"), Mapping)
            ):
                raise AuthorityConflict(
                    "existing allocation admission evidence is missing"
                )
            persisted = events[0]["payload"].get("allocation_evidence")
            if not isinstance(persisted, Mapping):
                raise AuthorityConflict(
                    "existing admission was not allocation-bound"
                )
            expected_refs = [
                {"evidence_id": evidence_id, "digest": digest}
                for evidence_id, digest in allocation_result.evidence_refs
            ]
            if (
                persisted.get("decision_digest")
                != allocation_result.decision_digest
                or persisted.get("evidence_refs") != expected_refs
            ):
                raise AuthorityConflict(
                    "allocation result changed for an existing financial command"
                )
            return dict(persisted)

        resolver = self.allocation_authority_resolver
        if resolver is None:
            raise AuthorityConflict(
                "allocation admission requires an AuthorityService-owned resolver"
            )
        try:
            current = resolver(allocation_result)
        except Exception as error:
            raise AuthorityConflict(
                "allocation authority resolver could not resolve canonical state"
            ) from error
        if not isinstance(current, AllocationAuthoritySnapshot):
            raise AuthorityConflict(
                "allocation authority resolver returned an invalid snapshot"
            )

        expected_ids = {
            evidence_id
            for evidence_id, _digest in allocation_result.evidence_refs
        }
        if set(current.resolved_evidence) != expected_ids:
            raise AuthorityConflict(
                "allocation authority evidence set is incomplete or ambiguous"
            )

        provider = _text(
            reservation_provider_id,
            name="reservation_provider_id",
        ).upper()
        account = _text(account_id, name="account_id")
        env = _text(environment, name="environment").upper()
        if current.provider_id.upper() != provider:
            raise AuthorityConflict(
                "allocation provider does not match admission provider"
            )
        if current.account_id != account:
            raise AuthorityConflict(
                "allocation account does not match admission account"
            )

        try:
            revalidate_evidence_bound_allocation(
                allocation_result,
                resolved_evidence=current.resolved_evidence,
                environment=env,
                as_of=now,
                current_policy_version=current.policy_version,
                current_provider_id=current.provider_id,
                current_instrument_versions=current.instrument_versions,
                current_capability_snapshot_ids=current.capability_snapshot_ids,
                current_account_id=current.account_id,
                current_account_snapshot_id=current.account_snapshot_id,
                current_reconciliation_run_id=current.reconciliation_run_id,
                current_account_state_version=current.account_state_version,
                current_reservation_state_version=reservation_book.version,
                current_reservation_state_digest=reservation_book.state_digest,
            )
        except (TypeError, ValueError) as error:
            raise AuthorityConflict(
                "allocation evidence is stale, mismatched, or non-authoritative"
            ) from error

        admission_instrument = InstrumentVersionIdentity(
            instrument_id,
            instrument_version,
        )
        bound_financial_instrument = current.financial_instruments.get(
            risk_intent.symbol
        )
        if bound_financial_instrument != admission_instrument:
            raise AuthorityConflict(
                "allocation instrument does not match admitted instrument version"
            )

        capability = dict(
            allocation_result.capability_snapshot_ids
        ).get(risk_intent.symbol)
        if (
            capability is None
            or capability
            != _text(
                capability_snapshot_id,
                name="capability_snapshot_id",
            )
        ):
            raise AuthorityConflict(
                "allocation capability does not match admitted instrument"
            )

        targets = [
            target
            for target in allocation_result.objective.allocation.targets
            if target.symbol == risk_intent.symbol
        ]
        if (
            allocation_result.objective.allocation.status != "ALLOCATED"
            or risk_intent.symbol
            not in allocation_result.objective.selected_symbols
            or len(targets) != 1
        ):
            raise AuthorityConflict(
                "allocation does not authorize this trading symbol"
            )
        target = targets[0]
        current_position = risk_context.positions.get(
            risk_intent.symbol,
            Decimal("0"),
        )
        reserved_delta = risk_context.reserved_position_delta.get(
            risk_intent.symbol,
            Decimal("0"),
        )
        effective_position = current_position + reserved_delta
        remaining = target.quantity - effective_position
        if remaining == 0:
            raise AuthorityConflict(
                "allocation target already has no remaining quantity"
            )
        expected_side = "BUY" if remaining > 0 else "SELL"
        if risk_intent.side != expected_side:
            raise AuthorityConflict(
                "risk intent side moves away from allocation target"
            )
        if risk_intent.quantity > abs(remaining):
            raise AuthorityConflict(
                "risk intent quantity exceeds remaining allocation target"
            )

        allocation_valid_until = min(
            (item.valid_until for item in current.resolved_evidence.values()),
            key=lambda value: _instant(
                value,
                name="allocation evidence valid_until",
            ),
        )

        return {
            "decision_digest": allocation_result.decision_digest,
            "evidence_refs": [
                {"evidence_id": evidence_id, "digest": digest}
                for evidence_id, digest in allocation_result.evidence_refs
            ],
            "environment": allocation_result.environment,
            "provider_id": allocation_result.provider_id,
            "account_id": allocation_result.account_id,
            "policy_version": allocation_result.policy_version,
            "valid_until": allocation_valid_until,
            "account_snapshot_id": allocation_result.account_snapshot_id,
            "reconciliation_run_id": allocation_result.reconciliation_run_id,
            "account_state_version": current.account_state_version,
            "financial_instrument": {
                "instrument_id": admission_instrument.instrument_id,
                "version": admission_instrument.version,
            },
            "reservation_state_version": reservation_book.version,
            "reservation_state_digest": reservation_book.state_digest,
            "target": {
                "symbol": target.symbol,
                "target_quantity": _canonical_decimal_text(target.quantity),
                "effective_position_before": _canonical_decimal_text(
                    effective_position
                ),
                "remaining_quantity_before": _canonical_decimal_text(remaining),
                "admitted_side": risk_intent.side,
                "admitted_quantity": _canonical_decimal_text(
                    risk_intent.quantity
                ),
            },
        }

    def admit(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        admission_id: str,
        policy_id: str,
        intent_id: str,
        intent_hash: str,
        account_id: str,
        environment: str,
        instrument_id: str,
        instrument_version: int,
        action: str,
        notional,
        capability_snapshot_id: str,
        risk_intent: RiskIntent,
        risk_context: RiskContext,
        risk_policy: RiskPolicy,
        risk_valid_until: str,
        reservation_book: DurableReservationBook,
        reservation_id: str,
        reservation_requirements,
        reservation_available,
        reservation_checkpoint_event_id: str,
        reservation_provider_id: str,
        reservation_max_age_seconds,
        now: str,
        confirmation_id: str | None = None,
        risk_reducing: bool = False,
        allocation_result: EvidenceBoundObjectiveAllocationResult | None = None,
    ) -> AdmissionRecord:
        """Evaluate final risk inside the financial-writer boundary, then commit.

        Callers provide typed intent/context/policy inputs, never an ALLOW boolean
        or a pre-approved RiskDecision. Exact retries re-evaluate the same inputs
        against the original immutable risk binding, while new commands bind to
        the current reservation journal version immediately before Transaction A.
        """

        if not isinstance(risk_intent, RiskIntent):
            raise TypeError("risk_intent must be a RiskIntent")
        if not isinstance(risk_context, RiskContext):
            raise TypeError("risk_context must be a RiskContext")
        if not isinstance(risk_policy, RiskPolicy):
            raise TypeError("risk_policy must be a RiskPolicy")
        if self.store is None:
            raise AuthorityConflict(
                "durable financial admission requires a JournalStore"
            )
        if not isinstance(reservation_book, DurableReservationBook):
            raise TypeError("reservation_book must be DurableReservationBook")
        if reservation_book.store is not self.store:
            raise AuthorityConflict(
                "authority and reservation book must share one JournalStore"
            )

        aid = _text(admission_id, name="admission_id")
        pid = _text(policy_id, name="policy_id")
        capability = _text(
            capability_snapshot_id, name="capability_snapshot_id"
        )
        policy = self._policies.get(pid)
        if policy is None:
            raise KeyError(pid)

        existing = self._admissions.get(aid)
        if existing is None:
            journal_sequence_cut = self.store.current_journal_sequence()
            reservation_version = reservation_book.version
            evaluated_at = _text(now, name="now")
            valid_until = _text(risk_valid_until, name="risk_valid_until")
        else:
            if existing.risk_decision_id is None:
                raise AuthorityConflict(
                    "existing admission was not created by financial risk admission"
                )
            risk_events = self.store.load_events(
                "risk_decision", existing.risk_decision_id
            )
            if (
                len(risk_events) != 1
                or risk_events[0]["event_type"] != "RiskDecisionRecorded"
                or not isinstance(risk_events[0].get("payload"), dict)
            ):
                raise AuthorityConflict(
                    "existing admission risk evidence is missing or ambiguous"
                )
            payload = risk_events[0]["payload"]
            journal_sequence_cut = payload.get("journal_sequence_cut")
            if (
                journal_sequence_cut is not None
                and (
                    type(journal_sequence_cut) is not int
                    or journal_sequence_cut < 0
                )
            ):
                raise AuthorityConflict(
                    "existing admission journal cut is invalid"
                )
            reservation_version = payload.get("reservation_version")
            if (
                not isinstance(reservation_version, int)
                or isinstance(reservation_version, bool)
                or reservation_version < 0
            ):
                raise AuthorityConflict(
                    "existing admission reservation version is invalid"
                )
            evaluated_at = _text(
                payload.get("evaluated_at"), name="risk.evaluated_at"
            )
            valid_until = _text(
                payload.get("valid_until"), name="risk.valid_until"
            )
            if _instant(
                risk_valid_until, name="risk_valid_until"
            ) != _instant(valid_until, name="risk.valid_until"):
                raise AuthorityConflict(
                    "risk_valid_until changed for an existing financial command"
                )

        decision = evaluate_bound_risk(
            risk_intent,
            risk_context,
            risk_policy,
            intent_hash=_text(intent_hash, name="intent_hash"),
            policy_version=policy.version,
            reservation_version=reservation_version,
            reservation_requirements=reservation_requirements,
            capability_snapshot_id=capability,
            evaluated_at=evaluated_at,
            valid_until=valid_until,
        )

        normalized_requirements = normalize_reservation_requirements(
            reservation_requirements
        )
        requirement_map = dict(normalized_requirements)
        borrow_resources = tuple(
            resource
            for resource, _amount in normalized_requirements
            if resource.startswith("BORROW:")
        )
        required_borrow_resource: str | None = None
        required_borrow_quantity = Decimal("0")
        current_borrowed_quantity = Decimal("0")
        if decision.admitted:
            if risk_intent.instrument_type == "EQUITY":
                current_position = risk_context.positions.get(
                    risk_intent.symbol,
                    Decimal("0"),
                )
                reserved_delta = risk_context.reserved_position_delta.get(
                    risk_intent.symbol,
                    Decimal("0"),
                )
                required_borrow_quantity = incremental_short_borrow_quantity(
                    side=risk_intent.side,
                    quantity=risk_intent.quantity,
                    current_position=current_position,
                    reserved_position_delta=reserved_delta,
                )
                current_borrowed_quantity = max(
                    Decimal("0"),
                    -current_position,
                )
                if required_borrow_quantity > 0:
                    required_borrow_resource = borrow_resource_key(
                        provider_id=reservation_provider_id,
                        account_id=account_id,
                        environment=environment,
                        instrument_id=instrument_id,
                        instrument_version=instrument_version,
                    )
                    if (
                        borrow_resources != (required_borrow_resource,)
                        or requirement_map.get(required_borrow_resource)
                        != required_borrow_quantity
                    ):
                        raise AuthorityConflict(
                            "increased equity short requires exact scoped borrow reservation"
                        )
                elif borrow_resources:
                    raise AuthorityConflict(
                        "non-increasing equity short must not reserve new borrow capacity"
                    )
            elif borrow_resources:
                raise AuthorityConflict(
                    "securities-borrow reservation is valid only for equity short risk"
                )

        availability_evidence: Mapping[str, Any] | None = None
        authoritative_available = reservation_available
        if decision.admitted:
            checkpoint_event_id = _text(
                reservation_checkpoint_event_id,
                name="reservation_checkpoint_event_id",
            )
            provider_id = _text(
                reservation_provider_id,
                name="reservation_provider_id",
            ).upper()
            max_age = _decimal(
                reservation_max_age_seconds,
                name="reservation_max_age_seconds",
            )
            if max_age < 0:
                raise ValueError(
                    "reservation_max_age_seconds must be non-negative"
                )
            normalized_max_age = _canonical_decimal_text(max_age)

            if existing is not None:
                durable_risk_events = self.store.load_events(
                    "risk_decision", existing.risk_decision_id
                )
                if (
                    len(durable_risk_events) != 1
                    or not isinstance(
                        durable_risk_events[0].get("payload"), Mapping
                    )
                ):
                    raise AuthorityConflict(
                        "existing admission availability evidence is missing"
                    )
                durable_evidence = durable_risk_events[0]["payload"].get(
                    "reservation_availability_evidence"
                )
                if not isinstance(durable_evidence, Mapping):
                    raise AuthorityConflict(
                        "existing admission availability evidence is missing"
                    )
                if (
                    durable_evidence.get("checkpoint_event_id")
                    != checkpoint_event_id
                    or durable_evidence.get("provider_id") != provider_id
                    or durable_evidence.get("max_age_seconds")
                    != normalized_max_age
                ):
                    raise AuthorityConflict(
                        "reservation availability evidence changed for an existing financial command"
                    )
                availability_evidence = dict(durable_evidence)
            else:
                try:
                    loaded = load_account_resource_availability_evidence(
                        self.store,
                        checkpoint_event_id=checkpoint_event_id,
                        provider_id=provider_id,
                        account_id=account_id,
                        environment=environment,
                        resources=tuple(
                            resource
                            for resource, _amount in normalized_requirements
                        ),
                        now=now,
                        max_age_seconds=normalized_max_age,
                        evidence_artifact_store=self.evidence_artifact_store,
                        require_latest_scope=True,
                    )
                except ValueError as error:
                    if (
                        str(error)
                        == "selected reconciliation checkpoint is superseded by newer provider truth"
                    ):
                        raise AuthorityConflict(
                            "reservation checkpoint is superseded by newer provider truth"
                        ) from error
                    raise
                availability_evidence = {
                    **loaded,
                    "max_age_seconds": normalized_max_age,
                }

            raw_authoritative = availability_evidence.get("availability")
            if not isinstance(raw_authoritative, Mapping):
                raise AuthorityConflict(
                    "authoritative reservation availability is malformed"
                )
            authoritative_available = dict(raw_authoritative)

            if not isinstance(reservation_available, Mapping):
                raise TypeError("reservation_available must be a mapping")
            caller_available: dict[str, Decimal] = {}
            for raw_resource, raw_amount in reservation_available.items():
                resource = _text(
                    raw_resource,
                    name="reservation_available resource",
                )
                if resource in caller_available:
                    raise ValueError(
                        "reservation_available resources must be unique after normalization"
                    )
                amount = _decimal(
                    raw_amount,
                    name=f"reservation_available[{resource}]",
                )
                if amount < 0:
                    raise ValueError(
                        "reservation_available amounts must be non-negative"
                    )
                caller_available[resource] = amount
            canonical_available = {
                _text(
                    resource,
                    name="authoritative availability resource",
                ): _decimal(
                    amount,
                    name=f"authoritative availability[{resource}]",
                )
                for resource, amount in authoritative_available.items()
            }
            caller_expected_available = dict(canonical_available)
            durable_borrow_adjustment: tuple[Decimal, Decimal] | None = None
            if existing is not None and required_borrow_resource is not None:
                raw_adjustments = availability_evidence.get(
                    "borrow_capacity_adjustments"
                )
                if (
                    not isinstance(raw_adjustments, Mapping)
                    or set(raw_adjustments) != {required_borrow_resource}
                ):
                    raise AuthorityConflict(
                        "existing borrow admission lacks exact capacity adjustment"
                    )
                raw_adjustment = raw_adjustments.get(required_borrow_resource)
                required_adjustment_fields = {
                    "total_capacity",
                    "current_borrowed_quantity",
                    "reservable_capacity",
                    "required_increment",
                }
                if (
                    not isinstance(raw_adjustment, Mapping)
                    or set(raw_adjustment) != required_adjustment_fields
                ):
                    raise AuthorityConflict(
                        "existing borrow capacity adjustment is malformed"
                    )
                total_capacity = _decimal(
                    raw_adjustment.get("total_capacity"),
                    name="borrow.total_capacity",
                )
                durable_current_borrowed = _decimal(
                    raw_adjustment.get("current_borrowed_quantity"),
                    name="borrow.current_borrowed_quantity",
                )
                reservable_capacity = _decimal(
                    raw_adjustment.get("reservable_capacity"),
                    name="borrow.reservable_capacity",
                )
                durable_required_increment = _decimal(
                    raw_adjustment.get("required_increment"),
                    name="borrow.required_increment",
                )
                durable_available = canonical_available.get(
                    required_borrow_resource
                )
                if (
                    durable_available is None
                    or total_capacity < 0
                    or durable_current_borrowed < 0
                    or reservable_capacity < 0
                    or durable_required_increment < 0
                    or durable_current_borrowed != current_borrowed_quantity
                    or durable_required_increment != required_borrow_quantity
                    or durable_current_borrowed > total_capacity
                    or reservable_capacity
                    != total_capacity - durable_current_borrowed
                    or durable_available
                    not in {total_capacity, reservable_capacity}
                ):
                    raise AuthorityConflict(
                        "existing borrow capacity adjustment is inconsistent"
                    )
                caller_expected_available[required_borrow_resource] = (
                    total_capacity
                )
                durable_borrow_adjustment = (
                    total_capacity,
                    reservable_capacity,
                )

            if caller_available != caller_expected_available:
                raise AuthorityConflict(
                    "reservation_available does not match authoritative reconciliation checkpoint"
                )

            authoritative_available = dict(canonical_available)
            if required_borrow_resource is not None:
                if durable_borrow_adjustment is not None:
                    total_capacity, reservable_capacity = (
                        durable_borrow_adjustment
                    )
                    authoritative_available[required_borrow_resource] = (
                        reservable_capacity
                    )
                else:
                    total_capacity = canonical_available.get(
                        required_borrow_resource
                    )
                    if total_capacity is None:
                        raise AuthorityConflict(
                            "authoritative checkpoint lacks required borrow capacity"
                        )
                    if total_capacity < current_borrowed_quantity:
                        raise AuthorityConflict(
                            "provider borrow capacity is below current local borrow"
                        )
                    reservable_capacity = (
                        total_capacity - current_borrowed_quantity
                    )
                    authoritative_available[required_borrow_resource] = (
                        reservable_capacity
                    )
                    availability_evidence = {
                        **availability_evidence,
                        "availability": {
                            resource: _canonical_decimal_text(amount)
                            for resource, amount in authoritative_available.items()
                        },
                        "borrow_capacity_adjustments": {
                            required_borrow_resource: {
                                "total_capacity": _canonical_decimal_text(
                                    total_capacity
                                ),
                                "current_borrowed_quantity": _canonical_decimal_text(
                                    current_borrowed_quantity
                                ),
                                "reservable_capacity": _canonical_decimal_text(
                                    reservable_capacity
                                ),
                                "required_increment": _canonical_decimal_text(
                                    required_borrow_quantity
                                ),
                            }
                        },
                    }

        allocation_binding = None
        if allocation_result is not None:
            allocation_binding = self._allocation_binding_for_admission(
                allocation_result=allocation_result,
                existing=existing,
                risk_intent=risk_intent,
                risk_context=risk_context,
                reservation_book=reservation_book,
                reservation_provider_id=reservation_provider_id,
                account_id=account_id,
                environment=environment,
                capability_snapshot_id=capability,
                instrument_id=instrument_id,
                instrument_version=instrument_version,
                now=now,
            )

        return self._admit_bound_risk(
            command_id=command_id,
            idempotency_key=idempotency_key,
            admission_id=aid,
            policy_id=pid,
            intent_id=intent_id,
            intent_hash=intent_hash,
            account_id=account_id,
            environment=environment,
            instrument_id=instrument_id,
            instrument_version=instrument_version,
            action=action,
            notional=notional,
            current_state_version=risk_context.state_version,
            capability_snapshot_id=capability,
            risk_decision=decision,
            reservation_book=reservation_book,
            reservation_id=reservation_id,
            reservation_requirements=reservation_requirements,
            reservation_available=authoritative_available,
            now=now,
            reservation_availability_evidence=availability_evidence,
            allocation_binding=allocation_binding,
            journal_sequence_cut=journal_sequence_cut,
            confirmation_id=confirmation_id,
            risk_reducing=risk_reducing,
        )

    def _admit_bound_risk(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        admission_id: str,
        policy_id: str,
        intent_id: str,
        intent_hash: str,
        account_id: str,
        environment: str,
        instrument_id: str,
        instrument_version: int,
        action: str,
        notional,
        current_state_version: int,
        capability_snapshot_id: str,
        risk_decision: RiskDecision,
        reservation_book: DurableReservationBook,
        reservation_id: str,
        reservation_requirements,
        reservation_available,
        now: str,
        reservation_availability_evidence: Mapping[str, Any] | None = None,
        allocation_binding: Mapping[str, Any] | None = None,
        journal_sequence_cut: int | None = None,
        confirmation_id: str | None = None,
        risk_reducing: bool = False,
    ) -> AdmissionRecord:
        """Atomically admit verified risk evidence and its worst-case reservation.

        This is the public financial admission boundary. A boolean risk assertion
        is intentionally not accepted. The risk decision, reservation mutation,
        confirmation consumption encoded by the admission record, admission
        event, and execution outbox intent share one JournalStore transaction.
        """

        if self.store is None:
            raise AuthorityConflict(
                "durable financial admission requires a JournalStore"
            )
        if not isinstance(reservation_book, DurableReservationBook):
            raise TypeError("reservation_book must be DurableReservationBook")
        if reservation_book.store is not self.store:
            raise AuthorityConflict(
                "authority and reservation book must share one JournalStore"
            )

        cid = _text(command_id, name="command_id")
        idem = _text(idempotency_key, name="idempotency_key")
        aid = _text(admission_id, name="admission_id")
        pid = _text(policy_id, name="policy_id")
        iid = _text(intent_id, name="intent_id")
        ihash = _text(intent_hash, name="intent_hash")
        account = _text(account_id, name="account_id")
        env = _text(environment, name="environment").upper()
        capability = _text(
            capability_snapshot_id, name="capability_snapshot_id"
        )
        rid = _text(reservation_id, name="reservation_id")
        try:
            expected_risk_id = (
                "risk:sha256:" + risk_decision_fingerprint(risk_decision)
            )
        except (TypeError, ValueError) as error:
            raise AuthorityConflict(
                "risk decision binding is invalid"
            ) from error
        if risk_decision.decision_id != expected_risk_id:
            raise AuthorityConflict(
                "risk decision id does not match bound evidence"
            )
        normalized_requirements = normalize_reservation_requirements(
            reservation_requirements
        )
        if risk_decision.reservation_requirements != normalized_requirements:
            raise AuthorityConflict(
                "reservation requirements do not match risk decision"
            )
        if (
            not isinstance(current_state_version, int)
            or isinstance(current_state_version, bool)
            or current_state_version < 0
        ):
            raise ValueError(
                "current_state_version must be a non-negative integer"
            )
        if reservation_book.environment != env or reservation_book.account_id != account:
            raise AuthorityConflict(
                "reservation book scope does not match admission account/environment"
            )
        if not self._durable_authority_state_current():
            raise AuthorityConflict(
                "durable authority journal advanced; reload required"
            )

        policy = self._policies.get(pid)
        if policy is None:
            raise KeyError(pid)
        if (
            journal_sequence_cut is not None
            and (
                type(journal_sequence_cut) is not int
                or journal_sequence_cut < 0
            )
        ):
            raise ValueError(
                "journal_sequence_cut must be a non-negative integer"
            )

        # Exact retries must be replayable after the first transaction advances
        # the reservation aggregate.  Otherwise the risk decision that was
        # correctly bound to reservation version N would look stale at N+1 and
        # a lost response could not be recovered idempotently.  Only an exact
        # immutable admission identity is replayed; any changed scope fails
        # closed before a new reservation or send can occur.
        existing = self._admissions.get(aid)
        if existing is None and journal_sequence_cut is None:
            raise AuthorityConflict(
                "new financial admission requires a transaction journal cut"
            )
        if existing is not None:
            expected_instrument = InstrumentVersionIdentity(
                instrument_id, instrument_version
            )
            same_command = (
                existing.financial_command_id == cid
                and existing.policy_id == pid
                and existing.intent_id == iid
                and existing.intent_hash == ihash
                and existing.account_id == account
                and existing.environment == env
                and existing.instrument_version == expected_instrument
                and existing.action == _text(action, name="action").upper()
                and existing.notional == _decimal(notional, name="notional")
                and existing.state_version == current_state_version
                and existing.risk_decision_id == risk_decision.decision_id
                and existing.reservation_id == (
                    rid if existing.outcome == "ADMITTED" else None
                )
                and existing.capability_snapshot_id == capability
                and existing.risk_valid_until == risk_decision.valid_until
                and existing.policy_version == policy.version
                and existing.confirmation_id == confirmation_id
                and existing.risk_reducing == risk_reducing
            )
            if not same_command:
                raise AuthorityConflict(
                    "admission_id already belongs to another financial command"
                )
            durable_risk_events = self.store.load_events(
                "risk_decision", existing.risk_decision_id
            )
            if (
                len(durable_risk_events) != 1
                or not isinstance(
                    durable_risk_events[0].get("payload"), Mapping
                )
            ):
                raise AuthorityConflict(
                    "existing admission risk evidence is missing"
                )
            durable_risk_payload = durable_risk_events[0]["payload"]
            if (
                reservation_availability_evidence is not None
                and durable_risk_payload.get(
                    "reservation_availability_evidence"
                )
                != reservation_availability_evidence
            ):
                raise AuthorityConflict(
                    "reservation availability evidence changed for an existing financial command"
                )
            if durable_risk_payload.get("allocation_evidence") != (
                None if allocation_binding is None else dict(allocation_binding)
            ):
                raise AuthorityConflict(
                    "allocation evidence changed for an existing financial command"
                )
            return existing

        validate_bound_risk_decision(risk_decision, now=now)
        if risk_decision.intent_hash != ihash:
            raise AuthorityConflict("risk decision intent_hash mismatch")
        if risk_decision.state_version != current_state_version:
            raise AuthorityConflict("risk decision state_version is stale")
        if risk_decision.policy_version != policy.version:
            raise AuthorityConflict("risk decision policy_version is stale")
        current_reservation_version = reservation_book.version
        if risk_decision.reservation_version != current_reservation_version:
            raise AuthorityConflict("risk decision reservation_version is stale")
        if risk_decision.capability_snapshot_id != capability:
            raise AuthorityConflict("risk decision capability snapshot is stale")

        reservation_plan = None
        if risk_decision.admitted:
            reservation_plan = reservation_book.prepare_reserve_mutation(
                event_key=cid,
                idempotency_key=idem,
                reservation_id=rid,
                intent_id=iid,
                requirements=reservation_requirements,
                available=reservation_available,
                committed_at=now,
            )

        # Evaluate authority/confirmation semantics against an isolated copy.
        # The copy has no JournalStore, so it cannot persist or consume durable
        # confirmation state before the multi-aggregate transaction commits.
        probe = AuthorityService.restore(self.export_state())
        candidate = probe._admit_unverified(
            admission_id=aid,
            policy_id=pid,
            intent_hash=ihash,
            account_id=account,
            environment=env,
            instrument_id=instrument_id,
            instrument_version=instrument_version,
            action=action,
            notional=notional,
            state_version=current_state_version,
            risk_admitted=risk_decision.admitted,
            now=now,
            confirmation_id=confirmation_id,
            risk_reducing=risk_reducing,
        )

        if candidate.outcome == "ADMITTED" and reservation_plan is None:
            raise AuthorityConflict(
                "admitted command is missing an atomic reservation plan"
            )

        request = {
            "command_id": cid,
            "admission_id": aid,
            "policy_id": pid,
            "policy_version": policy.version,
            "intent_id": iid,
            "intent_hash": ihash,
            "account_id": account,
            "environment": env,
            "instrument_id": candidate.instrument_version.instrument_id,
            "instrument_version": candidate.instrument_version.version,
            "action": candidate.action,
            "notional": _canonical_decimal_text(candidate.notional),
            "current_state_version": current_state_version,
            "capability_snapshot_id": capability,
            "risk_decision_id": risk_decision.decision_id,
            "risk_decision_fingerprint": risk_decision_fingerprint(risk_decision),
            "reservation_id": rid,
            "reservation": (
                reservation_plan.request if reservation_plan is not None else None
            ),
            "reservation_availability_evidence": reservation_availability_evidence,
            "confirmation_id": candidate.confirmation_id,
            "risk_reducing": risk_reducing,
        }
        if journal_sequence_cut is not None:
            request["journal_sequence_cut"] = journal_sequence_cut
        if allocation_binding is not None:
            request["allocation_evidence"] = dict(allocation_binding)
        request_fingerprint = sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        record = replace(
            candidate,
            request_fingerprint=request_fingerprint,
            intent_id=iid,
            risk_decision_id=risk_decision.decision_id,
            reservation_id=(rid if candidate.outcome == "ADMITTED" else None),
            capability_snapshot_id=capability,
            risk_valid_until=risk_decision.valid_until,
            policy_version=policy.version,
            financial_command_id=cid,
        )

        risk_payload = {
            "decision_id": risk_decision.decision_id,
            "fingerprint": risk_decision_fingerprint(risk_decision),
            "intent_hash": risk_decision.intent_hash,
            "state_version": risk_decision.state_version,
            "policy_version": risk_decision.policy_version,
            "reservation_version": risk_decision.reservation_version,
            "reservation_requirements": reservation_requirements_payload(
                risk_decision.reservation_requirements
            ),
            "reservation_availability_evidence": reservation_availability_evidence,
            "journal_sequence_cut": journal_sequence_cut,
            "capability_snapshot_id": risk_decision.capability_snapshot_id,
            "evaluated_at": risk_decision.evaluated_at,
            "valid_until": risk_decision.valid_until,
            "verdict": "ALLOW" if risk_decision.admitted else "REJECT",
            "resulting_position": _canonical_decimal_text(risk_decision.resulting_position),
            "gross_leverage": _canonical_decimal_text(risk_decision.gross_leverage),
            "net_leverage": _canonical_decimal_text(risk_decision.net_leverage),
            "worst_stress_loss": _canonical_decimal_text(risk_decision.worst_stress_loss),
            "checks": [
                {
                    "rule_id": item.rule,
                    "passed": item.passed,
                    "measured": item.observed,
                    "limit": item.limit,
                    "evidence": item.reason,
                }
                for item in risk_decision.rules
            ],
        }
        if allocation_binding is not None:
            risk_payload["allocation_evidence"] = dict(allocation_binding)
        risk_event = {
            "event_id": _authority_event_id(
                "RiskDecisionRecorded", risk_decision.decision_id
            ),
            "event_type": "RiskDecisionRecorded",
            "aggregate_type": "risk_decision",
            "aggregate_id": risk_decision.decision_id,
            "aggregate_version": "1",
            "payload": risk_payload,
            "payload_hash": payload_digest(risk_payload),
            "committed_at": now,
        }
        admission_payload = self._admission_payload(record)
        admission_event = {
            "event_id": _authority_event_id(
                "AuthorityAdmissionRecorded", aid
            ),
            "event_type": "AuthorityAdmissionRecorded",
            "aggregate_type": "authority_state",
            "aggregate_id": "canonical",
            "aggregate_version": str(self._journal_version + 1),
            "payload": admission_payload,
            "payload_hash": payload_digest(admission_payload),
            "committed_at": now,
        }

        events = [(risk_event, None)]
        if reservation_plan is not None and candidate.outcome == "ADMITTED":
            events.append((reservation_plan.envelope, None))
        events.append(
            (
                admission_event,
                "financial.admission.ready"
                if candidate.outcome == "ADMITTED"
                else None,
            )
        )
        result_payload = {
            "admission": admission_payload,
            "reservation": (
                reservation_plan.snapshot_payload
                if reservation_plan is not None
                and candidate.outcome == "ADMITTED"
                else None
            ),
        }
        scoped_command_id = _authority_event_id(
            "FinancialAdmissionCommand", f"{env}:{account}:{cid}"
        )
        scoped_idempotency_key = _authority_event_id(
            "FinancialAdmissionIdempotency", f"{env}:{account}:{idem}"
        )

        try:
            self.store.commit_command(
                command_id=scoped_command_id,
                actor="autotrade-financial-writer",
                environment=env,
                idempotency_key=scoped_idempotency_key,
                request=request,
                result=result_payload,
                state_version=current_state_version,
                events=events,
                expected_journal_sequence=journal_sequence_cut,
            )
        except Exception:
            reservation_book.refresh()
            raise

        self._journal_version += 1
        self._admissions[aid] = record
        if record.outcome == "ADMITTED" and record.confirmation_id is not None:
            self._used_confirmations.add(record.confirmation_id)
        reservation_book.refresh()
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
        capability_snapshot_id: str | None = None,
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
            if _instant(now, name="now") >= _instant(
                confirmation.expires_at, name="confirmation.expires_at"
            ):
                return False, "confirmation_expired"

        if (
            record.environment in {"PAPER", "LIVE"}
            and record.risk_decision_id is None
        ):
            return False, "financial_evidence_missing"

        if record.risk_decision_id is not None:
            if record.policy_version != policy.version:
                return False, "policy_version_changed"
            if capability_snapshot_id is None:
                return False, "capability_snapshot_required"
            try:
                current_capability = _text(
                    capability_snapshot_id, name="capability_snapshot_id"
                )
            except ValueError:
                return False, "capability_snapshot_required"
            if current_capability != record.capability_snapshot_id:
                return False, "capability_snapshot_changed"
            if _instant(now, name="now") >= _instant(
                record.risk_valid_until, name="risk_valid_until"
            ):
                return False, "risk_decision_expired"
            try:
                self._validate_durable_financial_evidence(
                    record,
                    policy,
                    require_transaction_cut=True,
                )
                reservation_book = DurableReservationBook(
                    self.store,
                    environment=record.environment,
                    account_id=record.account_id,
                )
                reservation = reservation_book.get(record.reservation_id)
                risk_event = self.store.load_events(
                    "risk_decision", record.risk_decision_id
                )[0]
                risk_payload = risk_event["payload"]
                allocation_evidence = risk_payload.get("allocation_evidence")
                if allocation_evidence is not None:
                    if not isinstance(allocation_evidence, Mapping):
                        raise AuthorityConflict(
                            "durable allocation evidence binding is malformed"
                        )
                    allocation_valid_until = _text(
                        allocation_evidence.get("valid_until"),
                        name="allocation evidence valid_until",
                    )
                    if _instant(now, name="now") > _instant(
                        allocation_valid_until,
                        name="allocation evidence valid_until",
                    ):
                        return False, "allocation_evidence_expired"
                admission_reservation_version = (
                    int(risk_payload["reservation_version"]) + 1
                )
                risk_requirements = risk_payload.get("reservation_requirements")
                if not isinstance(risk_requirements, Mapping):
                    raise AuthorityConflict(
                        "durable risk reservation requirements are malformed"
                    )
                availability_evidence = risk_payload.get(
                    "reservation_availability_evidence"
                )
                if not isinstance(availability_evidence, Mapping):
                    raise AuthorityConflict(
                        "durable financial admission lacks availability evidence"
                    )
                # Historical evidence above is intentionally regenerated at the
                # admission instant so restart/replay remains deterministic.
                # Sending is a distinct authority boundary: the exact persisted
                # checkpoint/resources must still be current *now*.
                load_account_resource_availability_evidence(
                    self.store,
                    checkpoint_event_id=_text(
                        availability_evidence.get("checkpoint_event_id"),
                        name="checkpoint_event_id",
                    ),
                    provider_id=_text(
                        availability_evidence.get("provider_id"),
                        name="provider_id",
                    ),
                    account_id=record.account_id,
                    environment=record.environment,
                    resources=tuple(sorted(risk_requirements)),
                    now=now,
                    max_age_seconds=availability_evidence.get(
                        "max_age_seconds"
                    ),
                    evidence_artifact_store=self.evidence_artifact_store,
                    require_latest_scope=True,
                )
                borrow_resources = tuple(
                    resource
                    for resource in risk_requirements
                    if resource.startswith("BORROW:")
                )
                if borrow_resources:
                    availability_evidence = risk_payload.get(
                        "reservation_availability_evidence"
                    )
                    if not isinstance(availability_evidence, Mapping):
                        raise AuthorityConflict(
                            "durable short admission lacks availability evidence"
                        )
                    resource_details = availability_evidence.get(
                        "resource_details"
                    )
                    if not isinstance(resource_details, Mapping):
                        raise AuthorityConflict(
                            "durable short admission lacks borrow resource details"
                        )
                    for borrow_resource in borrow_resources:
                        detail = resource_details.get(borrow_resource)
                        if not isinstance(detail, Mapping):
                            raise AuthorityConflict(
                                "durable short admission lacks typed borrow detail"
                            )
                        borrow = BorrowAvailabilityEvidence.from_resource_detail(
                            detail
                        )
                        if (
                            borrow.resource_key != borrow_resource
                            or borrow.account_id != record.account_id
                            or borrow.environment != record.environment
                            or borrow.instrument_id
                            != record.instrument_version.instrument_id
                            or borrow.instrument_version
                            != record.instrument_version.version
                        ):
                            raise AuthorityConflict(
                                "durable borrow dispatch scope mismatch"
                            )
                        if self.evidence_artifact_store is None:
                            raise AuthorityConflict(
                                "borrow dispatch requires trusted ArtifactStore"
                            )
                        projection = DurableBorrowRecallProjection(
                            self.store,
                            provider_id=borrow.provider_id,
                            account_id=borrow.account_id,
                            environment=borrow.environment,
                            instrument_id=borrow.instrument_id,
                            instrument_version=borrow.instrument_version,
                            evidence_artifact_store=self.evidence_artifact_store,
                        )
                        if projection.active_quantity > 0:
                            return False, "borrow_recall_active"
            except Exception:
                return False, "financial_evidence_invalid"
            if reservation.intent_id != record.intent_id:
                return False, "reservation_intent_changed"
            if reservation.state != "WORKING":
                return False, "reservation_not_dispatchable"
            if reservation_book.version != admission_reservation_version:
                return False, "reservation_state_changed"

        # Final linearization barrier for durable authority. A revoke committed
        # by another process after any of the reads above must fail the send.
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
        capability_snapshot_id: str | None = None,
    ) -> Callable[[str, str], tuple[bool, str]]:
        """Bind one admitted versioned scope to the dispatcher's final barrier."""
        aid = _text(admission_id, name="admission_id")
        account = _text(account_id, name="account_id")
        env = _text(environment, name="environment").upper()
        identity = InstrumentVersionIdentity(instrument_id, instrument_version)
        normalized_action = _text(action, name="action").upper()
        capability = (
            None
            if capability_snapshot_id is None
            else _text(capability_snapshot_id, name="capability_snapshot_id")
        )

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
                capability_snapshot_id=capability,
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
                    "max_notional": _canonical_decimal_text(policy.max_notional),
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
                    "notional": _canonical_decimal_text(confirmation.notional),
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
                    "notional": _canonical_decimal_text(record.notional),
                    "risk_reducing": record.risk_reducing,
                    "state_version": record.state_version,
                    "authority_epoch": record.authority_epoch,
                    "outcome": record.outcome,
                    "admitted_at": record.admitted_at,
                    "confirmation_id": record.confirmation_id,
                    "reason": record.reason,
                    "request_fingerprint": record.request_fingerprint,
                    "intent_id": record.intent_id,
                    "risk_decision_id": record.risk_decision_id,
                    "reservation_id": record.reservation_id,
                    "capability_snapshot_id": record.capability_snapshot_id,
                    "risk_valid_until": record.risk_valid_until,
                    "policy_version": record.policy_version,
                    "financial_command_id": record.financial_command_id,
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
                intent_id=item.get("intent_id"),
                risk_decision_id=item.get("risk_decision_id"),
                reservation_id=item.get("reservation_id"),
                capability_snapshot_id=item.get("capability_snapshot_id"),
                risk_valid_until=item.get("risk_valid_until"),
                policy_version=item.get("policy_version"),
                financial_command_id=item.get("financial_command_id"),
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
