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


# Retain installed authority callables once. The CONFIRM implementation resolves
# its private _resolved_confirmation global at call time, so merely assigning a
# hardened resolver into that module is not sufficient: a late rebind could
# otherwise remove pending-claim proof. The exported Host callables below are
# built from closures whose authority tuple is not present in their public
# signature and therefore cannot be supplied or replaced by a caller.
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


def _make_resolved_confirmation(
    base_resolved,
    registry_type,
    pending_read,
    conflict_type,
):
    def resolved_confirmation(
        journal: JournalStore,
        payload: Mapping[str, object],
    ):
        result = base_resolved(journal, payload)
        if result is None:
            return None
        pending_id = payload.get("pending_intent_id")
        confirmation_id = payload.get("confirmation_id")
        actor_id = payload.get("actor_id")
        try:
            pending, claim = pending_read(registry_type(journal), pending_id)
        except (TypeError, ValueError, PendingIntentError) as error:
            raise conflict_type(
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
            raise conflict_type(
                "CONFIRM_INTENT durable authority event lacks exact pending claim proof"
            )
        return result

    return resolved_confirmation


_resolved_confirmation = _make_resolved_confirmation(
    _base_resolved_confirmation,
    _PENDING_INTENT_REGISTRY,
    _PENDING_INTENT_READ,
    OperatorAuthorityConflict,
)

# Keep direct callers of the retained implementation hardened too. The Host
# exports below independently retain _resolved_confirmation, so a later rebind
# of this implementation-module name cannot weaken the durable Host path.
_confirm_impl._resolved_confirmation = _resolved_confirmation

canonical_operator_payload = _BASE_CANONICAL_OPERATOR_PAYLOAD
validate_persisted_payload = _BASE_VALIDATE_PERSISTED_PAYLOAD


def _make_execute_operator_authority_action(
    delegate,
    resolver,
    canonical_action,
    validate_payload,
    conflict_type,
    confirm_action,
):
    def execute_operator_authority_action(
        journal: JournalStore,
        action: object,
        action_payload: object,
        action_payload_hash: object,
        account_id: str,
        environment: str,
        accepted_at: str,
    ):
        action_name = canonical_action(action)
        result = delegate(
            journal,
            action_name,
            action_payload,
            action_payload_hash,
            account_id,
            environment,
            accepted_at,
        )
        if action_name != confirm_action:
            return result
        payload = validate_payload(
            action_name,
            action_payload,
            action_payload_hash,
            account_id,
            environment,
        )
        expected = resolver(journal, payload)
        if expected is None:
            raise conflict_type(
                "CONFIRM_INTENT execution lacks exact terminal authority proof"
            )
        if (
            result.affected_refs != expected.affected_refs
            or result.evidence != expected.evidence
        ):
            raise conflict_type(
                "CONFIRM_INTENT execution result differs from retained terminal proof"
            )
        return expected

    return execute_operator_authority_action


execute_operator_authority_action = _make_execute_operator_authority_action(
    _BASE_EXECUTE_OPERATOR_AUTHORITY_ACTION,
    _resolved_confirmation,
    _CANONICAL_HOST_ACTION,
    _BASE_VALIDATE_PERSISTED_PAYLOAD,
    OperatorAuthorityConflict,
    _CONFIRM_INTENT,
)


def _make_observed_authority_operation_effects(
    delegate,
    resolver,
    canonical_action,
    validate_payload,
    result_type,
    conflict_type,
    confirm_action,
):
    def observed_authority_operation_effects(
        journal,
        action,
        action_payload,
        action_payload_hash,
        account_id,
        environment,
        accepted_at,
    ):
        action_name = canonical_action(action)
        if action_name != confirm_action:
            return delegate(
                journal,
                action_name,
                action_payload,
                action_payload_hash,
                account_id,
                environment,
                accepted_at,
            )
        payload = validate_payload(
            action_name,
            action_payload,
            action_payload_hash,
            account_id,
            environment,
        )
        try:
            result = resolver(journal, payload)
        except conflict_type:
            return result_type((), ())
        return result or result_type((), ())

    return observed_authority_operation_effects


observed_authority_operation_effects = _make_observed_authority_operation_effects(
    _BASE_OBSERVED_AUTHORITY_OPERATION_EFFECTS,
    _resolved_confirmation,
    _CANONICAL_HOST_ACTION,
    _BASE_VALIDATE_PERSISTED_PAYLOAD,
    AuthorityExecutionResult,
    OperatorAuthorityConflict,
    _CONFIRM_INTENT,
)


def _make_validate_authority_success_evidence(
    delegate,
    resolver,
    canonical_action,
    validate_payload,
    confirm_action,
):
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
        action_name = canonical_action(action)
        if action_name != confirm_action:
            delegate(
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
        payload = validate_payload(
            action_name,
            action_payload,
            action_payload_hash,
            account_id,
            environment,
        )
        result = resolver(journal, payload)
        if result is None:
            raise ValueError("terminal CONFIRM_INTENT has no canonical authority outcome")
        if tuple(affected_refs) != result.affected_refs:
            raise ValueError("terminal CONFIRM_INTENT affected_refs do not match journal")
        if tuple(dict(item) for item in evidence) != tuple(
            dict(item) for item in result.evidence
        ):
            raise ValueError("terminal CONFIRM_INTENT evidence does not match journal")

    return validate_authority_success_evidence


validate_authority_success_evidence = _make_validate_authority_success_evidence(
    _BASE_VALIDATE_AUTHORITY_SUCCESS_EVIDENCE,
    _resolved_confirmation,
    _CANONICAL_HOST_ACTION,
    _BASE_VALIDATE_PERSISTED_PAYLOAD,
    _CONFIRM_INTENT,
)


def __getattr__(name: str):
    """Preserve the historical module surface for white-box tests/callers."""

    try:
        return getattr(_confirm_impl, name)
    except AttributeError:
        return getattr(_legacy, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_confirm_impl)) | set(dir(_legacy)))
