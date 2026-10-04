"""Canonical dispatch bridge for one sealed provider route.

The generic GuardedDispatcher owns the persist-before-send state machine and
invokes ``authority_check`` twice: once before transport entry and again inside
the provider wrapper's ``final_guard`` immediately before the durable
SubmissionSending transition. This bridge composes the sealed route's exact C
and Q identities into that existing authority seam rather than inventing a
second send state machine.

A request selected under C1/Q1 remains bound to C1/Q1 for its whole life. If
either authority expires or is superseded while the request is waiting, the
final barrier rejects the old identity with zero outbound side effects. Newer
C2/Q2 values never upgrade an already-prepared request implicitly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .capabilities import CapabilityError
from .dispatch import (
    AuthorityCheck,
    DispatchOutcome,
    GuardedDispatcher,
    SenderCheck,
    TransportSend,
)
from .durable_capabilities import DurableCapabilityRegistry
from .durable_provider_qualification import DurableProviderQualificationRegistry
from .provider_qualification_authority import ProviderQualificationError
from .provider_qualification_current_scope import ProviderQualificationCurrentScope
from .provider_selection import ProviderSelectionError, SelectedProviderRoute


class ProviderRouteDispatchError(ValueError):
    """The selected route cannot safely cross the canonical send boundary."""


_RESERVED_SCOPE_KEYS = frozenset(
    {
        "provider_route_qualification_id",
        "provider_route_capability_snapshot_id",
        "provider_route_decision_journal_sequence_cut",
        "provider_route_provider_environment",
        "provider_route_adapter_code_sha",
        "provider_route_packaged_artifact_digest",
        "provider_route_protocol_id",
        "provider_route_protocol_version",
        "provider_route_entity_policy_id",
        "provider_route_entity_id",
        "provider_route_product_family",
        "provider_route_semantics_digest",
    }
)


def _point(value: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderRouteDispatchError("barrier time must be canonical text")
    try:
        point = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProviderRouteDispatchError("barrier time must be ISO date-time text") from error
    if point.tzinfo is None or point.utcoffset() is None:
        raise ProviderRouteDispatchError("barrier time must be timezone-aware")
    return point.astimezone(timezone.utc)


def _journal_cut(store: object) -> int:
    cut = store.whole_store_state_cut()
    if type(cut) is not dict:
        raise ProviderRouteDispatchError("whole-store barrier cut is non-canonical")
    sequence = cut.get("journal_sequence")
    if type(sequence) is not int or sequence < 0:
        raise ProviderRouteDispatchError("whole-store barrier cut lacks canonical sequence")
    return sequence


def _current_scope(route: SelectedProviderRoute) -> ProviderQualificationCurrentScope:
    qualification = route.qualification
    return ProviderQualificationCurrentScope(
        provider_scope=qualification.scope.provider_scope,
        product_family=qualification.scope.product_family,
        adapter_source_git_sha=qualification.scope.adapter_source_git_sha,
        packaged_artifact_digest=qualification.scope.packaged_artifact_digest,
        protocol_id=qualification.scope.protocol_id,
        protocol_version=qualification.scope.protocol_version,
    )


def _require_bound_route(
    *,
    dispatcher: GuardedDispatcher,
    route: SelectedProviderRoute,
    capability_registry: DurableCapabilityRegistry,
    qualification_registry: DurableProviderQualificationRegistry,
) -> None:
    if type(dispatcher) is not GuardedDispatcher:
        raise TypeError("dispatcher must be exact GuardedDispatcher")
    if type(route) is not SelectedProviderRoute:
        raise TypeError("route must be exact SelectedProviderRoute")
    if type(capability_registry) is not DurableCapabilityRegistry:
        raise TypeError("capability_registry must be exact DurableCapabilityRegistry")
    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    if (
        capability_registry.store is not dispatcher.store
        or qualification_registry.store is not dispatcher.store
    ):
        raise ProviderRouteDispatchError(
            "route C/Q authority and dispatcher must share one JournalStore instance"
        )

    candidate = route.candidate
    capability = route.capability
    qualification = route.qualification
    provider_scope = qualification.scope.provider_scope

    if dispatcher.environment != provider_scope.runtime_environment:
        raise ProviderRouteDispatchError("dispatcher runtime environment differs from Q")
    if dispatcher.account_id != candidate.account_id:
        raise ProviderRouteDispatchError("dispatcher account differs from selected route")
    if capability.account_id != candidate.account_id:
        raise ProviderRouteDispatchError("capability account differs from selected route")
    if capability.entity_id != candidate.entity_id:
        raise ProviderRouteDispatchError("capability entity differs from selected route")
    if capability.environment != dispatcher.environment:
        raise ProviderRouteDispatchError("capability runtime environment differs from dispatcher")
    if capability.provider_id.upper() != candidate.provider_id:
        raise ProviderRouteDispatchError("capability provider differs from selected route")
    if capability.provider_environment != candidate.provider_environment:
        raise ProviderRouteDispatchError(
            "capability provider environment differs from selected route"
        )
    if provider_scope.provider_id != candidate.provider_id:
        raise ProviderRouteDispatchError("Q provider differs from selected route")
    if provider_scope.provider_environment != candidate.provider_environment:
        raise ProviderRouteDispatchError(
            "Q provider environment differs from selected route"
        )
    if provider_scope.entity_policy_id != candidate.entity_policy_id:
        raise ProviderRouteDispatchError("Q entity policy differs from selected route")
    if qualification.scope.product_family != candidate.product_family:
        raise ProviderRouteDispatchError("Q product family differs from selected route")
    if qualification.scope.adapter_source_git_sha != candidate.adapter_code_sha:
        raise ProviderRouteDispatchError("Q adapter build differs from selected route")
    if qualification.scope.packaged_artifact_digest != candidate.packaged_artifact_digest:
        raise ProviderRouteDispatchError("Q package digest differs from selected route")
    if qualification.scope.protocol_id != candidate.protocol_id:
        raise ProviderRouteDispatchError("Q protocol differs from selected route")
    if qualification.scope.protocol_version != candidate.protocol_version:
        raise ProviderRouteDispatchError("Q protocol version differs from selected route")


def _bound_submission_scope(
    route: SelectedProviderRoute,
    submission_scope: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if submission_scope is None:
        result: dict[str, Any] = {}
    else:
        if not isinstance(submission_scope, Mapping):
            raise TypeError("submission_scope must be a mapping")
        result = dict(submission_scope)
    collision = _RESERVED_SCOPE_KEYS.intersection(result)
    if collision:
        raise ProviderRouteDispatchError(
            "submission_scope attempts to override provider-route authority fields"
        )
    candidate = route.candidate
    result.update(
        {
            "provider_route_qualification_id": route.qualification_id,
            "provider_route_capability_snapshot_id": route.capability_snapshot_id,
            "provider_route_decision_journal_sequence_cut": (
                route.decision_journal_sequence_cut
            ),
            "provider_route_provider_environment": candidate.provider_environment,
            "provider_route_adapter_code_sha": candidate.adapter_code_sha,
            "provider_route_packaged_artifact_digest": candidate.packaged_artifact_digest,
            "provider_route_protocol_id": candidate.protocol_id,
            "provider_route_protocol_version": candidate.protocol_version,
            "provider_route_entity_policy_id": candidate.entity_policy_id,
            "provider_route_entity_id": candidate.entity_id,
            "provider_route_product_family": candidate.product_family,
            "provider_route_semantics_digest": (
                route.qualification.identity.route_semantics_digest
            ),
        }
    )
    return result


def dispatch_selected_provider_route(
    dispatcher: GuardedDispatcher,
    route: SelectedProviderRoute,
    qualification_registry: DurableProviderQualificationRegistry,
    *,
    capability_registry: DurableCapabilityRegistry,
    attempt_id: str,
    intent_id: str,
    intent_hash: str,
    request: Mapping[str, Any],
    now: str,
    authority_check: AuthorityCheck,
    transport_send: TransportSend,
    client_id_max_length: int = 32,
    client_id_format: str = "TOKEN",
    final_barrier_clock: Callable[[], str] | None = None,
    sender_check: SenderCheck | None = None,
    submission_scope: Mapping[str, Any] | None = None,
) -> DispatchOutcome:
    """Dispatch one selected route with expected C1/Q1 pinned through final_guard.

    The supplied non-provider authority check retains ownership of risk, intent,
    reconciliation and other product gates. This bridge adds independently
    durable provider C/Q currentness and does not turn either into permission to
    trade.
    """

    _require_bound_route(
        dispatcher=dispatcher,
        route=route,
        capability_registry=capability_registry,
        qualification_registry=qualification_registry,
    )
    if not callable(authority_check):
        raise TypeError("authority_check must be callable")

    candidate = route.candidate
    expected_capability_id = route.capability_snapshot_id
    expected_qualification_id = route.qualification_id
    current_scope = _current_scope(route)

    def combined_authority_check(intent_hash_value: str, at_text: str) -> tuple[bool, str]:
        upstream = authority_check(intent_hash_value, at_text)
        if type(upstream) is not tuple or len(upstream) != 2:
            return False, "upstream_authority_invalid_result"
        allowed, reason = upstream
        if type(allowed) is not bool:
            return False, "upstream_authority_invalid_allowed"
        if type(reason) is not str or not reason.strip():
            return False, "upstream_authority_invalid_reason"
        if not allowed:
            return False, reason.strip()
        point = _point(at_text)
        cut = _journal_cut(dispatcher.store)
        try:
            capability = capability_registry.require_verified(
                provider_id=candidate.provider_id,
                account_id=candidate.account_id,
                entity_id=candidate.entity_id,
                environment=dispatcher.environment,
                provider_environment=candidate.provider_environment,
                instrument_version=route.capability.instrument_version,
                at=point,
                journal_sequence_cut=cut,
            )
        except (CapabilityError, ValueError, TypeError):
            return False, "provider_capability_not_exact_current"
        if capability.snapshot_id != expected_capability_id:
            return False, "provider_capability_not_exact_current"
        try:
            current = qualification_registry.require_exact_current(
                scope=current_scope,
                at=point,
                expected_qualification_id=expected_qualification_id,
                journal_sequence_cut=cut,
            )
        except (ProviderQualificationError, ProviderSelectionError, ValueError, TypeError):
            return False, "provider_qualification_not_exact_current"
        if (
            current.qualification_id != expected_qualification_id
            or current.journal_sequence_cut != cut
        ):
            return False, "provider_qualification_not_exact_current"
        if _journal_cut(dispatcher.store) != cut:
            return False, "provider_route_authority_changed_during_barrier"
        return True, reason.strip()

    return GuardedDispatcher.dispatch(
        dispatcher,
        attempt_id=attempt_id,
        intent_id=intent_id,
        intent_hash=intent_hash,
        provider=route.candidate.provider_id,
        request=request,
        now=now,
        authority_check=combined_authority_check,
        transport_send=transport_send,
        client_id_max_length=client_id_max_length,
        client_id_format=client_id_format,
        final_barrier_clock=final_barrier_clock,
        sender_check=sender_check,
        submission_scope=_bound_submission_scope(route, submission_scope),
    )
