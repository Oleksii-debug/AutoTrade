"""Durable risk envelope for one server-owned pending financial intent.

This is a stacked Section-23 composition primitive.  It does not evaluate risk,
create a trading admission, or send provider requests.  It binds the immutable
pending-intent economics from :mod:`pending_intents` to one registry-issued
historical quantitative RiskPolicy episode and exact reservation requirements.

The policy content is not copied into a second authority format.  Durable state
retains the existing ``ResolvedRiskPolicy.evidence_payload`` and restart
re-resolves that exact historical cut through ``DurableRiskPolicyRegistry``.
Before publication and before operator confirmation, the same activation episode
must also still be current.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256

from .pending_intents import DurablePendingIntentRegistry, PendingIntentError
from .persistence import (
    JournalStore,
    payload_digest,
    require_exact_journal_store_authority,
)
from .risk import normalize_reservation_requirements, reservation_requirements_payload
from .risk_policy_authority import (
    DurableRiskPolicyRegistry,
    ResolvedRiskPolicy,
    RiskPolicyAuthorityError,
    RiskPolicyScope,
    journal_store_identity_digest,
    require_registry_issued_resolved_policy,
)


class PendingConfirmationEnvelopeError(RuntimeError):
    """Raised when the server-owned pending risk envelope is invalid or stale."""


_AGGREGATE_TYPE = "pending_financial_confirmation_envelope"
_EVENT_TYPE = "PendingFinancialConfirmationEnvelopePrepared"
_ENVELOPE_FACTORY = object()
_EVENT_KEYS = frozenset(
    {
        "event_id",
        "event_type",
        "aggregate_type",
        "aggregate_id",
        "aggregate_version",
        "payload",
        "payload_hash",
        "committed_at",
        "journal_sequence",
    }
)
_PAYLOAD_KEYS = frozenset(
    {
        "pending_intent_id",
        "pending_intent_hash",
        "risk_policy_evidence",
        "reservation_requirements",
        "prepared_at",
    }
)
_RISK_EVIDENCE_KEYS = frozenset(
    {
        "identity",
        "registration_event_id",
        "registration_journal_sequence",
        "activation_event_id",
        "activation_journal_sequence",
        "resolved_journal_sequence_cut",
        "journal_store_identity_digest",
    }
)
_RISK_IDENTITY_KEYS = frozenset({"policy_id", "version", "content_digest", "scope"})
_RISK_SCOPE_KEYS = frozenset(
    {
        "provider_id",
        "account_id",
        "environment",
        "provider_environment",
        "entity_policy_id",
        "instrument_family",
    }
)

# Retain the already-installed authority primitives.  Later public class/module
# rebinding must not redirect the risk-envelope durable cut.
_CANONICAL_JOURNAL_APPEND_EVENT = JournalStore.append_event
_CANONICAL_JOURNAL_LOAD_EVENTS = JournalStore.load_events
_CANONICAL_PAYLOAD_DIGEST = payload_digest
_CANONICAL_RISK_POLICY_RESOLVE_CURRENT = DurableRiskPolicyRegistry.resolve_current
_CANONICAL_REQUIRE_RESOLVED_POLICY = require_registry_issued_resolved_policy


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise PendingConfirmationEnvelopeError(
            f"{name} must be exact canonical non-empty text"
        )
    return value


def _utc(value: object, *, name: str) -> datetime:
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise PendingConfirmationEnvelopeError(
            f"{name} must use an exact datetime with a built-in timezone"
        )
    if datetime.utcoffset(value) is None:
        raise PendingConfirmationEnvelopeError(f"{name} must be timezone-aware")
    return datetime.astimezone(value, timezone.utc)


def _parse_utc_text(value: object, *, name: str) -> datetime:
    text = _text(value, name=name)
    if not text.endswith("Z"):
        raise PendingConfirmationEnvelopeError(f"{name} must be canonical UTC text")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise PendingConfirmationEnvelopeError(
            f"{name} must be canonical UTC text"
        ) from error
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != text:
        raise PendingConfirmationEnvelopeError(f"{name} must be canonical UTC text")
    return parsed


def _strict_dict(value: object, *, name: str, keys: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise PendingConfirmationEnvelopeError(f"{name} schema is not exact")
    return value


def _canonical_requirements(value: object) -> tuple[tuple[str, Decimal], ...]:
    try:
        payload = reservation_requirements_payload(value)
        requirements = normalize_reservation_requirements(payload)
    except (TypeError, ValueError) as error:
        raise PendingConfirmationEnvelopeError(
            "reservation requirements are not canonical"
        ) from error
    if type(requirements) is not tuple:
        raise PendingConfirmationEnvelopeError(
            "reservation requirements did not normalize to an exact tuple"
        )
    return requirements


def _scope_from_evidence(value: object) -> RiskPolicyScope:
    evidence = _strict_dict(
        value,
        name="risk policy evidence",
        keys=_RISK_EVIDENCE_KEYS,
    )
    identity = _strict_dict(
        evidence.get("identity"),
        name="risk policy identity",
        keys=_RISK_IDENTITY_KEYS,
    )
    scope_payload = _strict_dict(
        identity.get("scope"),
        name="risk policy scope",
        keys=_RISK_SCOPE_KEYS,
    )
    try:
        return RiskPolicyScope(**scope_payload)
    except (TypeError, ValueError, RiskPolicyAuthorityError) as error:
        raise PendingConfirmationEnvelopeError(
            "risk policy scope is not canonical"
        ) from error


def _resolved_policy_at(
    store: JournalStore,
    evidence: dict[str, object],
) -> ResolvedRiskPolicy:
    scope = _scope_from_evidence(evidence)
    cut = evidence.get("resolved_journal_sequence_cut")
    if type(cut) is not int or cut < 0:
        raise PendingConfirmationEnvelopeError(
            "risk policy resolved journal cut is invalid"
        )
    registry = DurableRiskPolicyRegistry(store)
    try:
        resolved = _CANONICAL_RISK_POLICY_RESOLVE_CURRENT(
            registry,
            scope,
            journal_sequence_cut=cut,
        )
        _CANONICAL_REQUIRE_RESOLVED_POLICY(resolved)
    except (RiskPolicyAuthorityError, TypeError, ValueError) as error:
        raise PendingConfirmationEnvelopeError(
            "historical risk policy authority cannot be reconstructed"
        ) from error
    if resolved.evidence_payload != evidence:
        raise PendingConfirmationEnvelopeError(
            "historical risk policy evidence differs from durable envelope"
        )
    return resolved


def _same_policy_episode(left: ResolvedRiskPolicy, right: ResolvedRiskPolicy) -> bool:
    try:
        admitted_left = _CANONICAL_REQUIRE_RESOLVED_POLICY(left)
        admitted_right = _CANONICAL_REQUIRE_RESOLVED_POLICY(right)
    except (RiskPolicyAuthorityError, TypeError, ValueError) as error:
        raise PendingConfirmationEnvelopeError(
            "risk policy episode is not registry-issued"
        ) from error
    return (
        admitted_left.identity == admitted_right.identity
        and admitted_left.registration_event_id == admitted_right.registration_event_id
        and admitted_left.registration_journal_sequence
        == admitted_right.registration_journal_sequence
        and admitted_left.activation_event_id == admitted_right.activation_event_id
        and admitted_left.activation_journal_sequence
        == admitted_right.activation_journal_sequence
        and admitted_left.journal_store_identity_digest
        == admitted_right.journal_store_identity_digest
    )


def _current_policy_for_episode(
    store: JournalStore,
    historical: ResolvedRiskPolicy,
) -> ResolvedRiskPolicy:
    scope = historical.identity.scope
    registry = DurableRiskPolicyRegistry(store)
    try:
        current = _CANONICAL_RISK_POLICY_RESOLVE_CURRENT(registry, scope)
        _CANONICAL_REQUIRE_RESOLVED_POLICY(current)
    except (RiskPolicyAuthorityError, TypeError, ValueError) as error:
        raise PendingConfirmationEnvelopeError(
            "current risk policy authority is unavailable"
        ) from error
    if not _same_policy_episode(current, historical):
        raise PendingConfirmationEnvelopeError(
            "risk policy episode is not current"
        )
    return current


def _event_id(pending_intent_id: str, pending_intent_hash: str) -> str:
    digest = sha256(
        (
            "pending-confirmation-risk-envelope-v1|"
            + pending_intent_id
            + "|"
            + pending_intent_hash
        ).encode("utf-8")
    ).hexdigest()
    return "pending-confirmation-envelope:" + digest


@dataclass(frozen=True)
class PendingConfirmationEnvelope:
    """Restart-reconstructed exact pending intent + historical risk envelope."""

    pending_intent_id: str
    pending_intent_hash: str
    resolved_risk_policy: ResolvedRiskPolicy
    reservation_requirements: tuple[tuple[str, Decimal], ...]
    prepared_at: str
    event_id: str
    journal_sequence: int
    _factory: InitVar[object | None] = None

    def __post_init__(self, _factory: object | None) -> None:
        if _factory is not _ENVELOPE_FACTORY:
            raise PendingConfirmationEnvelopeError(
                "pending confirmation envelope must come from durable registry"
            )
        pending_id = _text(self.pending_intent_id, name="pending_intent_id")
        intent_hash = _text(self.pending_intent_hash, name="pending_intent_hash")
        try:
            _CANONICAL_REQUIRE_RESOLVED_POLICY(self.resolved_risk_policy)
        except (RiskPolicyAuthorityError, TypeError, ValueError) as error:
            raise PendingConfirmationEnvelopeError(
                "pending envelope risk policy is not registry-issued"
            ) from error
        requirements = _canonical_requirements(self.reservation_requirements)
        prepared_at = _text(self.prepared_at, name="prepared_at")
        _parse_utc_text(prepared_at, name="prepared_at")
        expected_event_id = _event_id(pending_id, intent_hash)
        if self.event_id != expected_event_id:
            raise PendingConfirmationEnvelopeError(
                "pending envelope event id does not match intent identity"
            )
        if type(self.journal_sequence) is not int or self.journal_sequence < 1:
            raise PendingConfirmationEnvelopeError(
                "pending envelope journal sequence is invalid"
            )
        object.__setattr__(self, "pending_intent_id", pending_id)
        object.__setattr__(self, "pending_intent_hash", intent_hash)
        object.__setattr__(self, "reservation_requirements", requirements)


class DurablePendingConfirmationEnvelopeRegistry:
    """Risk-envelope authority sharing the pending intent's JournalStore."""

    def __init__(self, pending_registry: DurablePendingIntentRegistry) -> None:
        if type(pending_registry) is not DurablePendingIntentRegistry:
            raise TypeError(
                "pending_registry must be exact DurablePendingIntentRegistry"
            )
        self._pending_registry = pending_registry
        self._store = pending_registry.store
        identity = require_exact_journal_store_authority(
            self._store,
            subject="pending confirmation envelope journal",
        )
        self._journal_store_identity_digest = journal_store_identity_digest(identity)

    @property
    def store(self) -> JournalStore:
        if self._pending_registry.store is not self._store:
            raise PendingConfirmationEnvelopeError(
                "pending confirmation envelope JournalStore composition changed"
            )
        identity = require_exact_journal_store_authority(
            self._store,
            subject="pending confirmation envelope journal",
        )
        if journal_store_identity_digest(identity) != self._journal_store_identity_digest:
            raise PendingConfirmationEnvelopeError(
                "pending confirmation envelope JournalStore generation changed"
            )
        return self._store

    def _pending_historical(self, pending_intent_id: str):
        try:
            pending, claim = self._pending_registry._read(pending_intent_id)
        except (PendingIntentError, TypeError, ValueError) as error:
            raise PendingConfirmationEnvelopeError(
                "pending intent authority cannot be reconstructed"
            ) from error
        return pending, claim

    def prepare(
        self,
        pending_intent_id: str,
        *,
        account_id: str,
        environment: str,
        resolved_risk_policy: ResolvedRiskPolicy,
        reservation_requirements,
        prepared_at: datetime,
    ) -> PendingConfirmationEnvelope:
        pending_id = _text(pending_intent_id, name="pending_intent_id")
        point = _utc(prepared_at, name="prepared_at")
        pending, claim = self._pending_historical(pending_id)
        if claim is not None:
            raise PendingConfirmationEnvelopeError(
                "risk envelope cannot be prepared after pending confirmation claim"
            )
        if pending.account_id != _text(account_id, name="account_id"):
            raise PendingConfirmationEnvelopeError(
                "risk envelope account differs from authenticated pending scope"
            )
        env = _text(environment, name="environment").upper()
        if pending.environment != env:
            raise PendingConfirmationEnvelopeError(
                "risk envelope environment differs from authenticated pending scope"
            )
        registered = _parse_utc_text(pending.registered_at, name="registered_at")
        expires = _parse_utc_text(pending.expires_at, name="expires_at")
        if not registered <= point < expires:
            raise PendingConfirmationEnvelopeError(
                "pending intent is not current when risk envelope is prepared"
            )

        try:
            resolved = _CANONICAL_REQUIRE_RESOLVED_POLICY(resolved_risk_policy)
        except (RiskPolicyAuthorityError, TypeError, ValueError) as error:
            raise PendingConfirmationEnvelopeError(
                "risk envelope requires registry-issued ResolvedRiskPolicy"
            ) from error
        if resolved.journal_store_identity_digest != self._journal_store_identity_digest:
            raise PendingConfirmationEnvelopeError(
                "risk policy belongs to another JournalStore generation"
            )
        scope = resolved.identity.scope
        if scope.account_id != pending.account_id or scope.environment != pending.environment:
            raise PendingConfirmationEnvelopeError(
                "risk policy scope differs from pending financial intent"
            )
        _current_policy_for_episode(self.store, resolved)

        requirements = _canonical_requirements(reservation_requirements)
        prepared_text = point.isoformat().replace("+00:00", "Z")
        risk_evidence = resolved.evidence_payload
        if type(risk_evidence) is not dict:
            raise PendingConfirmationEnvelopeError(
                "resolved risk policy evidence is not canonical"
            )
        payload = {
            "pending_intent_id": pending.pending_intent_id,
            "pending_intent_hash": pending.intent_hash,
            "risk_policy_evidence": risk_evidence,
            "reservation_requirements": reservation_requirements_payload(requirements),
            "prepared_at": prepared_text,
        }
        event_id = _event_id(pending.pending_intent_id, pending.intent_hash)
        event = {
            "event_id": event_id,
            "event_type": _EVENT_TYPE,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": pending.pending_intent_id,
            "aggregate_version": "1",
            "payload": payload,
            "payload_hash": _CANONICAL_PAYLOAD_DIGEST(payload),
            "committed_at": prepared_text,
        }
        try:
            _CANONICAL_JOURNAL_APPEND_EVENT(self.store, event)
        except ValueError as error:
            try:
                existing = self.load(pending.pending_intent_id)
            except PendingConfirmationEnvelopeError:
                raise PendingConfirmationEnvelopeError(
                    "pending risk envelope conflicts with durable state"
                ) from error
            if (
                existing.pending_intent_hash != pending.intent_hash
                or existing.resolved_risk_policy.evidence_payload != risk_evidence
                or reservation_requirements_payload(existing.reservation_requirements)
                != payload["reservation_requirements"]
                or existing.prepared_at != prepared_text
            ):
                raise PendingConfirmationEnvelopeError(
                    "pending risk envelope conflicts with durable state"
                ) from error
            return existing
        return self.load(pending.pending_intent_id)

    def load(self, pending_intent_id: str) -> PendingConfirmationEnvelope:
        pending_id = _text(pending_intent_id, name="pending_intent_id")
        pending, _claim = self._pending_historical(pending_id)
        events = _CANONICAL_JOURNAL_LOAD_EVENTS(
            self.store,
            _AGGREGATE_TYPE,
            pending_id,
        )
        if len(events) != 1:
            raise PendingConfirmationEnvelopeError(
                "pending risk envelope is missing or ambiguous"
            )
        event = events[0]
        if type(event) is not dict or set(event) != _EVENT_KEYS:
            raise PendingConfirmationEnvelopeError(
                "pending risk envelope event schema is invalid"
            )
        payload = _strict_dict(
            event.get("payload"),
            name="pending risk envelope payload",
            keys=_PAYLOAD_KEYS,
        )
        expected_event_id = _event_id(pending_id, pending.intent_hash)
        if (
            event.get("event_id") != expected_event_id
            or event.get("event_type") != _EVENT_TYPE
            or event.get("aggregate_type") != _AGGREGATE_TYPE
            or event.get("aggregate_id") != pending_id
            or event.get("aggregate_version") != 1
            or payload.get("pending_intent_id") != pending_id
            or payload.get("pending_intent_hash") != pending.intent_hash
            or event.get("payload_hash") != _CANONICAL_PAYLOAD_DIGEST(payload)
            or event.get("committed_at") != payload.get("prepared_at")
        ):
            raise PendingConfirmationEnvelopeError(
                "pending risk envelope durable identity is corrupt"
            )
        sequence = event.get("journal_sequence")
        if type(sequence) is not int or sequence < 1:
            raise PendingConfirmationEnvelopeError(
                "pending risk envelope journal sequence is invalid"
            )
        prepared_at = _text(payload.get("prepared_at"), name="prepared_at")
        _parse_utc_text(prepared_at, name="prepared_at")
        risk_evidence = _strict_dict(
            payload.get("risk_policy_evidence"),
            name="risk policy evidence",
            keys=_RISK_EVIDENCE_KEYS,
        )
        if risk_evidence.get("journal_store_identity_digest") != self._journal_store_identity_digest:
            raise PendingConfirmationEnvelopeError(
                "durable risk policy belongs to another JournalStore generation"
            )
        historical = _resolved_policy_at(self.store, risk_evidence)
        requirements = _canonical_requirements(payload.get("reservation_requirements"))
        if reservation_requirements_payload(requirements) != payload.get(
            "reservation_requirements"
        ):
            raise PendingConfirmationEnvelopeError(
                "durable reservation requirements are non-canonical"
            )
        return PendingConfirmationEnvelope(
            pending_intent_id=pending_id,
            pending_intent_hash=pending.intent_hash,
            resolved_risk_policy=historical,
            reservation_requirements=requirements,
            prepared_at=prepared_at,
            event_id=expected_event_id,
            journal_sequence=sequence,
            _factory=_ENVELOPE_FACTORY,
        )

    def require_current(
        self,
        pending_intent_id: str,
    ) -> tuple[PendingConfirmationEnvelope, ResolvedRiskPolicy]:
        envelope = self.load(pending_intent_id)
        historical = _CANONICAL_REQUIRE_RESOLVED_POLICY(envelope.resolved_risk_policy)
        current = _current_policy_for_episode(self.store, historical)
        return envelope, current
