"""Hardened operator-authority dispatch compatibility surface.

The retained legacy module remains byte-identical for pre-existing actions. The
CONFIRM_INTENT implementation is retained separately and this final shim adds
one terminal invariant: a durable AuthorityConfirmationAdded is successful host
proof only when the same pending intent was durably claimed by the exact
accepted confirmation id and authenticated actor.
"""

from __future__ import annotations

from typing import Mapping

from . import operator_authority_commands_confirm_impl as _confirm_impl
from . import operator_authority_commands_legacy as _legacy
from .pending_intents import DurablePendingIntentRegistry, PendingIntentError
from .persistence import JournalStore


OperatorAuthorityConflict = _confirm_impl.OperatorAuthorityConflict
AuthorityExecutionResult = _confirm_impl.AuthorityExecutionResult
authenticated_host_actor_scope = _confirm_impl.authenticated_host_actor_scope


# Retain the installed terminal-proof path once.  The host's retained legacy
# implementation imports our exported functions at module construction, but the
# CONFIRM implementation itself resolves its private _resolved_confirmation
# global at call time.  A later module-global rebind must therefore not be able
# to remove the pending-claim proof from execution, observation, or restart
# validation.
_base_resolved_confirmation = _confirm_impl._resolved_confirmation
_BASE_EXECUTE_OPERATOR_AUTHORITY_ACTION = (
    _confirm_impl.execute_operator_authority_action
)
_BASE_OBSERVED_AUTHORITY_OPERATION_EFFECTS = (
    _confirm_impl.observed_authority_operation_effects
)
_BASE_VALIDATE_AUTHORITY_SUCCESS_EVIDENCE = (
    _confirm_impl.validate_authority_success_evidence
)
_BASE_VALIDATE_PERSISTED_PAYLOAD = _confirm_impl.validate_persisted_payload
_BASE_CANONICAL_OPERATOR_PAYLOAD = _confirm_impl.canonical_operator_payload
_CANONICAL_HOST_ACTION = _confirm_impl.canonical_host_action
_PENDING_INTENT_REGISTRY = DurablePendingIntentRegistry
_PENDING_INTENT_READ = DurablePendingIntentRegistry._read
_CONFIRM_INTENT = _confirm_impl.CONFIRM_INTENT


def _resolved_confirmation(
    journal: JournalStore,
    payload: Mapping[str, object],
    *,
    _base_resolved=_base_resolved_confirmation,
    _registry_type=_PENDING_INTENT_REGISTRY,
    _pending_read=_PENDING_INTENT_READ,
    _conflict_type=OperatorAuthorityConflict,
):
    result = _base_resolved(journal, payload)
    if result is None:
        return None
    pending_id = payload.get("pending_intent_id")
    confirmation_id = payload.get("confirmation_id")
    actor_id = payload.get("actor_id")
    try:
        pending, claim = _pending_read(_registry_type(journal), pending_id)
    except (TypeError, ValueError, PendingIntentError) as error:
        raise _conflict_type(
            "CONFIRM_INTENT pending claim proof is unavailable"
        ) from error
    if (
        type(claim) is not dict
        or claim.get("pending_intent_id") != pending_id
        or claim.get("registered_event_id")
        != f"pending-intent:{pending_id}:registered"
        or claim.get("intent_hash") != pending.intent_hash
        or claim.get("confirmation_id") != confirmation_id
        or claim.get("actor_id") != actor_id
    ):
        raise _conflict_type(
            "CONFIRM_INTENT durable authority event lacks exact pending claim proof"
        )
    return result


# Keep the retained implementation's own direct callers hardened too, while the
# wrappers below independently retain this exact resolver so a later rebind of
# the implementation module cannot weaken the Host path.
_confirm_impl._resolved_confirmation = _resolved_confirmation

canonical_operator_payload = _BASE_CANONICAL_OPERATOR_PAYLOAD
validate_persisted_payload = _BASE_VALIDATE_PERSISTED_PAYLOAD


