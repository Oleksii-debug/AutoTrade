"""Publish confirmable pending intents only from durable financial-writer evidence.

A client must never mint the economics consumed by ``CONFIRM_INTENT``.  This
adapter starts from an already durable financial admission attempt that the
canonical AuthorityService rejected *only* because operator confirmation was
missing.  The financial writer has therefore already resolved and sealed the
risk snapshot, quantitative policy episode, reservation requirements and order
semantics, while creating no reservation and no provider-send authority.

The public entrypoint accepts only the durable admission id and a server clock
instant.  Quantity, price, notional, policy content and reservation deltas are
reconstructed from canonical journal evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json

from .authority import AuthorityConflict, AuthorityService
from .pending_intent_financial_binding import (
    DurablePendingIntentFinancialBindingRegistry,
    PendingIntentFinancialBindingError,
)
from .pending_intents import (
    DurablePendingIntentRegistry,
    PendingFinancialIntent,
    PendingIntentError,
)
from .persistence import JournalStore
from .risk import RiskIntent
from .risk_policy_authority import (
    DurableRiskPolicyRegistry,
    RiskPolicyAuthorityError,
    RiskPolicyScope,
)


class PendingIntentPublicationError(RuntimeError):
    """Raised when durable financial evidence cannot become confirmable intent."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise PendingIntentPublicationError(
            f"{name} must be exact canonical non-empty text"
        )
    return value


def _utc(value: object, *, name: str) -> datetime:
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise PendingIntentPublicationError(
            f"{name} must use an exact datetime with built-in timezone"
        )
    if datetime.utcoffset(value) is None:
        raise PendingIntentPublicationError(f"{name} must be timezone-aware")
    return datetime.astimezone(value, timezone.utc)


def _parse_utc_text(value: object, *, name: str) -> datetime:
    text = _text(value, name=name)
    if not text.endswith("Z"):
        raise PendingIntentPublicationError(f"{name} must be canonical UTC text")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise PendingIntentPublicationError(
            f"{name} must be canonical UTC text"
        ) from error
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != text:
        raise PendingIntentPublicationError(f"{name} must be canonical UTC text")
    return parsed


def _risk_intent(value: object) -> RiskIntent:
    expected = {
        "symbol",
        "side",
        "quantity",
        "price",
        "expected_state_version",
        "reduce_only",
        "action",
        "instrument_type",
    }
    if type(value) is not dict or set(value) != expected:
        raise PendingIntentPublicationError(
            "durable financial risk_intent schema is invalid"
        )
    try:
        return RiskIntent.create(
            symbol=value["symbol"],
            side=value["side"],
            quantity=value["quantity"],
            price=value["price"],
            expected_state_version=value["expected_state_version"],
            reduce_only=value["reduce_only"],
            action=value["action"],
            instrument_type=value["instrument_type"],
        )
    except (TypeError, ValueError) as error:
        raise PendingIntentPublicationError(
            "durable financial risk_intent is invalid"
        ) from error


def _scope_from_resolved_payload(value: object) -> RiskPolicyScope:
    if type(value) is not dict:
        raise PendingIntentPublicationError(
            "durable risk snapshot lacks registry-issued RiskPolicy evidence"
        )
    identity = value.get("identity")
    if type(identity) is not dict:
        raise PendingIntentPublicationError(
            "durable RiskPolicy identity is malformed"
        )
    scope = identity.get("scope")
    expected = {
        "provider_id",
        "account_id",
        "environment",
        "provider_environment",
        "entity_policy_id",
        "instrument_family",
    }
    if type(scope) is not dict or set(scope) != expected:
        raise PendingIntentPublicationError(
            "durable RiskPolicy scope is malformed"
        )
    try:
        return RiskPolicyScope(**scope)
    except (TypeError, ValueError) as error:
        raise PendingIntentPublicationError(
            "durable RiskPolicy scope is invalid"
        ) from error


def _pending_id(*, admission_id: str, request_fingerprint: str,
                risk_decision_id: str, financial_command_id: str) -> str:
    payload = {
        "schema_version": 1,
        "source_admission_id": admission_id,
        "source_request_fingerprint": request_fingerprint,
        "source_risk_decision_id": risk_decision_id,
        "source_financial_command_id": financial_command_id,
    }
    digest = sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    return "pending-intent:sha256:" + digest


