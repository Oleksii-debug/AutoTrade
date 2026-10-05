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

from .authority import (
    AuthorityConflict,
    AuthorityService,
    _authority_event_id,
    _authority_store_call,
)
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
from .persistence import JournalStore, payload_digest
from .risk_policy_authority import (
    DurableRiskPolicyRegistry,
    RiskPolicyAuthorityError,
    RiskPolicyScope,
)


class ConfirmIntentError(RuntimeError):
    """Raised when server-owned confirmation authority cannot be completed."""


class _JournalCutChanged(AuthorityConflict):
    """The global journal moved before the cut-bound confirmation append."""


class _CutBoundAuthorityService(AuthorityService):
    """AuthorityService whose only write is CAS-bound to one global journal cut."""

    def __init__(self, store: JournalStore, *, expected_journal_sequence: int) -> None:
        if type(expected_journal_sequence) is not int or expected_journal_sequence < 0:
            raise ValueError("expected_journal_sequence must be non-negative")
        self._confirmation_journal_cut = expected_journal_sequence
        super().__init__(store)

    def _persist(
        self,
        event_type: str,
        key: str,
        payload: dict[str, object],
        *,
        committed_at: str,
    ) -> None:
        if event_type != "AuthorityConfirmationAdded":
            raise AuthorityConflict(
                "cut-bound confirmation service may persist only confirmation authority"
            )

        durable_next = _authority_store_call(
            self,
            "next_aggregate_version",
            "authority_state",
            "canonical",
        )
        durable_version = durable_next - 1
        if durable_version != self._journal_version:
            raise AuthorityConflict(
                "durable authority journal advanced; reload required"
            )

        event_id = _authority_event_id(event_type, key)
        existing = _authority_store_call(self, "get_event", event_id)
        if existing is not None:
            if existing["event_type"] != event_type or existing["payload"] != payload:
                raise AuthorityConflict(
                    "durable authority event conflicts with existing content"
                )
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
            result = _authority_store_call(
                self,
                "append_event",
                envelope,
                expected_journal_sequence=self._confirmation_journal_cut,
            )
        except ValueError as error:
            raise _JournalCutChanged(
                "global journal changed before financial confirmation append"
            ) from error
        if result.inserted:
            self._journal_version += 1


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


def _sequence(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ConfirmIntentError(f"{name} must be a non-negative integer")
    return value


def _require_authority_cut(
    authority: AuthorityService,
    *,
    expected_epoch: int,
    expected_version: int,
) -> None:
    actual_epoch = authority.epoch
    actual_version = authority._journal_version
    if (
        type(actual_epoch) is not int
        or type(actual_version) is not int
        or actual_epoch != expected_epoch
        or actual_version != expected_version
    ):
        raise ConfirmIntentError(
            "authority state changed after confirmation acceptance"
        )


def confirm_pending_intent(
    store: JournalStore,
    *,
    pending_intent_id: str,
    confirmation_id: str,
    actor_id: str,
    account_id: str,
    environment: str,
    accepted_at: datetime,
    expected_authority_epoch: int | None = None,
    expected_authority_version: int | None = None,
) -> ConfirmedIntent:
    """Claim and persist an exact financial confirmation from server authority.

    Ordering is intentional.  Read-only validation happens before the one-way
    pending claim.  After that claim, quantitative-policy episode and authority
    cut are revalidated and AuthorityConfirmationAdded is atomically appended
    only if the whole journal is still at that exact cut. Exact claim retries
    remain recoverable after pending expiry; competing actor/confirmation pairs
    can never take over the claim.
    """

    if type(store) is not JournalStore:
        raise TypeError("store must be exact JournalStore")
    pending_id = _text(pending_intent_id, name="pending_intent_id")
    confirmation = _text(confirmation_id, name="confirmation_id")
    actor = _text(actor_id, name="actor_id")
    account = _text(account_id, name="account_id")
    env = _text(environment, name="environment").upper()
    point = _utc(accepted_at, name="accepted_at")
    if (expected_authority_epoch is None) != (expected_authority_version is None):
        raise ConfirmIntentError(
            "expected authority epoch and version must be supplied together"
        )
    supplied_authority_cut = expected_authority_epoch is not None
    if supplied_authority_cut:
        expected_epoch = _sequence(
            expected_authority_epoch,
            name="expected_authority_epoch",
        )
        expected_version = _sequence(
            expected_authority_version,
            name="expected_authority_version",
        )

    pending_registry = DurablePendingIntentRegistry(store)
    binding_registry = DurablePendingIntentFinancialBindingRegistry(store)

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

    scope = _risk_scope(binding)
    requirements = dict(binding.reservation_requirements)
    try:
        risk_registry = DurableRiskPolicyRegistry(store)
        current_risk_policy = risk_registry.resolve_current(scope)
        binding_registry.resolve_current(
            pending_id,
            resolved_risk_policy=current_risk_policy,
            reservation_requirements=requirements,
        )
    except (RiskPolicyAuthorityError, PendingIntentFinancialBindingError) as error:
        raise ConfirmIntentError(
            "current quantitative risk authority no longer matches confirmed envelope"
        ) from error

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

    # Establish the accepted authority cut before consuming the pending intent.
    # Direct package callers bind to the current exact cut; the Host adapter
    # supplies its already-durable acceptance cut so a race after Host checking
    # cannot silently upgrade the command onto newer authority state.
    try:
        preclaim_authority = AuthorityService(store)
    except (TypeError, ValueError, RuntimeError) as error:
        raise ConfirmIntentError("authority state is unavailable") from error
    if supplied_authority_cut:
        _require_authority_cut(
            preclaim_authority,
            expected_epoch=expected_epoch,
            expected_version=expected_version,
        )
    else:
        expected_epoch = _sequence(
            preclaim_authority.epoch,
            name="authority_epoch",
        )
        expected_version = _sequence(
            preclaim_authority._journal_version,
            name="authority_version",
        )

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

    # The pending claim is now durable.  Re-resolve the exact activation episode
    # and couple the confirmation append to the same whole-journal cut.  A small
    # bounded retry absorbs unrelated concurrent journal traffic; every retry
    # rechecks both RiskPolicy episode and accepted AuthorityService cut first.
    final_risk_policy = None
    for _attempt in range(8):
        try:
            final_risk_policy = risk_registry.resolve_current(scope)
            binding_registry.resolve_current(
                pending_id,
                resolved_risk_policy=final_risk_policy,
                reservation_requirements=requirements,
            )
            authority = _CutBoundAuthorityService(
                store,
                expected_journal_sequence=(
                    final_risk_policy.resolved_journal_sequence_cut
                ),
            )
            _require_authority_cut(
                authority,
                expected_epoch=expected_epoch,
                expected_version=expected_version,
            )
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
                risk_policy=final_risk_policy.policy,
                reservation_requirements=requirements,
            )
            break
        except _JournalCutChanged:
            final_risk_policy = None
            continue
        except (RiskPolicyAuthorityError, PendingIntentFinancialBindingError) as error:
            raise ConfirmIntentError(
                "current quantitative risk authority changed after pending claim"
            ) from error
        except ConfirmIntentError:
            raise
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise ConfirmIntentError(
                "AuthorityService rejected the server-derived financial confirmation"
            ) from error
    else:
        raise ConfirmIntentError(
            "global journal remained unstable during financial confirmation append"
        )

    if final_risk_policy is None:
        raise ConfirmIntentError("financial confirmation risk authority is unavailable")

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