def execute_operator_authority_action(
    journal: JournalStore,
    action: object,
    action_payload: object,
    action_payload_hash: object,
    account_id: str,
    environment: str,
    accepted_at: str,
    *,
    _delegate=_BASE_EXECUTE_OPERATOR_AUTHORITY_ACTION,
    _resolver=_resolved_confirmation,
    _canonical_action=_CANONICAL_HOST_ACTION,
    _validate_payload=_BASE_VALIDATE_PERSISTED_PAYLOAD,
    _conflict_type=OperatorAuthorityConflict,
):
    """Execute with an independently retained terminal pending-claim proof."""

    action_name = _canonical_action(action)
    result = _delegate(
        journal,
        action_name,
        action_payload,
        action_payload_hash,
        account_id,
        environment,
        accepted_at,
    )
    if action_name != _CONFIRM_INTENT:
        return result
    payload = _validate_payload(
        action_name,
        action_payload,
        action_payload_hash,
        account_id,
        environment,
    )
    expected = _resolver(journal, payload)
    if expected is None:
        raise _conflict_type(
            "CONFIRM_INTENT execution lacks exact terminal authority proof"
        )
    if (
        result.affected_refs != expected.affected_refs
        or result.evidence != expected.evidence
    ):
        raise _conflict_type(
            "CONFIRM_INTENT execution result differs from retained terminal proof"
        )
    return expected


def observed_authority_operation_effects(
    journal,
    action,
    action_payload,
    action_payload_hash,
    account_id,
    environment,
    accepted_at,
    *,
    _delegate=_BASE_OBSERVED_AUTHORITY_OPERATION_EFFECTS,
    _resolver=_resolved_confirmation,
    _canonical_action=_CANONICAL_HOST_ACTION,
    _validate_payload=_BASE_VALIDATE_PERSISTED_PAYLOAD,
    _result_type=AuthorityExecutionResult,
    _conflict_type=OperatorAuthorityConflict,
):
    """Invalid confirmation evidence is observed as no attributable success."""

    action_name = _canonical_action(action)
    if action_name != _CONFIRM_INTENT:
        return _delegate(
            journal,
            action_name,
            action_payload,
            action_payload_hash,
            account_id,
            environment,
            accepted_at,
        )
    payload = _validate_payload(
        action_name,
        action_payload,
        action_payload_hash,
        account_id,
        environment,
    )
    try:
        result = _resolver(journal, payload)
    except _conflict_type:
        return _result_type((), ())
    return result or _result_type((), ())


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
    *,
    _delegate=_BASE_VALIDATE_AUTHORITY_SUCCESS_EVIDENCE,
    _resolver=_resolved_confirmation,
    _canonical_action=_CANONICAL_HOST_ACTION,
    _validate_payload=_BASE_VALIDATE_PERSISTED_PAYLOAD,
) -> None:
    """Revalidate terminal Host success from the retained canonical proof."""

    action_name = _canonical_action(action)
    if action_name != _CONFIRM_INTENT:
        _delegate(
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
    payload = _validate_payload(
        action_name,
        action_payload,
        action_payload_hash,
        account_id,
        environment,
    )
    result = _resolver(journal, payload)
    if result is None:
        raise ValueError("terminal CONFIRM_INTENT has no canonical authority outcome")
    if tuple(affected_refs) != result.affected_refs:
        raise ValueError("terminal CONFIRM_INTENT affected_refs do not match journal")
    if tuple(dict(item) for item in evidence) != tuple(
        dict(item) for item in result.evidence
    ):
        raise ValueError("terminal CONFIRM_INTENT evidence does not match journal")


def __getattr__(name: str):
    """Preserve the historical module surface for white-box tests/callers."""

    try:
        return getattr(_confirm_impl, name)
    except AttributeError:
        return getattr(_legacy, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_confirm_impl)) | set(dir(_legacy)))
