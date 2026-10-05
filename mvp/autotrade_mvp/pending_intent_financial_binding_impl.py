"""Durable financial-envelope binding for server-owned pending intents.

This module is the narrow bridge between ``DurablePendingIntentRegistry`` and
product-owned ``CONFIRM_INTENT``.  It does not mint authority, admit risk, or
send provider requests.  Instead it freezes the exact registry-issued
quantitative RiskPolicy identity and canonical reservation requirements that
were shown to the operator, on the same canonical JournalStore as the pending
intent.

A later host command may consume only this durable binding plus authenticated
actor identity.  Client-authored quantity, price, notional, policy content, or
reservation deltas are never accepted here.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from typing import Mapping

from .pending_intents import DurablePendingIntentRegistry, PendingIntentError
from .persistence import JournalStore, payload_digest
from .risk import normalize_reservation_requirements, reservation_requirements_payload
from .risk_policy_authority import (
    ResolvedRiskPolicy,
    journal_store_identity_digest,
    require_registry_issued_resolved_policy,
)


class PendingIntentFinancialBindingError(RuntimeError):
    """Raised when a pending intent cannot be bound to exact financial evidence."""


_AGGREGATE_TYPE = "pending_intent_financial_binding"
_EVENT_TYPE = "PendingIntentFinancialEnvelopeBound.v1"
_SCHEMA_VERSION = "1.0.0"
_FACTORY = object()


_CANONICAL_APPEND_EVENT = JournalStore.append_event
_CANONICAL_LOAD_EVENTS = JournalStore.load_events
_CANONICAL_STORE_IDENTITY = JournalStore.store_identity
_CANONICAL_PAYLOAD_DIGEST = payload_digest


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise PendingIntentFinancialBindingError(
            f"{name} must be exact canonical non-empty text"
        )
    return value


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 1:
        raise PendingIntentFinancialBindingError(f"{name} must be a positive integer")
    return value


def _utc(value: object, *, name: str) -> datetime:
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise PendingIntentFinancialBindingError(
            f"{name} must use an exact datetime with a built-in timezone"
        )
    if datetime.utcoffset(value) is None:
        raise PendingIntentFinancialBindingError(f"{name} must be timezone-aware")
    return datetime.astimezone(value, timezone.utc)


def _utc_text(value: object, *, name: str) -> str:
    return _utc(value, name=name).isoformat().replace("+00:00", "Z")


def _canonical_requirements(value: object) -> dict[str, str]:
    if type(value) is not dict:
        raise PendingIntentFinancialBindingError(
            "reservation_requirements must be an exact dict"
        )
    try:
        normalized = normalize_reservation_requirements(dict.copy(value))
        return reservation_requirements_payload(normalized)
    except (TypeError, ValueError) as error:
        raise PendingIntentFinancialBindingError(
            "reservation_requirements are not canonical financial requirements"
        ) from error


def _resolved_policy_snapshot(value: object) -> ResolvedRiskPolicy:
    try:
        resolved = require_registry_issued_resolved_policy(value)
    except (TypeError, ValueError) as error:
        raise PendingIntentFinancialBindingError(
            "risk policy must be a registry-issued ResolvedRiskPolicy"
        ) from error
    return resolved


def _store_identity_digest(store: JournalStore) -> str:
    if type(store) is not JournalStore:
        raise PendingIntentFinancialBindingError(
            "financial binding requires exact JournalStore authority"
        )
    try:
        identity = _CANONICAL_STORE_IDENTITY.__get__(store, JournalStore)
        return journal_store_identity_digest(identity)
    except (TypeError, ValueError, RuntimeError) as error:
        raise PendingIntentFinancialBindingError(
            "financial binding JournalStore authority is unavailable"
        ) from error


@dataclass(frozen=True)
class PendingIntentFinancialBinding:
    """Immutable server-issued binding consumed by confirmation composition."""

    pending_intent_id: str
    intent_hash: str
    account_id: str
    environment: str
    authority_policy_id: str
    authority_policy_version: int
    risk_policy_id: str
    risk_policy_version: int
    risk_policy_content_digest: str
    risk_policy_scope: Mapping[str, str]
    risk_policy_registration_event_id: str
    risk_policy_activation_event_id: str
    risk_policy_resolved_journal_sequence_cut: int
    journal_store_identity_digest: str
    reservation_requirements: Mapping[str, str]
    bound_at: str
    binding_hash: str
    _factory: InitVar[object | None] = None

    def __post_init__(self, _factory: object | None) -> None:
        if _factory is not _FACTORY:
            raise PendingIntentFinancialBindingError(
                "pending intent financial binding must come from durable registry"
            )
        _text(self.pending_intent_id, name="pending_intent_id")
        _text(self.intent_hash, name="intent_hash")
        _text(self.account_id, name="account_id")
        _text(self.environment, name="environment")
        _text(self.authority_policy_id, name="authority_policy_id")
        _positive_int(self.authority_policy_version, name="authority_policy_version")
        _text(self.risk_policy_id, name="risk_policy_id")
        _positive_int(self.risk_policy_version, name="risk_policy_version")
        _text(self.risk_policy_content_digest, name="risk_policy_content_digest")
        _text(
            self.risk_policy_registration_event_id,
            name="risk_policy_registration_event_id",
        )
        _text(
            self.risk_policy_activation_event_id,
            name="risk_policy_activation_event_id",
        )
        if (
            type(self.risk_policy_resolved_journal_sequence_cut) is not int
            or self.risk_policy_resolved_journal_sequence_cut < 0
        ):
            raise PendingIntentFinancialBindingError(
                "risk_policy_resolved_journal_sequence_cut must be non-negative"
            )
        _text(
            self.journal_store_identity_digest,
            name="journal_store_identity_digest",
        )
        if type(self.risk_policy_scope) is not dict:
            raise PendingIntentFinancialBindingError(
                "risk_policy_scope must be exact durable object"
            )
        if any(type(key) is not str or type(item) is not str for key, item in self.risk_policy_scope.items()):
            raise PendingIntentFinancialBindingError(
                "risk_policy_scope must contain exact text"
            )
        if type(self.reservation_requirements) is not dict:
            raise PendingIntentFinancialBindingError(
                "reservation_requirements must be exact durable object"
            )
        canonical_requirements = _canonical_requirements(
            dict(self.reservation_requirements)
        )
        if canonical_requirements != dict(self.reservation_requirements):
            raise PendingIntentFinancialBindingError(
                "reservation_requirements durable payload is non-canonical"
            )
        _text(self.bound_at, name="bound_at")
        expected = _binding_hash(_binding_payload_without_hash(self))
        if self.binding_hash != expected:
            raise PendingIntentFinancialBindingError(
                "financial binding hash does not match durable envelope"
            )


def _binding_payload_without_hash(
    value: PendingIntentFinancialBinding,
) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "pending_intent_id": value.pending_intent_id,
        "intent_hash": value.intent_hash,
        "account_id": value.account_id,
        "environment": value.environment,
        "authority_policy_id": value.authority_policy_id,
        "authority_policy_version": value.authority_policy_version,
        "risk_policy_id": value.risk_policy_id,
        "risk_policy_version": value.risk_policy_version,
        "risk_policy_content_digest": value.risk_policy_content_digest,
        "risk_policy_scope": dict(value.risk_policy_scope),
        "risk_policy_registration_event_id": value.risk_policy_registration_event_id,
        "risk_policy_activation_event_id": value.risk_policy_activation_event_id,
        "risk_policy_resolved_journal_sequence_cut": (
            value.risk_policy_resolved_journal_sequence_cut
        ),
        "journal_store_identity_digest": value.journal_store_identity_digest,
        "reservation_requirements": dict(value.reservation_requirements),
        "bound_at": value.bound_at,
    }


def _binding_hash(payload: Mapping[str, object]) -> str:
    return _CANONICAL_PAYLOAD_DIGEST(dict(payload))


def _binding_payload(value: PendingIntentFinancialBinding) -> dict[str, object]:
    payload = _binding_payload_without_hash(value)
    payload["binding_hash"] = value.binding_hash
    return payload


def _from_payload(value: object) -> PendingIntentFinancialBinding:
    if type(value) is not dict:
        raise PendingIntentFinancialBindingError(
            "durable financial binding payload must be an exact object"
        )
    expected = {
        "schema_version",
        "pending_intent_id",
        "intent_hash",
        "account_id",
        "environment",
        "authority_policy_id",
        "authority_policy_version",
        "risk_policy_id",
        "risk_policy_version",
        "risk_policy_content_digest",
        "risk_policy_scope",
        "risk_policy_registration_event_id",
        "risk_policy_activation_event_id",
        "risk_policy_resolved_journal_sequence_cut",
        "journal_store_identity_digest",
        "reservation_requirements",
        "bound_at",
        "binding_hash",
    }
    if set(value) != expected or value.get("schema_version") != _SCHEMA_VERSION:
        raise PendingIntentFinancialBindingError(
            "durable financial binding payload schema is invalid"
        )
    risk_scope = value.get("risk_policy_scope")
    requirements = value.get("reservation_requirements")
    if type(risk_scope) is not dict or type(requirements) is not dict:
        raise PendingIntentFinancialBindingError(
            "durable financial binding nested payload is invalid"
        )
    binding = PendingIntentFinancialBinding(
        pending_intent_id=value.get("pending_intent_id"),
        intent_hash=value.get("intent_hash"),
        account_id=value.get("account_id"),
        environment=value.get("environment"),
        authority_policy_id=value.get("authority_policy_id"),
        authority_policy_version=value.get("authority_policy_version"),
        risk_policy_id=value.get("risk_policy_id"),
        risk_policy_version=value.get("risk_policy_version"),
        risk_policy_content_digest=value.get("risk_policy_content_digest"),
        risk_policy_scope=dict(risk_scope),
        risk_policy_registration_event_id=value.get("risk_policy_registration_event_id"),
        risk_policy_activation_event_id=value.get("risk_policy_activation_event_id"),
        risk_policy_resolved_journal_sequence_cut=value.get(
            "risk_policy_resolved_journal_sequence_cut"
        ),
        journal_store_identity_digest=value.get("journal_store_identity_digest"),
        reservation_requirements=dict(requirements),
        bound_at=value.get("bound_at"),
        binding_hash=value.get("binding_hash"),
        _factory=_FACTORY,
    )
    if _binding_payload(binding) != value:
        raise PendingIntentFinancialBindingError(
            "durable financial binding payload is non-canonical"
        )
    return binding


class DurablePendingIntentFinancialBindingRegistry:
    """One immutable financial envelope per server-owned pending intent."""

    def __init__(self, store: JournalStore) -> None:
        if type(store) is not JournalStore:
            raise TypeError("store must be exact JournalStore")
        self._store = store
        self._store_identity_digest = _store_identity_digest(store)

    def _require_store(self) -> JournalStore:
        if type(self._store) is not JournalStore:
            raise PendingIntentFinancialBindingError(
                "financial binding JournalStore authority changed"
            )
        if _store_identity_digest(self._store) != self._store_identity_digest:
            raise PendingIntentFinancialBindingError(
                "financial binding JournalStore generation changed"
            )
        return self._store

    def _load(self, pending_intent_id: str) -> PendingIntentFinancialBinding:
        pending_id = _text(pending_intent_id, name="pending_intent_id")
        events = _CANONICAL_LOAD_EVENTS(
            self._require_store(),
            _AGGREGATE_TYPE,
            pending_id,
        )
        if len(events) != 1:
            raise PendingIntentFinancialBindingError(
                "pending financial binding is missing or ambiguous"
            )
        event = events[0]
        if (
            type(event) is not dict
            or event.get("event_id") != f"pending-financial-binding:{pending_id}"
            or event.get("event_type") != _EVENT_TYPE
            or event.get("aggregate_type") != _AGGREGATE_TYPE
            or event.get("aggregate_id") != pending_id
            or event.get("aggregate_version") != 1
        ):
            raise PendingIntentFinancialBindingError(
                "pending financial binding journal chronology is invalid"
            )
        payload = event.get("payload")
        if event.get("payload_hash") != _CANONICAL_PAYLOAD_DIGEST(payload):
            raise PendingIntentFinancialBindingError(
                "pending financial binding durable payload hash mismatch"
            )
        return _from_payload(payload)

    def bind(
        self,
        pending_registry: DurablePendingIntentRegistry,
        *,
        pending_intent_id: str,
        account_id: str,
        environment: str,
        authority_policy_id: str,
        authority_policy_version: int,
        resolved_risk_policy: ResolvedRiskPolicy,
        reservation_requirements: Mapping[str, object],
        at: datetime,
    ) -> PendingIntentFinancialBinding:
        if type(pending_registry) is not DurablePendingIntentRegistry:
            raise TypeError("pending_registry must be exact DurablePendingIntentRegistry")
        account = _text(account_id, name="account_id")
        env = _text(environment, name="environment").upper()
        policy_id = _text(authority_policy_id, name="authority_policy_id")
        policy_version = _positive_int(
            authority_policy_version,
            name="authority_policy_version",
        )
        bound_at = _utc_text(at, name="at")
        requirements = _canonical_requirements(reservation_requirements)
        resolved = _resolved_policy_snapshot(resolved_risk_policy)

        own_digest = self._store_identity_digest
        pending_digest = _store_identity_digest(pending_registry.store)
        if pending_digest != own_digest:
            raise PendingIntentFinancialBindingError(
                "pending intent and financial binding must use the same JournalStore"
            )
        if resolved.journal_store_identity_digest != own_digest:
            raise PendingIntentFinancialBindingError(
                "risk policy and pending intent must use the same JournalStore"
            )
        scope = resolved.identity.scope
        if scope.account_id != account or scope.environment != env:
            raise PendingIntentFinancialBindingError(
                "resolved risk policy scope differs from pending confirmation scope"
            )

        try:
            pending = pending_registry.resolve(
                pending_intent_id,
                account_id=account,
                environment=env,
                policy_id=policy_id,
                authority_policy_version=policy_version,
                at=at,
            )
        except PendingIntentError as error:
            raise PendingIntentFinancialBindingError(
                "pending intent is not authoritative for this confirmation scope"
            ) from error

        shell = {
            "schema_version": _SCHEMA_VERSION,
            "pending_intent_id": pending.pending_intent_id,
            "intent_hash": pending.intent_hash,
            "account_id": pending.account_id,
            "environment": pending.environment,
            "authority_policy_id": pending.policy_id,
            "authority_policy_version": pending.authority_policy_version,
            "risk_policy_id": resolved.identity.policy_id,
            "risk_policy_version": resolved.identity.version,
            "risk_policy_content_digest": resolved.identity.content_digest,
            "risk_policy_scope": resolved.identity.scope.payload(),
            "risk_policy_registration_event_id": resolved.registration_event_id,
            "risk_policy_activation_event_id": resolved.activation_event_id,
            "risk_policy_resolved_journal_sequence_cut": (
                resolved.resolved_journal_sequence_cut
            ),
            "journal_store_identity_digest": own_digest,
            "reservation_requirements": requirements,
            "bound_at": bound_at,
        }
        binding = PendingIntentFinancialBinding(
            pending_intent_id=pending.pending_intent_id,
            intent_hash=pending.intent_hash,
            account_id=pending.account_id,
            environment=pending.environment,
            authority_policy_id=pending.policy_id,
            authority_policy_version=pending.authority_policy_version,
            risk_policy_id=resolved.identity.policy_id,
            risk_policy_version=resolved.identity.version,
            risk_policy_content_digest=resolved.identity.content_digest,
            risk_policy_scope=resolved.identity.scope.payload(),
            risk_policy_registration_event_id=resolved.registration_event_id,
            risk_policy_activation_event_id=resolved.activation_event_id,
            risk_policy_resolved_journal_sequence_cut=(
                resolved.resolved_journal_sequence_cut
            ),
            journal_store_identity_digest=own_digest,
            reservation_requirements=requirements,
            bound_at=bound_at,
            binding_hash=_binding_hash(shell),
            _factory=_FACTORY,
        )
        payload = _binding_payload(binding)
        envelope = {
            "event_id": f"pending-financial-binding:{pending.pending_intent_id}",
            "event_type": _EVENT_TYPE,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": pending.pending_intent_id,
            "aggregate_version": "1",
            "payload": payload,
            "payload_hash": _CANONICAL_PAYLOAD_DIGEST(payload),
            "committed_at": bound_at,
        }
        try:
            _CANONICAL_APPEND_EVENT(self._require_store(), envelope)
        except ValueError as error:
            try:
                existing = self._load(pending.pending_intent_id)
            except PendingIntentFinancialBindingError:
                raise PendingIntentFinancialBindingError(
                    "pending financial binding conflicts with durable state"
                ) from error
            if existing != binding:
                raise PendingIntentFinancialBindingError(
                    "pending financial binding conflicts with durable state"
                ) from error
            return existing
        return self._load(pending.pending_intent_id)

    def resolve_current(
        self,
        pending_intent_id: str,
        *,
        resolved_risk_policy: ResolvedRiskPolicy,
        reservation_requirements: Mapping[str, object],
    ) -> PendingIntentFinancialBinding:
        binding = self._load(pending_intent_id)
        resolved = _resolved_policy_snapshot(resolved_risk_policy)
        requirements = _canonical_requirements(reservation_requirements)
        if resolved.journal_store_identity_digest != self._store_identity_digest:
            raise PendingIntentFinancialBindingError(
                "current risk policy does not belong to binding JournalStore"
            )
        if (
            resolved.identity.policy_id != binding.risk_policy_id
            or resolved.identity.version != binding.risk_policy_version
            or resolved.identity.content_digest != binding.risk_policy_content_digest
            or resolved.identity.scope.payload() != dict(binding.risk_policy_scope)
        ):
            raise PendingIntentFinancialBindingError(
                "current risk policy no longer matches operator-confirmed envelope"
            )
        if requirements != dict(binding.reservation_requirements):
            raise PendingIntentFinancialBindingError(
                "current reservation requirements no longer match operator-confirmed envelope"
            )
        return binding
