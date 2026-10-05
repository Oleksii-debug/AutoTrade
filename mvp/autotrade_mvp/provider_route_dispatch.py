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
from .persistence import JournalStore
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
    if type(store) is not JournalStore:
        raise TypeError("store must be exact JournalStore")
    cut = JournalStore.whole_store_state_cut(store)
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


def _require_bound_route_authority(
    *,
    store: JournalStore,
    environment: str,
    account_id: str,
    route: SelectedProviderRoute,
    capability_registry: DurableCapabilityRegistry,
    qualification_registry: DurableProviderQualificationRegistry,
) -> None:
    if type(store) is not JournalStore:
        raise TypeError("store must be exact JournalStore")
    if type(environment) is not str or not environment:
        raise TypeError("environment must be exact non-empty text")
    if type(account_id) is not str or not account_id:
        raise TypeError("account_id must be exact non-empty text")
    if type(route) is not SelectedProviderRoute:
        raise TypeError("route must be exact SelectedProviderRoute")
    if type(capability_registry) is not DurableCapabilityRegistry:
        raise TypeError("capability_registry must be exact DurableCapabilityRegistry")
    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    if capability_registry.store is not store or qualification_registry.store is not store:
        raise ProviderRouteDispatchError(
            "route C/Q authority and dispatcher must share one JournalStore instance"
        )

    candidate = route.candidate
    capability = route.capability
    qualification = route.qualification
    provider_scope = qualification.scope.provider_scope

    if environment != provider_scope.runtime_environment:
        raise ProviderRouteDispatchError("dispatcher runtime environment differs from Q")
    if account_id != candidate.account_id:
        raise ProviderRouteDispatchError("dispatcher account differs from selected route")
    if capability.account_id != candidate.account_id:
        raise ProviderRouteDispatchError("capability account differs from selected route")
    if capability.entity_id != candidate.entity_id:
        raise ProviderRouteDispatchError("capability entity differs from selected route")
    if capability.environment != environment:
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


def _require_bound_route(
    *,
    dispatcher: GuardedDispatcher,
    route: SelectedProviderRoute,
    capability_registry: DurableCapabilityRegistry,
    qualification_registry: DurableProviderQualificationRegistry,
) -> None:
    if type(dispatcher) is not GuardedDispatcher:
        raise TypeError("dispatcher must be exact GuardedDispatcher")
    _require_bound_route_authority(
        store=dispatcher.store,
        environment=dispatcher.environment,
        account_id=dispatcher.account_id,
        route=route,
        capability_registry=capability_registry,
        qualification_registry=qualification_registry,
    )


