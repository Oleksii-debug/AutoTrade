"""Hardened operator-authority dispatch compatibility surface.

The retained legacy module remains byte-identical for pre-existing actions. The
CONFIRM_INTENT implementation is retained separately and this final shim adds
terminal invariants that are independently reconstructable from durable journal
chronology: exact pending claim, accepted authority successor, and the bound
RiskPolicy activation episode immediately before confirmation.
"""

from __future__ import annotations

from typing import Mapping

from . import operator_authority_commands_confirm_impl as _confirm_impl
from . import operator_authority_commands_legacy as _legacy
from .pending_intent_financial_binding import (
    DurablePendingIntentFinancialBindingRegistry,
)
from .pending_intents import DurablePendingIntentRegistry, PendingIntentError
from .persistence import JournalStore
from .risk_policy_authority import (
    DurableRiskPolicyRegistry,
    RiskPolicyScope,
)


OperatorAuthorityConflict = _confirm_impl.OperatorAuthorityConflict
AuthorityExecutionResult = _confirm_impl.AuthorityExecutionResult
authenticated_host_actor_scope = _confirm_impl.authenticated_host_actor_scope


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
_PENDING_INTENT_EVENTS = DurablePendingIntentRegistry._events
_BINDING_REGISTRY = DurablePendingIntentFinancialBindingRegistry
_BINDING_LOAD = DurablePendingIntentFinancialBindingRegistry._load
_RISK_REGISTRY = DurableRiskPolicyRegistry
_RISK_RESOLVE = DurableRiskPolicyRegistry.resolve_current
_RISK_SCOPE = RiskPolicyScope
_JOURNAL_LOAD_EVENTS = JournalStore.load_events
_CONFIRM_INTENT = _confirm_impl.CONFIRM_INTENT


def _make_resolved_confirmation(
    base_resolved,
    pending_registry_type,
    pending_read,
    pending_events,
    binding_registry_type,
    binding_load,
    risk_registry_type,
    risk_resolve,
    risk_scope_type,
    journal_load_events,
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
            pending_registry = pending_registry_type(journal)
            pending, claim = pending_read(pending_registry, pending_id)
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

        # Reconstruct chronology independently of the mutable implementation
        # module. A claim written after a confirmation is not causation, and an
        # authority event later than accepted_version+1 proves intervening
        # authority state rather than the accepted command's exact successor.
        try:
            pending_history = pending_events(pending_registry, pending_id)
            claim_events = [
                event
                for event in pending_history
                if event.get("event_type")
                == "PendingFinancialIntentConfirmationClaimed"
                and event.get("payload") == claim
            ]
            authority_history = journal_load_events(
                journal,
                "authority_state",
                "canonical",
            )
            confirmation_events = [
                event
                for event in authority_history
                if event.get("event_type") == "AuthorityConfirmationAdded"
                and type(event.get("payload")) is dict
                and event["payload"].get("confirmation_id") == confirmation_id
            ]
            if len(claim_events) != 1 or len(confirmation_events) != 1:
                raise conflict_type(
                    "CONFIRM_INTENT durable causal events are missing or ambiguous"
                )
            claim_sequence = claim_events[0].get("journal_sequence")
            confirmation_event = confirmation_events[0]
            confirmation_sequence = confirmation_event.get("journal_sequence")
            confirmation_version = confirmation_event.get("aggregate_version")
            raw_expected_version = payload.get("expected_authority_version")
            if (
                type(claim_sequence) is not int
                or claim_sequence < 1
                or type(confirmation_sequence) is not int
                or confirmation_sequence < 2
                or claim_sequence >= confirmation_sequence
                or type(confirmation_version) is not int
                or type(raw_expected_version) is not str
                or not raw_expected_version.isdigit()
                or confirmation_version != int(raw_expected_version) + 1
            ):
                raise conflict_type(
                    "CONFIRM_INTENT durable chronology is outside accepted authority cut"
                )

            binding_registry = binding_registry_type(journal)
            binding = binding_load(binding_registry, pending_id)
            raw_scope = binding.risk_policy_scope
            if type(raw_scope) is not dict:
                raise conflict_type(
                    "CONFIRM_INTENT durable risk policy scope is malformed"
                )
            scope = risk_scope_type(**dict(raw_scope))
            immediate_predecessor_policy = risk_resolve(
                risk_registry_type(journal),
                scope,
                journal_sequence_cut=confirmation_sequence - 1,
            )
            if (
                immediate_predecessor_policy.registration_event_id
                != binding.risk_policy_registration_event_id
                or immediate_predecessor_policy.activation_event_id
                != binding.risk_policy_activation_event_id
                or immediate_predecessor_policy.identity.policy_id
                != binding.risk_policy_id
                or immediate_predecessor_policy.identity.version
                != binding.risk_policy_version
                or immediate_predecessor_policy.identity.content_digest
                != binding.risk_policy_content_digest
            ):
                raise conflict_type(
                    "CONFIRM_INTENT bound RiskPolicy episode was not active immediately before confirmation"
                )
        except conflict_type:
            raise
        except (TypeError, ValueError, RuntimeError) as error:
            raise conflict_type(
                "CONFIRM_INTENT durable causal chronology cannot be reconstructed"
            ) from error
        return result

    return resolved_confirmation


_resolved_confirmation = _make_resolved_confirmation(
    _base_resolved_confirmation,
    _PENDING_INTENT_REGISTRY,
    _PENDING_INTENT_READ,
    _PENDING_INTENT_EVENTS,
    _BINDING_REGISTRY,
    _BINDING_LOAD,
    _RISK_REGISTRY,
    _RISK_RESOLVE,
    _RISK_SCOPE,
    _JOURNAL_LOAD_EVENTS,
    OperatorAuthorityConflict,
)

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
