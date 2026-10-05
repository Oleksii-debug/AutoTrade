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


_base_resolved_confirmation = _confirm_impl._resolved_confirmation


def _resolved_confirmation(
    journal: JournalStore,
    payload: Mapping[str, object],
):
    result = _base_resolved_confirmation(journal, payload)
    if result is None:
        return None
    pending_id = payload.get("pending_intent_id")
    confirmation_id = payload.get("confirmation_id")
    actor_id = payload.get("actor_id")
    try:
        _pending, claim = DurablePendingIntentRegistry(journal)._read(pending_id)
    except (TypeError, ValueError, PendingIntentError) as error:
        raise OperatorAuthorityConflict(
            "CONFIRM_INTENT pending claim proof is unavailable"
        ) from error
    if (
        type(claim) is not dict
        or claim.get("confirmation_id") != confirmation_id
        or claim.get("actor_id") != actor_id
    ):
        raise OperatorAuthorityConflict(
            "CONFIRM_INTENT durable authority event lacks exact pending claim proof"
        )
    return result


# The retained implementation resolves this global at call time. Install the
# stronger read-only terminal proof before exporting any dispatcher entry point.
_confirm_impl._resolved_confirmation = _resolved_confirmation

canonical_operator_payload = _confirm_impl.canonical_operator_payload
validate_persisted_payload = _confirm_impl.validate_persisted_payload
execute_operator_authority_action = _confirm_impl.execute_operator_authority_action


def observed_authority_operation_effects(
    journal,
    action,
    action_payload,
    action_payload_hash,
    account_id,
    environment,
    accepted_at,
):
    """Invalid confirmation evidence is observed as no attributable success."""

    try:
        return _confirm_impl.observed_authority_operation_effects(
            journal,
            action,
            action_payload,
            action_payload_hash,
            account_id,
            environment,
            accepted_at,
        )
    except OperatorAuthorityConflict:
        if action == _confirm_impl.CONFIRM_INTENT:
            return AuthorityExecutionResult((), ())
        raise


validate_authority_success_evidence = (
    _confirm_impl.validate_authority_success_evidence
)


def __getattr__(name: str):
    """Preserve the historical module surface for white-box tests/callers."""

    try:
        return getattr(_confirm_impl, name)
    except AttributeError:
        return getattr(_legacy, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_confirm_impl)) | set(dir(_legacy)))