def bind_selected_provider_route_submission_scope(
    route: SelectedProviderRoute,
    submission_scope: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Durably name the exact selected C/Q/build identity on one send attempt."""

    if type(route) is not SelectedProviderRoute:
        raise TypeError("route must be exact SelectedProviderRoute")
    if submission_scope is None:
        result: dict[str, Any] = {}
    else:
        if type(submission_scope) is not dict:
            raise TypeError("submission_scope must be an exact dict")
        if any(type(key) is not str for key in submission_scope):
            raise TypeError("submission_scope keys must be exact strings")
        result = dict.copy(submission_scope)
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
        }
    )
    return result


def _install_provider_route_authority_composer() -> Callable[..., AuthorityCheck]:
    """Closure-bind every executable used by the provider C/Q final barrier.

    The returned composer still fails closed if a module/class name is retargeted,
    but it never invokes the replacement to discover the mismatch. This closes
    the pre-composition gap where a forged helper could previously run before the
    per-guard executable snapshot was created.
    """

    point_function = _point
    point_code = point_function.__code__
    journal_cut_function = _journal_cut
    journal_cut_code = journal_cut_function.__code__
    current_scope_function = _current_scope
    current_scope_code = current_scope_function.__code__
    bound_route_function = _require_bound_route_authority
    bound_route_code = bound_route_function.__code__

    journal_store_type = JournalStore
    whole_store_cut_function = journal_store_type.whole_store_state_cut
    whole_store_cut_code = whole_store_cut_function.__code__
    capability_registry_type = DurableCapabilityRegistry
    capability_reader = capability_registry_type.require_verified
    capability_reader_code = capability_reader.__code__
    qualification_registry_type = DurableProviderQualificationRegistry
    qualification_reader = qualification_registry_type.require_exact_current
    qualification_reader_code = qualification_reader.__code__
    current_scope_type = ProviderQualificationCurrentScope
    selected_route_type = SelectedProviderRoute

    def module_authority_current() -> bool:
        return (
            _point is point_function
            and point_function.__code__ is point_code
            and _journal_cut is journal_cut_function
            and journal_cut_function.__code__ is journal_cut_code
            and _current_scope is current_scope_function
            and current_scope_function.__code__ is current_scope_code
            and _require_bound_route_authority is bound_route_function
            and bound_route_function.__code__ is bound_route_code
            and JournalStore is journal_store_type
            and journal_store_type.whole_store_state_cut is whole_store_cut_function
            and whole_store_cut_function.__code__ is whole_store_cut_code
            and DurableCapabilityRegistry is capability_registry_type
            and capability_registry_type.require_verified is capability_reader
            and capability_reader.__code__ is capability_reader_code
            and DurableProviderQualificationRegistry is qualification_registry_type
            and qualification_registry_type.require_exact_current is qualification_reader
            and qualification_reader.__code__ is qualification_reader_code
            and ProviderQualificationCurrentScope is current_scope_type
            and SelectedProviderRoute is selected_route_type
        )

    def compose_selected_provider_route_authority(
        *,
        store: JournalStore,
        environment: str,
        account_id: str,
        route: SelectedProviderRoute,
        capability_registry: DurableCapabilityRegistry,
        qualification_registry: DurableProviderQualificationRegistry,
        authority_check: AuthorityCheck,
    ) -> AuthorityCheck:
        if not module_authority_current():
            raise ProviderRouteDispatchError(
                "provider route executable authority changed before composition"
            )
        bound_route_function(
            store=store,
            environment=environment,
            account_id=account_id,
            route=route,
            capability_registry=capability_registry,
            qualification_registry=qualification_registry,
        )
        if not callable(authority_check):
            raise TypeError("authority_check must be callable")

        candidate = route.candidate
        expected_capability_id = route.capability_snapshot_id
        expected_qualification_id = route.qualification_id
        current_scope = current_scope_function(route)
        if not module_authority_current():
            raise ProviderRouteDispatchError(
                "provider route executable authority changed during composition"
            )

        def has_class_owned_instance_shadow(value: object) -> bool:
            try:
                state = object.__getattribute__(value, "__dict__")
            except AttributeError:
                return False
            if type(state) is not dict:
                return True
            class_owned_names: set[str] = set()
            for base in type(value).__mro__:
                class_owned_names.update(base.__dict__)
            return bool(class_owned_names.intersection(state))

        def executable_authority_current() -> bool:
            return (
                module_authority_current()
                and not has_class_owned_instance_shadow(store)
                and not has_class_owned_instance_shadow(capability_registry)
                and not has_class_owned_instance_shadow(qualification_registry)
            )

        def combined_authority_check(
            intent_hash_value: str,
            at_text: str,
        ) -> tuple[bool, str]:
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
            if not executable_authority_current():
                return False, "provider_route_executable_authority_changed"
            point = point_function(at_text)
            cut = journal_cut_function(store)
            try:
                capability = capability_reader(
                    capability_registry,
                    provider_id=candidate.provider_id,
                    account_id=candidate.account_id,
                    entity_id=candidate.entity_id,
                    environment=environment,
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
                current = qualification_reader(
                    qualification_registry,
                    scope=current_scope,
                    at=point,
                    expected_qualification_id=expected_qualification_id,
                    journal_sequence_cut=cut,
                )
            except (
                ProviderQualificationError,
                ProviderSelectionError,
                ValueError,
                TypeError,
            ):
                return False, "provider_qualification_not_exact_current"
            if (
                current.qualification_id != expected_qualification_id
                or current.journal_sequence_cut != cut
            ):
                return False, "provider_qualification_not_exact_current"
            if not executable_authority_current():
                return False, "provider_route_executable_authority_changed"
            if journal_cut_function(store) != cut:
                return False, "provider_route_authority_changed_during_barrier"
            return True, reason.strip()

        return combined_authority_check

    return compose_selected_provider_route_authority


compose_selected_provider_route_authority = _install_provider_route_authority_composer()
del _install_provider_route_authority_composer


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
    combined_authority_check = compose_selected_provider_route_authority(
        store=dispatcher.store,
        environment=dispatcher.environment,
        account_id=dispatcher.account_id,
        route=route,
        capability_registry=capability_registry,
        qualification_registry=qualification_registry,
        authority_check=authority_check,
    )

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
        submission_scope=bind_selected_provider_route_submission_scope(
            route,
            submission_scope,
        ),
    )
