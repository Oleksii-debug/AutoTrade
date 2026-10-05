"""Canonical operator authority command dispatch.

Every pre-existing host authority action delegates byte-for-byte to the retained
legacy implementation.  CONFIRM_INTENT is the only added dispatch path and is
bound to the server-owned pending-intent composition.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Mapping

from . import operator_authority_commands_legacy as _legacy
from .authority import (
    AuthorityService,
    _financial_confirmation_binding_hash,
    _risk_policy_fingerprint,
)
from .confirm_intent import ConfirmIntentError, confirm_pending_intent
from .host_actions import canonical_host_action
from .pending_intent_financial_binding import (
    DurablePendingIntentFinancialBindingRegistry,
    PendingIntentFinancialBindingError,
)
from .pending_intents import DurablePendingIntentRegistry, PendingIntentError
from .persistence import JournalStore, payload_digest
from .risk_policy_authority import DurableRiskPolicyRegistry, RiskPolicyScope


OperatorAuthorityConflict = _legacy.OperatorAuthorityConflict
AuthorityExecutionResult = _legacy.AuthorityExecutionResult
CONFIRM_INTENT = "CONFIRM_INTENT"
_CONFIRM_SCHEMA_VERSION = 1
_AUTHENTICATED_HOST_ACTOR: ContextVar[object | None] = ContextVar(
    "autotrade_authenticated_host_actor",
    default=None,
)


@contextmanager
def authenticated_host_actor_scope(actor: object):
    """Carry the already-authenticated host actor into payload canonicalization.

    The durable host wrapper enters this scope around its existing submit path.
    The retained submit implementation validates session+actor+origin+action
    before calling canonical_operator_payload(), so this value cannot become
    confirmation authority unless that existing authentication has succeeded.
    """

    token = _AUTHENTICATED_HOST_ACTOR.set(actor)
    try:
        yield
    finally:
        _AUTHENTICATED_HOST_ACTOR.reset(token)


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be exact canonical non-empty text")
    return value


def _sha256(value: object, *, name: str) -> str:
    text = _exact_text(value, name=name).lower()
    if (
        len(text) != 71
        or not text.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in text[7:])
    ):
        raise ValueError(f"{name} must be canonical lowercase sha256:<64-hex>")
    return text


def _accepted_datetime(value: object) -> datetime:
    text = _exact_text(value, name="accepted_at")
    try:
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except ValueError as error:
        raise ValueError("accepted_at must be an ISO-8601 instant") from error
    if type(parsed.tzinfo) is not timezone or datetime.utcoffset(parsed) is None:
        raise ValueError("accepted_at must use a built-in timezone")
    return parsed.astimezone(timezone.utc)


def _confirm_payload(
    journal: JournalStore,
    raw_payload: Mapping[str, object],
    command_id: str,
    account_id: str,
    environment: str,
) -> dict[str, object]:
    if type(raw_payload) is not dict or set(raw_payload) != {"pending_intent_id"}:
        raise ValueError(
            "CONFIRM_INTENT payload must contain only pending_intent_id"
        )
    command = _exact_text(command_id, name="command_id")
    account = _exact_text(account_id, name="account_id")
    env = _exact_text(environment, name="environment").upper()
    pending_id = _exact_text(
        raw_payload.get("pending_intent_id"),
        name="pending_intent_id",
    )
    actor = _AUTHENTICATED_HOST_ACTOR.get()
    actor_id = _exact_text(actor, name="authenticated actor")

    try:
        binding = DurablePendingIntentFinancialBindingRegistry(journal)._load(
            pending_id
        )
    except PendingIntentFinancialBindingError as error:
        raise OperatorAuthorityConflict(
            "CONFIRM_INTENT pending financial binding is unavailable"
        ) from error
    if binding.account_id != account or binding.environment != env:
        raise OperatorAuthorityConflict(
            "CONFIRM_INTENT pending binding differs from host scope"
        )

    _service, state, _events, version = _legacy._state(journal)
    epoch = state.get("epoch")
    if type(epoch) is not int or epoch < 0:
        raise OperatorAuthorityConflict("authority epoch is malformed")
    return {
        "schema_version": _CONFIRM_SCHEMA_VERSION,
        "command_id": command,
        "confirmation_id": command,
        "pending_intent_id": pending_id,
        "actor_id": actor_id,
        "account_id": account,
        "environment": env,
        "expected_authority_epoch": str(epoch),
        "expected_authority_version": str(version),
        "financial_binding_hash": binding.binding_hash,
    }


def canonical_operator_payload(
    journal: JournalStore,
    action: object,
    raw_payload: Mapping[str, object],
    command_id: str,
    account_id: str,
    environment: str,
) -> dict[str, object]:
    action_name = canonical_host_action(action)
    if action_name != CONFIRM_INTENT:
        return _legacy.canonical_operator_payload(
            journal,
            action_name,
            raw_payload,
            command_id,
            account_id,
            environment,
        )
    if type(journal) is not JournalStore:
        raise TypeError("journal must be exact JournalStore")
    return _confirm_payload(
        journal,
        raw_payload,
        command_id,
        account_id,
        environment,
    )


def validate_persisted_payload(
    action: object,
    payload: object,
    payload_hash: object,
    account_id: str,
    environment: str,
) -> dict[str, object]:
    action_name = canonical_host_action(action)
    if action_name != CONFIRM_INTENT:
        return _legacy.validate_persisted_payload(
            action_name,
            payload,
            payload_hash,
            account_id,
            environment,
        )
    if type(payload) is not dict:
        raise ValueError("persisted CONFIRM_INTENT payload must be exact object")
    value = dict(payload)
    if payload_digest(value) != payload_hash:
        raise ValueError("persisted CONFIRM_INTENT payload hash mismatch")
    expected_keys = {
        "schema_version",
        "command_id",
        "confirmation_id",
        "pending_intent_id",
        "actor_id",
        "account_id",
        "environment",
        "expected_authority_epoch",
        "expected_authority_version",
        "financial_binding_hash",
    }
    if set(value) != expected_keys or value.get("schema_version") != _CONFIRM_SCHEMA_VERSION:
        raise ValueError("persisted CONFIRM_INTENT payload schema is invalid")
    command = _exact_text(value.get("command_id"), name="command_id")
    if _exact_text(value.get("confirmation_id"), name="confirmation_id") != command:
        raise ValueError("CONFIRM_INTENT confirmation_id must equal host command_id")
    _exact_text(value.get("pending_intent_id"), name="pending_intent_id")
    _exact_text(value.get("actor_id"), name="actor_id")
    if _exact_text(value.get("account_id"), name="account_id") != _exact_text(
        account_id, name="account_id"
    ):
        raise ValueError("persisted CONFIRM_INTENT account scope mismatch")
    if _exact_text(value.get("environment"), name="environment") != _exact_text(
        environment, name="environment"
    ):
        raise ValueError("persisted CONFIRM_INTENT environment scope mismatch")
    _legacy._seq(value.get("expected_authority_epoch"), "expected_authority_epoch")
    _legacy._seq(
        value.get("expected_authority_version"),
        "expected_authority_version",
    )
    _sha256(value.get("financial_binding_hash"), name="financial_binding_hash")
    return value


def _historical_binding_hash(
    journal: JournalStore,
    payload: Mapping[str, object],
):
    pending_id = _exact_text(
        payload.get("pending_intent_id"),
        name="pending_intent_id",
    )
    binding = DurablePendingIntentFinancialBindingRegistry(journal)._load(pending_id)
    if binding.binding_hash != payload.get("financial_binding_hash"):
        raise OperatorAuthorityConflict(
            "CONFIRM_INTENT durable financial binding changed"
        )
    pending, _claim = DurablePendingIntentRegistry(journal)._read(pending_id)
    raw_scope = binding.risk_policy_scope
    if type(raw_scope) is not dict:
        raise OperatorAuthorityConflict("CONFIRM_INTENT risk scope is malformed")
    try:
        scope = RiskPolicyScope(**dict(raw_scope))
        resolved = DurableRiskPolicyRegistry(journal).resolve_current(
            scope,
            journal_sequence_cut=binding.risk_policy_resolved_journal_sequence_cut,
        )
    except (TypeError, ValueError, RuntimeError) as error:
        raise OperatorAuthorityConflict(
            "CONFIRM_INTENT historical risk policy cannot be resolved"
        ) from error
    if (
        resolved.identity.policy_id != binding.risk_policy_id
        or resolved.identity.version != binding.risk_policy_version
        or resolved.identity.content_digest != binding.risk_policy_content_digest
    ):
        raise OperatorAuthorityConflict(
            "CONFIRM_INTENT historical risk policy differs from durable binding"
        )
    expected = _financial_confirmation_binding_hash(
        authority_policy_version=binding.authority_policy_version,
        risk_intent=pending.risk_intent,
        risk_policy_fingerprint=_risk_policy_fingerprint(resolved.policy),
        reservation_requirements=dict(binding.reservation_requirements),
    )
    return binding, pending, expected


def _resolved_confirmation(
    journal: JournalStore,
    payload: Mapping[str, object],
) -> AuthorityExecutionResult | None:
    confirmation_id = _exact_text(
        payload.get("confirmation_id"),
        name="confirmation_id",
    )
    expected_version = _legacy._seq(
        payload.get("expected_authority_version"),
        "expected_authority_version",
    )
    binding, pending, expected_financial_hash = _historical_binding_hash(
        journal,
        payload,
    )
    try:
        service = AuthorityService(journal)
    except (TypeError, ValueError, RuntimeError) as error:
        raise OperatorAuthorityConflict(
            "CONFIRM_INTENT durable authority state is unavailable"
        ) from error
    confirmation = service._confirmations.get(confirmation_id)
    if confirmation is None:
        return None
    if (
        confirmation.policy_id != pending.policy_id
        or confirmation.intent_hash != pending.intent_hash
        or confirmation.account_id != pending.account_id
        or confirmation.environment != pending.environment
        or confirmation.instrument_version.instrument_id != pending.instrument_id
        or confirmation.instrument_version.version != pending.instrument_version
        or confirmation.action != pending.authority_action
        or confirmation.notional != pending.notional
        or confirmation.expires_at != pending.expires_at
        or confirmation.financial_binding_hash != expected_financial_hash
    ):
        raise OperatorAuthorityConflict(
            "CONFIRM_INTENT durable confirmation differs from bound pending intent"
        )
    matches = [
        event
        for event in _legacy._events(journal)
        if event.get("event_type") == "AuthorityConfirmationAdded"
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("confirmation_id") == confirmation_id
    ]
    if len(matches) != 1:
        raise OperatorAuthorityConflict(
            "CONFIRM_INTENT durable confirmation event is missing or ambiguous"
        )
    event = matches[0]
    if (
        event.get("payload") != AuthorityService._confirmation_payload(confirmation)
        or int(event["aggregate_version"]) <= expected_version
    ):
        raise OperatorAuthorityConflict(
            "CONFIRM_INTENT durable confirmation event is outside accepted authority cut"
        )
    return AuthorityExecutionResult(
        affected_refs=("authority-confirmation:" + confirmation_id,),
        evidence=(_legacy._evidence(event),),
    )


def execute_operator_authority_action(
    journal: JournalStore,
    action: object,
    action_payload: object,
    action_payload_hash: object,
    account_id: str,
    environment: str,
    accepted_at: str,
) -> AuthorityExecutionResult:
    action_name = canonical_host_action(action)
    if action_name != CONFIRM_INTENT:
        return _legacy.execute_operator_authority_action(
            journal,
            action_name,
            action_payload,
            action_payload_hash,
            account_id,
            environment,
            accepted_at,
        )
    payload = validate_persisted_payload(
        action_name,
        action_payload,
        action_payload_hash,
        account_id,
        environment,
    )
    resolved = _resolved_confirmation(journal, payload)
    if resolved is not None:
        return resolved

    expected_version = _legacy._seq(
        payload.get("expected_authority_version"),
        "expected_authority_version",
    )
    expected_epoch = _legacy._seq(
        payload.get("expected_authority_epoch"),
        "expected_authority_epoch",
    )
    _service, state, _events, current_version = _legacy._state(journal)
    if state.get("epoch") != expected_epoch or current_version != expected_version:
        raise OperatorAuthorityConflict(
            "authority changed after CONFIRM_INTENT acceptance"
        )
    try:
        confirm_pending_intent(
            journal,
            pending_intent_id=_exact_text(
                payload.get("pending_intent_id"),
                name="pending_intent_id",
            ),
            confirmation_id=_exact_text(
                payload.get("confirmation_id"),
                name="confirmation_id",
            ),
            actor_id=_exact_text(payload.get("actor_id"), name="actor_id"),
            account_id=_exact_text(payload.get("account_id"), name="account_id"),
            environment=_exact_text(
                payload.get("environment"),
                name="environment",
            ),
            accepted_at=_accepted_datetime(accepted_at),
            expected_authority_epoch=expected_epoch,
            expected_authority_version=expected_version,
        )
    except ConfirmIntentError as error:
        raise OperatorAuthorityConflict(
            "server-owned CONFIRM_INTENT composition failed"
        ) from error
    resolved = _resolved_confirmation(journal, payload)
    if resolved is None:
        raise OperatorAuthorityConflict(
            "CONFIRM_INTENT completed without durable authority evidence"
        )
    return resolved


def observed_authority_operation_effects(
    journal: JournalStore,
    action: object,
    action_payload: object,
    action_payload_hash: object,
    account_id: str,
    environment: str,
    accepted_at: str,
) -> AuthorityExecutionResult:
    action_name = canonical_host_action(action)
    if action_name != CONFIRM_INTENT:
        return _legacy.observed_authority_operation_effects(
            journal,
            action_name,
            action_payload,
            action_payload_hash,
            account_id,
            environment,
            accepted_at,
        )
    payload = validate_persisted_payload(
        action_name,
        action_payload,
        action_payload_hash,
        account_id,
        environment,
    )
    result = _resolved_confirmation(journal, payload)
    return result or AuthorityExecutionResult((), ())


def validate_authority_success_evidence(
    journal: JournalStore,
    action: object,
    action_payload: object,
    action_payload_hash: object,
    account_id: str,
    environment: str,
    accepted_at: str,
    affected_refs: tuple[str, ...],
    evidence: tuple[Mapping[str, object], ...],
) -> None:
    action_name = canonical_host_action(action)
    if action_name != CONFIRM_INTENT:
        _legacy.validate_authority_success_evidence(
            journal,
            action_name,
            action_payload,
            action_payload_hash,
            account_id,
            environment,
            accepted_at,
            affected_refs,
            evidence,
        )
        return
    payload = validate_persisted_payload(
        action_name,
        action_payload,
        action_payload_hash,
        account_id,
        environment,
    )
    result = _resolved_confirmation(journal, payload)
    if result is None:
        raise ValueError("terminal CONFIRM_INTENT has no canonical authority outcome")
    if tuple(affected_refs) != result.affected_refs:
        raise ValueError("terminal CONFIRM_INTENT affected_refs do not match journal")
    if tuple(dict(item) for item in evidence) != tuple(
        dict(item) for item in result.evidence
    ):
        raise ValueError("terminal CONFIRM_INTENT evidence does not match journal")
