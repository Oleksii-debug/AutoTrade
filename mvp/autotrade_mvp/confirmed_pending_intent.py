"""Restart-safe recovery of one already confirmed server-owned pending intent.

The confirmation write path returns ``ConfirmedIntent`` in-process, but product
composition must be able to recover the same authority after restart without
asking a browser or caller to replay financial economics.  This module accepts
only the opaque pending id plus a server clock instant and reconstructs the
result from canonical journals.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .authority import (
    AuthorityService,
    _financial_confirmation_binding_hash,
    _risk_policy_fingerprint,
)
from .confirm_intent import ConfirmedIntent
from .pending_intent_financial_binding import (
    DurablePendingIntentFinancialBindingRegistry,
    PendingIntentFinancialBindingError,
)
from .pending_intents import DurablePendingIntentRegistry, PendingIntentError
from .persistence import JournalStore
from .risk_policy_authority import (
    DurableRiskPolicyRegistry,
    RiskPolicyAuthorityError,
    RiskPolicyScope,
)


class ConfirmedPendingIntentResolutionError(RuntimeError):
    """Raised when durable confirmed-pending authority cannot be recovered."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ConfirmedPendingIntentResolutionError(
            f"{name} must be exact canonical non-empty text"
        )
    return value


def _utc(value: object, *, name: str) -> datetime:
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise ConfirmedPendingIntentResolutionError(
            f"{name} must use an exact datetime with built-in timezone"
        )
    if datetime.utcoffset(value) is None:
        raise ConfirmedPendingIntentResolutionError(f"{name} must be timezone-aware")
    return datetime.astimezone(value, timezone.utc)


def _parse_utc_text(value: object, *, name: str) -> datetime:
    text = _text(value, name=name)
    if not text.endswith("Z"):
        raise ConfirmedPendingIntentResolutionError(
            f"{name} must be canonical UTC text"
        )
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ConfirmedPendingIntentResolutionError(
            f"{name} must be canonical UTC text"
        ) from error
    if parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") != text:
        raise ConfirmedPendingIntentResolutionError(
            f"{name} must be canonical UTC text"
        )
    return parsed


def _scope(binding) -> RiskPolicyScope:
    raw = binding.risk_policy_scope
    if type(raw) is not dict:
        raise ConfirmedPendingIntentResolutionError(
            "durable pending financial binding has invalid RiskPolicy scope"
        )
    try:
        return RiskPolicyScope(**dict(raw))
    except (TypeError, ValueError) as error:
        raise ConfirmedPendingIntentResolutionError(
            "durable pending financial binding RiskPolicy scope is invalid"
        ) from error


def resolve_confirmed_pending_intent(
    store: JournalStore,
    *,
    pending_intent_id: str,
    at: datetime,
) -> ConfirmedIntent:
    """Recover a current confirmed intent using server-owned durable authority only."""

    if type(store) is not JournalStore:
        raise TypeError("store must be exact JournalStore")
    pending_id = _text(pending_intent_id, name="pending_intent_id")
    point = _utc(at, name="at")

    pending_registry = DurablePendingIntentRegistry(store)
    binding_registry = DurablePendingIntentFinancialBindingRegistry(store)
    try:
        pending, claim = pending_registry._read(pending_id)
        binding = binding_registry._load(pending_id)
    except (PendingIntentError, PendingIntentFinancialBindingError) as error:
        raise ConfirmedPendingIntentResolutionError(
            "confirmed pending intent durable state is unavailable"
        ) from error

    if claim is None:
        raise ConfirmedPendingIntentResolutionError(
            "pending intent has no durable confirmation claim"
        )
    actor = _text(claim.get("actor_id"), name="actor_id")
    confirmation_id = _text(claim.get("confirmation_id"), name="confirmation_id")
    claimed_at = _parse_utc_text(claim.get("claimed_at"), name="claimed_at")
    expires_at = _parse_utc_text(pending.expires_at, name="expires_at")
    if point < claimed_at:
        raise ConfirmedPendingIntentResolutionError(
            "confirmed intent cannot be resolved before its durable claim"
        )
    if point >= expires_at:
        raise ConfirmedPendingIntentResolutionError(
            "confirmed pending intent authority has expired"
        )

    if (
        pending.pending_intent_id != binding.pending_intent_id
        or pending.intent_hash != binding.intent_hash
        or pending.account_id != binding.account_id
        or pending.environment != binding.environment
        or pending.policy_id != binding.authority_policy_id
        or pending.authority_policy_version != binding.authority_policy_version
    ):
        raise ConfirmedPendingIntentResolutionError(
            "pending intent no longer matches its durable financial binding"
        )

    requirements = dict(binding.reservation_requirements)
    try:
        risk_registry = DurableRiskPolicyRegistry(store)
        current_risk_policy = risk_registry.resolve_current(_scope(binding))
        binding_registry.resolve_current(
            pending_id,
            resolved_risk_policy=current_risk_policy,
            reservation_requirements=requirements,
        )
    except (RiskPolicyAuthorityError, PendingIntentFinancialBindingError) as error:
        raise ConfirmedPendingIntentResolutionError(
            "current quantitative risk authority no longer matches confirmed intent"
        ) from error

    try:
        authority = AuthorityService(store)
    except (TypeError, ValueError, RuntimeError) as error:
        raise ConfirmedPendingIntentResolutionError(
            "canonical financial authority is unavailable"
        ) from error
    confirmation = authority._confirmations.get(confirmation_id)
    if confirmation is None:
        raise ConfirmedPendingIntentResolutionError(
            "durable confirmation claim has no AuthorityConfirmationAdded proof"
        )

    try:
        expected_financial_binding_hash = _financial_confirmation_binding_hash(
            authority_policy_version=pending.authority_policy_version,
            risk_intent=pending.risk_intent,
            risk_policy_fingerprint=_risk_policy_fingerprint(
                current_risk_policy.policy
            ),
            reservation_requirements=requirements,
        )
    except (TypeError, ValueError) as error:
        raise ConfirmedPendingIntentResolutionError(
            "confirmed intent financial binding cannot be recomputed"
        ) from error

    if (
        confirmation.confirmation_id != confirmation_id
        or confirmation.policy_id != pending.policy_id
        or confirmation.intent_hash != pending.intent_hash
        or confirmation.account_id != pending.account_id
        or confirmation.environment != pending.environment
        or confirmation.instrument_version.instrument_id != pending.instrument_id
        or confirmation.instrument_version.version != pending.instrument_version
        or confirmation.action != pending.authority_action
        or confirmation.notional != pending.notional
        or confirmation.expires_at != pending.expires_at
        or confirmation.financial_binding_hash != expected_financial_binding_hash
    ):
        raise ConfirmedPendingIntentResolutionError(
            "durable AuthorityConfirmationAdded proof differs from server-owned intent"
        )

    return ConfirmedIntent(
        confirmation_id=confirmation_id,
        actor_id=actor,
        pending_intent=pending,
        financial_binding=binding,
    )
