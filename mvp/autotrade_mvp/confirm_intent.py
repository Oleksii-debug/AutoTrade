"""Server-side composition for confirming one durable pending financial intent.

The caller supplies only authenticated identity/scope, a durable host command
identity, the opaque pending-intent id, and the accepted timestamp.  All
financial economics, quantitative-policy content, reservation requirements,
and confirmation expiry are recovered from canonical journal authority.

This module deliberately does not expose a second admission or provider-send
path.  It composes the existing pending-intent registry, quantitative risk
policy registry, durable financial-envelope binding, and AuthorityService.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .authority import AuthorityService
from .pending_intent_financial_binding import (
    DurablePendingIntentFinancialBindingRegistry,
    PendingIntentFinancialBinding,
    PendingIntentFinancialBindingError,
)
from .pending_intents import (
    DurablePendingIntentRegistry,
    PendingFinancialIntent,
    PendingIntentError,
)
from .persistence import JournalStore
from .risk_policy_authority import (
    DurableRiskPolicyRegistry,
    RiskPolicyAuthorityError,
    RiskPolicyScope,
)


class ConfirmIntentError(RuntimeError):
    """Raised when server-owned confirmation authority cannot be completed."""


@dataclass(frozen=True)
class ConfirmedIntent:
    """Exact durable result of one server-side confirmation composition."""

    confirmation_id: str
    actor_id: str
    pending_intent: PendingFinancialIntent
    financial_binding: PendingIntentFinancialBinding


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ConfirmIntentError(f"{name} must be exact canonical non-empty text")
    return value


def _utc(value: object, *, name: str) -> datetime:
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise ConfirmIntentError(
            f"{name} must use an exact datetime with a built-in timezone"
        )
    if datetime.utcoffset(value) is None:
        raise ConfirmIntentError(f"{name} must be timezone-aware")
    return datetime.astimezone(value, timezone.utc)


def _risk_scope(binding: PendingIntentFinancialBinding) -> RiskPolicyScope:
    raw = binding.risk_policy_scope
    if type(raw) is not dict:
        raise ConfirmIntentError("durable risk policy scope is malformed")
    try:
        return RiskPolicyScope(**dict(raw))
    except (TypeError, ValueError) as error:
        raise ConfirmIntentError("durable risk policy scope is invalid") from error


def confirm_pending_intent(
    store: JournalStore,
    *,
    pending_intent_id: str,
    confirmation_id: str,
    actor_id: str,
    account_id: str,
    environment: str,
    accepted_at: datetime,
) -> ConfirmedIntent:
    """Claim and persist an exact financial confirmation from server authority.

    Ordering is intentional.  All read-only revalidation happens before the
    pending intent is claimed.  The claim is then written before the authority
    confirmation.  If the process crashes in that narrow window, retrying the
    same confirmation/actor pair reuses the durable claim and finishes the
    idempotent AuthorityConfirmationAdded write, even after pending expiry.  A
    different actor or confirmation id can never take over the claimed intent.
    """

    if type(store) is not JournalStore:
        raise TypeError("store must be exact JournalStore")
    pending_id = _text(pending_intent_id, name="pending_intent_id")
    confirmation = _text(confirmation_id, name="confirmation_id")
    actor = _text(actor_id, name="actor_id")
    account = _text(account_id, name="account_id")
    env = _text(environment, name="environment").upper()
    point = _utc(accepted_at, name="accepted_at")

    pending_registry = DurablePendingIntentRegistry(store)
    binding_registry = DurablePendingIntentFinancialBindingRegistry(store)

    # The binding registry owns durable binding parsing.  This package-internal
    # read is deliberately read-only; no duplicate financial authority is
    # reconstructed here.  The public host surface never receives the binding.
    try:
        binding = binding_registry._load(pending_id)
    except PendingIntentFinancialBindingError as error:
        raise ConfirmIntentError(
            "pending intent lacks exact durable financial-envelope binding"
        ) from error

    if binding.account_id != account or binding.environment != env:
        raise ConfirmIntentError(
            "pending financial binding differs from authenticated host scope"
        )

    try:
        risk_registry = DurableRiskPolicyRegistry(store)
        current_risk_policy = risk_registry.resolve_current(_risk_scope(binding))
        binding_registry.resolve_current(
            pending_id,
            resolved_risk_policy=current_risk_policy,
            reservation_requirements=dict(binding.reservation_requirements),
        )
    except (RiskPolicyAuthorityError, PendingIntentFinancialBindingError) as error:
        raise ConfirmIntentError(
            "current quantitative risk authority no longer matches confirmed envelope"
        ) from error

    # Read the registered economics without applying the first-claim expiry
    # rule. claim_confirmation() below owns that rule and can distinguish a new
    # claim from an exact durable retry. Calling resolve() here would wrongly
    # block crash recovery after a previously durable claim has expired.
    try:
        pending, _existing_claim = pending_registry._read(pending_id)
    except PendingIntentError as error:
        raise ConfirmIntentError("pending intent durable state is unavailable") from error
    if (
        pending.account_id != account
        or pending.environment != env
        or pending.policy_id != binding.authority_policy_id
        or pending.authority_policy_version != binding.authority_policy_version
        or pending.intent_hash != binding.intent_hash
    ):
        raise ConfirmIntentError(
            "pending intent no longer matches authenticated financial-envelope binding"
        )

    # Construct/replay AuthorityService before the one-way pending claim.  This
    # catches corrupt or unavailable authority state without consuming the
    # pending intent.  The exact authority policy is rechecked by
    # add_financial_confirmation after the claim; any concurrent authority
    # change can therefore only fail closed, never create executable authority.
    try:
        authority = AuthorityService(store)
    except (TypeError, ValueError, RuntimeError) as error:
        raise ConfirmIntentError("authority state is unavailable") from error

    try:
        claimed = pending_registry.claim_confirmation(
            pending_id,
            confirmation_id=confirmation,
            actor_id=actor,
            account_id=account,
            environment=env,
            policy_id=binding.authority_policy_id,
            authority_policy_version=binding.authority_policy_version,
            at=point,
        )
    except PendingIntentError as error:
        raise ConfirmIntentError("pending intent confirmation claim failed") from error
    if claimed != pending:
        raise ConfirmIntentError("durable pending intent changed during confirmation")

    try:
        authority.add_financial_confirmation(
            confirmation_id=confirmation,
            policy_id=claimed.policy_id,
            intent_hash=claimed.intent_hash,
            account_id=claimed.account_id,
            environment=claimed.environment,
            instrument_id=claimed.instrument_id,
            instrument_version=claimed.instrument_version,
            action=claimed.authority_action,
            notional=claimed.notional,
            expires_at=claimed.expires_at,
            risk_intent=claimed.risk_intent,
            risk_policy=current_risk_policy.policy,
            reservation_requirements=dict(binding.reservation_requirements),
        )
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise ConfirmIntentError(
            "AuthorityService rejected the server-derived financial confirmation"
        ) from error

    # Re-open from the journal so success is based on durable readback, not only
    # on the in-memory return from add_financial_confirmation.
    try:
        verified_authority = AuthorityService(store)
        durable_confirmation = verified_authority._confirmations.get(confirmation)
    except (TypeError, ValueError, RuntimeError) as error:
        raise ConfirmIntentError(
            "durable financial confirmation could not be revalidated"
        ) from error
    if durable_confirmation is None:
        raise ConfirmIntentError("financial confirmation is not durable")
    if (
        durable_confirmation.policy_id != claimed.policy_id
        or durable_confirmation.intent_hash != claimed.intent_hash
        or durable_confirmation.account_id != claimed.account_id
        or durable_confirmation.environment != claimed.environment
        or durable_confirmation.instrument_version.instrument_id
        != claimed.instrument_id
        or durable_confirmation.instrument_version.version
        != claimed.instrument_version
        or durable_confirmation.action != claimed.authority_action
        or durable_confirmation.notional != claimed.notional
        or durable_confirmation.expires_at != claimed.expires_at
        or durable_confirmation.financial_binding_hash is None
    ):
        raise ConfirmIntentError(
            "durable financial confirmation does not match server-owned pending intent"
        )

    return ConfirmedIntent(
        confirmation_id=confirmation,
        actor_id=actor,
        pending_intent=claimed,
        financial_binding=binding,
    )