def publish_confirmation_required_intent(
    store: JournalStore,
    *,
    admission_id: str,
    at: datetime,
) -> PendingFinancialIntent:
    """Publish one immutable pending intent from canonical rejected admission.

    ``admission_id`` is the only caller-selected business identifier.  Every
    economic field is reconstructed from AuthorityService's durable financial
    evidence.  ``at`` is used only to prove the proposal has not expired; the
    durable pending/binding timestamps are derived from the original admission
    so exact retries at a later instant remain byte-identical.
    """

    if type(store) is not JournalStore:
        raise TypeError("store must be exact JournalStore")
    aid = _text(admission_id, name="admission_id")
    observed_at = _utc(at, name="at")

    try:
        authority = AuthorityService(store)
    except (AuthorityConflict, TypeError, ValueError) as error:
        raise PendingIntentPublicationError(
            "durable financial authority cannot be reconstructed"
        ) from error

    record = authority._admissions.get(aid)
    if record is None:
        raise PendingIntentPublicationError("durable admission is missing")
    if (
        record.outcome != "REJECTED"
        or record.reason != "confirmation_required"
        or record.confirmation_id is not None
        or record.requested_confirmation_id is not None
    ):
        raise PendingIntentPublicationError(
            "only a confirmation-required financial rejection is publishable"
        )
    if (
        record.risk_decision_id is None
        or record.risk_valid_until is None
        or record.policy_version is None
        or record.financial_command_id is None
    ):
        raise PendingIntentPublicationError(
            "confirmation-required admission lacks durable financial evidence"
        )

    policy = authority._policies.get(record.policy_id)
    if policy is None or policy.autonomous:
        raise PendingIntentPublicationError(
            "confirmation proposal requires a current non-autonomous authority policy"
        )

    try:
        risk_payload = authority._validate_historical_financial_retry_evidence(
            record,
            policy,
        )
    except (AuthorityConflict, KeyError, TypeError, ValueError) as error:
        raise PendingIntentPublicationError(
            "durable confirmation proposal evidence is invalid"
        ) from error

    if risk_payload.get("verdict") != "ALLOW":
        raise PendingIntentPublicationError(
            "risk-rejected financial evidence cannot become confirmable intent"
        )
    risk_intent = _risk_intent(risk_payload.get("risk_intent"))
    requirements = risk_payload.get("reservation_requirements")
    if type(requirements) is not dict:
        raise PendingIntentPublicationError(
            "durable confirmation proposal lacks reservation requirements"
        )
    snapshot = risk_payload.get("authoritative_risk_snapshot")
    if type(snapshot) is not dict:
        raise PendingIntentPublicationError(
            "durable confirmation proposal lacks authoritative risk snapshot"
        )
    durable_resolved = snapshot.get("resolved_risk_policy")
    scope = _scope_from_resolved_payload(durable_resolved)
    if (
        scope.account_id != record.account_id
        or scope.environment != record.environment
    ):
        raise PendingIntentPublicationError(
            "durable RiskPolicy scope differs from rejected financial admission"
        )

    registered_at = _parse_utc_text(record.admitted_at, name="admitted_at")
    expires_at = _parse_utc_text(record.risk_valid_until, name="risk_valid_until")
    if observed_at < registered_at:
        raise PendingIntentPublicationError(
            "confirmation proposal cannot be published before its financial decision"
        )
    if observed_at >= expires_at:
        raise PendingIntentPublicationError(
            "confirmation proposal financial evidence has expired"
        )

    try:
        risk_registry = DurableRiskPolicyRegistry(store)
        historical_cut = durable_resolved.get("resolved_journal_sequence_cut")
        if type(historical_cut) is not int or historical_cut < 0:
            raise PendingIntentPublicationError(
                "durable RiskPolicy resolution cut is invalid"
            )
        historical = risk_registry.resolve_current(
            scope,
            journal_sequence_cut=historical_cut,
        )
        if historical.evidence_payload != durable_resolved:
            raise PendingIntentPublicationError(
                "durable risk snapshot RiskPolicy evidence cannot be reproduced"
            )
        current = risk_registry.resolve_current(scope)
        if (
            current.identity != historical.identity
            or current.registration_event_id != historical.registration_event_id
            or current.activation_event_id != historical.activation_event_id
        ):
            raise PendingIntentPublicationError(
                "quantitative RiskPolicy activation changed before publication"
            )
    except PendingIntentPublicationError:
        raise
    except (RiskPolicyAuthorityError, KeyError, TypeError, ValueError) as error:
        raise PendingIntentPublicationError(
            "registry-issued quantitative RiskPolicy is unavailable"
        ) from error

    pending_id = _pending_id(
        admission_id=record.admission_id,
        request_fingerprint=record.request_fingerprint,
        risk_decision_id=record.risk_decision_id,
        financial_command_id=record.financial_command_id,
    )
    pending_registry = DurablePendingIntentRegistry(store)
    bindings = DurablePendingIntentFinancialBindingRegistry(store)
    try:
        pending_registry.register(
            pending_intent_id=pending_id,
            account_id=record.account_id,
            environment=record.environment,
            policy_id=record.policy_id,
            authority_policy_version=record.policy_version,
            instrument_id=record.instrument_version.instrument_id,
            instrument_version=record.instrument_version.version,
            authority_action=record.action,
            notional=record.notional,
            risk_intent=risk_intent,
            registered_at=registered_at,
            expires_at=expires_at,
        )
        bindings.bind(
            pending_registry,
            pending_intent_id=pending_id,
            account_id=record.account_id,
            environment=record.environment,
            authority_policy_id=record.policy_id,
            authority_policy_version=record.policy_version,
            resolved_risk_policy=historical,
            reservation_requirements=requirements,
            at=registered_at,
        )

        # Re-read current policy after both durable writes.  If another actor
        # changed the activation episode during publication, the proposal is
        # left durable but is explicitly non-confirmable and this call fails.
        current_after = risk_registry.resolve_current(scope)
        bindings.resolve_current(
            pending_id,
            resolved_risk_policy=current_after,
            reservation_requirements=requirements,
        )
        return pending_registry.resolve(
            pending_id,
            account_id=record.account_id,
            environment=record.environment,
            policy_id=record.policy_id,
            authority_policy_version=record.policy_version,
            at=observed_at,
        )
    except (PendingIntentError, PendingIntentFinancialBindingError,
            RiskPolicyAuthorityError, TypeError, ValueError) as error:
        raise PendingIntentPublicationError(
            "durable confirmation proposal publication failed closed"
        ) from error
