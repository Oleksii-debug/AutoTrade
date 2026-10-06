"""Sealed financial authority for one exact provider request.

This module closes the product-level gap between durable financial admission and
an irreversible provider send without creating another dispatcher.  The
existing AuthorityService remains the dynamic financial policy/risk authority;
the existing HostBoundFinancialDispatcher remains the durable send-state owner.
This module only mints an issuer-bound capability for one immutable
FinancialRequestBindingMaterial and turns it back into the legacy two-argument
dispatch guard *inside* trusted composition.

Provider qualification remains an upstream authority.  A qualification digest
is retained in FinancialRequestBindingMaterial but this module does not upgrade
or manufacture qualification evidence.  When the issuer is bound to one sealed
SelectedProviderRoute, the exact current provider C/Q authority is composed into
the same final financial guard immediately before the existing dispatcher send.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .authority import AuthorityService
from .capabilities import CapabilityRegistry
from .dispatch import DispatchOutcome, submission_attempt_aggregate_id
from .durable_capabilities import DurableCapabilityRegistry
from .durable_provider_qualification import DurableProviderQualificationRegistry
from .financial_request_binding import FinancialRequestBindingMaterial
from .persistence import JournalStore, payload_digest
from .production_bybit import ProductionBybitOrderSender
from .production_financial_host import FinancialProductionHostRuntime
from .provider_route_dispatch import (
    bind_selected_provider_route_submission_scope,
    compose_selected_provider_route_authority,
)
from .provider_route_financial_binding import (
    require_financial_binding_matches_selected_route,
)
from .provider_selection import SelectedProviderRoute


class FinancialSendAuthorityError(PermissionError):
    """Raised when an exact financial send capability is absent or retargeted."""


_ISSUER_FACTORY_TOKEN = object()
_CAPABILITY_FACTORY_TOKEN = object()
_BOUND_BYBIT_FACTORY_TOKEN = object()


def _exact_json_value(value: object, *, name: str) -> object:
    """Detach one JSON-domain value without caller-polymorphic dispatch."""

    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is list:
        return [
            _exact_json_value(item, name=f"{name}[{index}]")
            for index, item in enumerate(list.copy(value))
        ]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise TypeError(f"{name} keys must be exact strings")
        detached = dict.copy(value)
        return {
            key: _exact_json_value(item, name=f"{name}.{key}")
            for key, item in detached.items()
        }
    raise TypeError(f"{name} must contain exact JSON-domain values")


def _detached_mapping_snapshot(value: object, *, name: str) -> dict[str, Any]:
    """Snapshot an exact built-in JSON object once before authority callbacks."""

    if type(value) is not dict:
        raise TypeError(f"{name} must be an exact dict")
    detached = _exact_json_value(value, name=name)
    if type(detached) is not dict:
        raise TypeError(f"{name} must be an exact dict")
    return detached


def _mapping_digest(value: object, *, name: str) -> str:
    # payload_digest uses the same canonical-json contract as GuardedDispatcher,
    # but only after the caller-owned input is detached into exact built-ins.
    return payload_digest(_detached_mapping_snapshot(value, name=name))


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise FinancialSendAuthorityError(f"{name} must be exact non-empty text")
    return value


def _require_durable_prepared_financial_request(
    *,
    journal: JournalStore,
    load_events: Callable[..., Any],
    aggregate_id_function: Callable[..., str],
    binding: FinancialRequestBindingMaterial,
    attempt_id: str,
    intent_id: str,
    intent_hash: str,
    checked_intent_hash: str,
    submission_scope: dict[str, Any],
) -> None:
    """Bind the sealed financial capability to the dispatcher's durable Prepared event."""

    if type(journal) is not JournalStore:
        raise FinancialSendAuthorityError("durable prepared journal authority is not exact")
    if type(binding) is not FinancialRequestBindingMaterial:
        raise FinancialSendAuthorityError("durable prepared financial binding is not exact")
    canonical_attempt_id = _exact_text(attempt_id, name="attempt_id")
    canonical_intent_id = _exact_text(intent_id, name="intent_id")
    canonical_intent_hash = _exact_text(intent_hash, name="intent_hash")
    if (
        type(checked_intent_hash) is not str
        or checked_intent_hash != canonical_intent_hash
    ):
        raise FinancialSendAuthorityError(
            "dispatcher authority intent differs from sealed financial authority"
        )
    expected_scope = _detached_mapping_snapshot(
        submission_scope,
        name="selected_submission_scope",
    )
    if payload_digest(expected_scope) != binding.submission_scope_digest:
        raise FinancialSendAuthorityError(
            "selected provider route scope differs from sealed financial binding"
        )

    aggregate_id = aggregate_id_function(
        environment=binding.runtime_environment,
        account_id=binding.account_id,
        attempt_id=canonical_attempt_id,
    )
    events = load_events(journal, "submission_attempt", aggregate_id)
    if type(events) is not list or len(events) != 1:
        raise FinancialSendAuthorityError(
            "financial send requires one durable SubmissionPrepared event"
        )
    prepared = events[0]
    if type(prepared) is not dict:
        raise FinancialSendAuthorityError("durable SubmissionPrepared event is malformed")
    if (
        prepared.get("event_type") != "SubmissionPrepared"
        or prepared.get("aggregate_id") != aggregate_id
        or prepared.get("aggregate_version") != 1
    ):
        raise FinancialSendAuthorityError(
            "durable submission state is not the exact Prepared authority"
        )
    payload = prepared.get("payload")
    if type(payload) is not dict:
        raise FinancialSendAuthorityError("durable SubmissionPrepared payload is malformed")

    expected_projection = (
        canonical_attempt_id,
        canonical_intent_id,
        canonical_intent_hash,
        binding.provider_id,
        binding.request_sha256,
        binding.client_order_id,
        binding.runtime_environment,
        binding.account_id,
        binding.submission_scope_digest,
    )
    durable_projection = (
        payload.get("attempt_id"),
        payload.get("intent_id"),
        payload.get("intent_hash"),
        payload.get("provider"),
        payload.get("request_hash"),
        payload.get("client_order_id"),
        payload.get("environment"),
        payload.get("account_id"),
        payload.get("submission_scope_hash"),
    )
    if durable_projection != expected_projection:
        raise FinancialSendAuthorityError(
            "durable SubmissionPrepared scope differs from sealed financial authority"
        )
    durable_scope = payload.get("submission_scope")
    if type(durable_scope) is not dict:
        raise FinancialSendAuthorityError(
            "durable SubmissionPrepared submission scope is malformed"
        )
    if durable_scope != expected_scope:
        raise FinancialSendAuthorityError(
            "durable SubmissionPrepared provider scope differs from selected route"
        )
    if payload_digest(durable_scope) != binding.submission_scope_digest:
        raise FinancialSendAuthorityError(
            "durable SubmissionPrepared scope digest differs from financial binding"
        )


def _risk_payload(
    journal: JournalStore,
    binding: FinancialRequestBindingMaterial,
    *,
    load_events: Callable[..., Any],
) -> Mapping[str, Any]:
    events = load_events(journal, "risk_decision", binding.risk_decision_id)
    if type(events) is not list or len(events) != 1:
        raise FinancialSendAuthorityError(
            "financial binding risk decision is not one durable canonical event"
        )
    payload = events[0].get("payload")
    if type(payload) is not dict:
        raise FinancialSendAuthorityError("durable risk decision payload is malformed")
    return payload


def _require_binding_matches_durable_admission(
    *,
    service: AuthorityService,
    journal: JournalStore,
    historical_admission: Callable[..., Any],
    load_events: Callable[..., Any],
    risk_payload_function: Callable[..., Mapping[str, Any]],
    admission_id: str,
    intent_hash: str,
    action: str,
    binding: FinancialRequestBindingMaterial,
) -> Mapping[str, Any]:
    admission = historical_admission(service, admission_id)
    if type(admission) is not dict or admission.get("outcome") != "ADMITTED":
        raise FinancialSendAuthorityError("financial send requires an admitted durable record")

    instrument = admission.get("instrument")
    if type(instrument) is not dict:
        raise FinancialSendAuthorityError("durable admission instrument is malformed")

    expected = (
        _exact_text(intent_hash, name="intent_hash"),
        binding.account_id,
        binding.runtime_environment,
        binding.instrument_id,
        binding.instrument_version,
        _exact_text(action, name="action").upper(),
        binding.risk_decision_id,
        binding.reservation_id,
        binding.capability_snapshot_id,
    )
    actual = (
        admission.get("intent_hash"),
        admission.get("account_id"),
        admission.get("environment"),
        instrument.get("instrument_id"),
        instrument.get("version"),
        admission.get("action"),
        admission.get("risk_decision_id"),
        admission.get("reservation_id"),
        admission.get("capability_snapshot_id"),
    )
    if actual != expected:
        raise FinancialSendAuthorityError(
            "financial request binding differs from durable admission scope"
        )

    risk_payload = risk_payload_function(journal, binding, load_events=load_events)
    if risk_payload.get("journal_sequence_cut") != binding.admitted_journal_sequence_cut:
        raise FinancialSendAuthorityError(
            "financial request binding journal cut differs from durable risk decision"
        )
    snapshot = risk_payload.get("authoritative_risk_snapshot")
    if type(snapshot) is not dict:
        raise FinancialSendAuthorityError(
            "durable risk decision lacks authoritative risk snapshot"
        )
    if snapshot.get("snapshot_id") != binding.risk_snapshot_id:
        raise FinancialSendAuthorityError(
            "financial request binding risk snapshot differs from durable decision"
        )

    durable_intent = risk_payload.get("risk_intent")
    if type(durable_intent) is not dict:
        raise FinancialSendAuthorityError("durable risk decision lacks canonical risk intent")
    durable_side = durable_intent.get("side")
    durable_quantity = durable_intent.get("quantity")
    durable_price = durable_intent.get("price")
    if durable_side != binding.side:
        raise FinancialSendAuthorityError("financial binding side differs from admitted risk")
    if durable_quantity != binding.quantity:
        raise FinancialSendAuthorityError("financial binding quantity differs from admitted risk")
    if durable_price != binding.price:
        raise FinancialSendAuthorityError("financial binding price differs from admitted risk")

    return admission


def require_exact_bybit_financial_request(
    binding: FinancialRequestBindingMaterial,
    request: Mapping[str, Any],
    submission_scope: Mapping[str, Any] | None,
    *,
    provider_environment: str,
) -> None:
    """Prove one Bybit dispatcher request is the exact financial binding.

    This is intentionally pure and public so provider-composition tests can
    exercise the terminal equality rule without minting live authority.
    """

    if type(binding) is not FinancialRequestBindingMaterial:
        raise TypeError("binding must be exact FinancialRequestBindingMaterial")
    if binding.provider_id != "BYBIT":
        raise FinancialSendAuthorityError("financial binding belongs to another provider")
    provider_env = _exact_text(
        provider_environment,
        name="provider_environment",
    ).upper()
    if provider_env != binding.provider_environment:
        raise FinancialSendAuthorityError(
            "Bybit provider environment differs from financial binding"
        )

    canonical_request = _detached_mapping_snapshot(request, name="request")
    if payload_digest(canonical_request) != binding.request_sha256:
        raise FinancialSendAuthorityError(
            "dispatcher request digest differs from financial binding"
        )

    exact_projection = (
        canonical_request.get("endpoint"),
        canonical_request.get("account_id"),
        canonical_request.get("environment"),
        canonical_request.get("provider_environment"),
        canonical_request.get("capability_snapshot_id"),
        canonical_request.get("body_sha256"),
    )
    expected_projection = (
        binding.endpoint,
        binding.account_id,
        binding.runtime_environment,
        binding.provider_environment,
        binding.capability_snapshot_id,
        binding.body_sha256,
    )
    if exact_projection != expected_projection:
        raise FinancialSendAuthorityError(
            "dispatcher provider projection differs from financial binding"
        )

    body = canonical_request.get("body")
    if type(body) is not dict:
        raise FinancialSendAuthorityError("Bybit dispatcher request body is missing")
    if payload_digest(body) != binding.body_sha256:
        raise FinancialSendAuthorityError("Bybit body digest differs from financial binding")

    expected_side = "Buy" if binding.side == "BUY" else "Sell"
    expected_order_type = "Market" if binding.order_type == "MARKET" else "Limit"
    if body.get("orderLinkId") != binding.client_order_id:
        raise FinancialSendAuthorityError("Bybit client order id differs from financial binding")
    if body.get("side") != expected_side:
        raise FinancialSendAuthorityError("Bybit side differs from financial binding")
    if body.get("qty") != binding.quantity:
        raise FinancialSendAuthorityError("Bybit quantity differs from financial binding")
    if body.get("orderType") != expected_order_type:
        raise FinancialSendAuthorityError("Bybit order type differs from financial binding")
    if body.get("timeInForce") != binding.time_in_force:
        raise FinancialSendAuthorityError("Bybit TIF differs from financial binding")
    if binding.order_type == "LIMIT":
        if body.get("price") != binding.price:
            raise FinancialSendAuthorityError("Bybit limit price differs from financial binding")
    elif "price" in body:
        raise FinancialSendAuthorityError("Bybit market request carries an unbound price")
    body_reduce_only = body.get("reduceOnly", False)
    if type(body_reduce_only) is not bool or body_reduce_only != binding.reduce_only:
        raise FinancialSendAuthorityError("Bybit reduce-only differs from financial binding")

    scope = {} if submission_scope is None else _detached_mapping_snapshot(
        submission_scope,
        name="submission_scope",
    )
    if payload_digest(scope) != binding.submission_scope_digest:
        raise FinancialSendAuthorityError(
            "dispatcher submission scope differs from financial binding"
        )


class FinancialSendAuthority:
    """One issuer-bound, admission-bound, exact-request send capability."""

    __slots__ = (
        "__issuer_identity",
        "__binding",
        "__admission_id",
        "__intent_id",
        "__intent_hash",
        "__action",
    )

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("FinancialSendAuthority is sealed")

    def __init__(
        self,
        *,
        issuer_identity: object,
        binding: FinancialRequestBindingMaterial,
        admission_id: str,
        intent_id: str,
        intent_hash: str,
        action: str,
        _factory_token: object = None,
    ) -> None:
        if _factory_token is not _CAPABILITY_FACTORY_TOKEN:
            raise FinancialSendAuthorityError(
                "FinancialSendAuthority must be minted by canonical issuer"
            )
        if type(binding) is not FinancialRequestBindingMaterial:
            raise TypeError("binding must be exact FinancialRequestBindingMaterial")
        self.__issuer_identity = issuer_identity
        self.__binding = binding
        self.__admission_id = _exact_text(admission_id, name="admission_id")
        self.__intent_id = _exact_text(intent_id, name="intent_id")
        self.__intent_hash = _exact_text(intent_hash, name="intent_hash")
        self.__action = _exact_text(action, name="action").upper()

    @property
    def binding(self) -> FinancialRequestBindingMaterial:
        return self.__binding

    @property
    def binding_id(self) -> str:
        return self.__binding.binding_id

    @property
    def admission_id(self) -> str:
        return self.__admission_id

    @property
    def intent_id(self) -> str:
        return self.__intent_id

    @property
    def intent_hash(self) -> str:
        return self.__intent_hash

    @property
    def action(self) -> str:
        return self.__action

    def _require_issuer(self, issuer_identity: object) -> None:
        if issuer_identity is not self.__issuer_identity:
            raise FinancialSendAuthorityError(
                "financial send capability belongs to another authority issuer"
            )


def _capability_property_authority(name: str) -> tuple[str, property, Callable[..., Any], object]:
    """Capture one exact capability property without descriptor dispatch."""

    descriptor = FinancialSendAuthority.__dict__.get(name)
    if type(descriptor) is not property or descriptor.fget is None:
        raise FinancialSendAuthorityError(
            f"FinancialSendAuthority {name} property authority is unavailable"
        )
    getter = descriptor.fget
    code = getattr(getter, "__code__", None)
    if code is None:
        raise FinancialSendAuthorityError(
            f"FinancialSendAuthority {name} getter authority is unavailable"
        )
    return name, descriptor, getter, code


class FinancialSendAuthorityIssuer:
    """Product-owned bridge from AuthorityService to exact send capabilities."""

    __slots__ = (
        "__service",
        "__runtime",
        "__journal",
        "__store_identity",
        "__dispatcher",
        "__issuer_identity",
        "__dispatch_guard_function",
        "__dispatch_guard_code",
        "__historical_function",
        "__historical_code",
        "__journal_load_events_function",
        "__journal_load_events_code",
        "__risk_payload_function",
        "__risk_payload_code",
        "__durable_binding_function",
        "__durable_binding_code",
        "__submission_attempt_id_function",
        "__submission_attempt_id_code",
        "__prepared_request_function",
        "__prepared_request_code",
        "__capability_issuer_function",
        "__capability_issuer_code",
        "__capability_property_authorities",
        "__selected_route",
        "__capability_registry",
        "__qualification_registry",
        "__route_authority_function",
        "__route_authority_code",
        "__route_scope_function",
        "__route_scope_code",
        "__financial_route_binding_function",
        "__financial_route_binding_code",
    )

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("FinancialSendAuthorityIssuer is sealed")

    def __init__(
        self,
        service: AuthorityService,
        runtime: FinancialProductionHostRuntime,
        *,
        selected_route: SelectedProviderRoute | None = None,
        capability_registry: DurableCapabilityRegistry | None = None,
        qualification_registry: DurableProviderQualificationRegistry | None = None,
        _factory_token: object = None,
    ) -> None:
        if _factory_token is not _ISSUER_FACTORY_TOKEN:
            raise FinancialSendAuthorityError(
                "FinancialSendAuthorityIssuer must come from canonical composition"
            )
        if type(service) is not AuthorityService:
            raise TypeError("service must be exact AuthorityService")
        if type(runtime) is not FinancialProductionHostRuntime:
            raise TypeError("runtime must be exact FinancialProductionHostRuntime")
        journal = runtime.journal
        if type(journal) is not JournalStore or service.store is not journal:
            raise FinancialSendAuthorityError(
                "AuthorityService and production host must share one exact JournalStore"
            )
        if runtime.store_identity != journal.store_identity:
            raise FinancialSendAuthorityError("production journal generation is inconsistent")
        dispatcher = runtime.financial_dispatcher

        route_parts = (selected_route, capability_registry, qualification_registry)
        route_bound = any(part is not None for part in route_parts)
        if route_bound and not all(part is not None for part in route_parts):
            raise FinancialSendAuthorityError(
                "provider route authority requires selected_route plus durable C/Q registries"
            )
        if route_bound:
            if type(selected_route) is not SelectedProviderRoute:
                raise TypeError("selected_route must be exact SelectedProviderRoute")
            if type(capability_registry) is not DurableCapabilityRegistry:
                raise TypeError(
                    "capability_registry must be exact DurableCapabilityRegistry"
                )
            if type(qualification_registry) is not DurableProviderQualificationRegistry:
                raise TypeError(
                    "qualification_registry must be exact DurableProviderQualificationRegistry"
                )
            if (
                capability_registry.store is not journal
                or qualification_registry.store is not journal
            ):
                raise FinancialSendAuthorityError(
                    "financial and provider-route authorities must share one exact JournalStore"
                )
            if selected_route.candidate.account_id != dispatcher.account_id:
                raise FinancialSendAuthorityError(
                    "selected provider route account differs from production financial host"
                )
            provider_scope = selected_route.qualification.scope.provider_scope
            if provider_scope.runtime_environment != dispatcher.environment:
                raise FinancialSendAuthorityError(
                    "selected provider route environment differs from production financial host"
                )

        dispatch_guard_function = AuthorityService.dispatch_guard
        historical_function = AuthorityService.historical_admission
        journal_load_events_function = JournalStore.load_events
        risk_payload_function = _risk_payload
        durable_binding_function = _require_binding_matches_durable_admission
        submission_attempt_id_function = submission_attempt_aggregate_id
        prepared_request_function = _require_durable_prepared_financial_request
        route_authority_function = compose_selected_provider_route_authority
        route_scope_function = bind_selected_provider_route_submission_scope
        financial_route_binding_function = require_financial_binding_matches_selected_route
        journal_load_events_code = getattr(journal_load_events_function, "__code__", None)
        risk_payload_code = getattr(risk_payload_function, "__code__", None)
        durable_binding_code = getattr(durable_binding_function, "__code__", None)
        submission_attempt_id_code = getattr(submission_attempt_id_function, "__code__", None)
        prepared_request_code = getattr(prepared_request_function, "__code__", None)
        if journal_load_events_code is None:
            raise FinancialSendAuthorityError(
                "JournalStore load-events executable authority is unavailable"
            )
        if risk_payload_code is None:
            raise FinancialSendAuthorityError(
                "durable risk-payload executable authority is unavailable"
            )
        if durable_binding_code is None:
            raise FinancialSendAuthorityError(
                "durable admission-binding executable authority is unavailable"
            )
        if submission_attempt_id_code is None:
            raise FinancialSendAuthorityError(
                "submission attempt identity executable authority is unavailable"
            )
        if prepared_request_code is None:
            raise FinancialSendAuthorityError(
                "durable Prepared financial authority executable is unavailable"
            )
        capability_issuer_function = FinancialSendAuthority.__dict__.get("_require_issuer")
        capability_issuer_code = getattr(capability_issuer_function, "__code__", None)
        if not callable(capability_issuer_function) or capability_issuer_code is None:
            raise FinancialSendAuthorityError(
                "FinancialSendAuthority issuer executable authority is unavailable"
            )
        capability_property_authorities = tuple(
            _capability_property_authority(name)
            for name in ("binding", "admission_id", "intent_id", "intent_hash", "action")
        )
        self.__service = service
        self.__runtime = runtime
        self.__journal = journal
        self.__store_identity = runtime.store_identity
        self.__dispatcher = dispatcher
        self.__issuer_identity = object()
        self.__dispatch_guard_function = dispatch_guard_function
        self.__dispatch_guard_code = dispatch_guard_function.__code__
        self.__historical_function = historical_function
        self.__historical_code = historical_function.__code__
        self.__journal_load_events_function = journal_load_events_function
        self.__journal_load_events_code = journal_load_events_code
        self.__risk_payload_function = risk_payload_function
        self.__risk_payload_code = risk_payload_code
        self.__durable_binding_function = durable_binding_function
        self.__durable_binding_code = durable_binding_code
        self.__submission_attempt_id_function = submission_attempt_id_function
        self.__submission_attempt_id_code = submission_attempt_id_code
        self.__prepared_request_function = prepared_request_function
        self.__prepared_request_code = prepared_request_code
        self.__capability_issuer_function = capability_issuer_function
        self.__capability_issuer_code = capability_issuer_code
        self.__capability_property_authorities = capability_property_authorities
        self.__selected_route = selected_route
        self.__capability_registry = capability_registry
        self.__qualification_registry = qualification_registry
        self.__route_authority_function = route_authority_function
        self.__route_authority_code = route_authority_function.__code__
        self.__route_scope_function = route_scope_function
        self.__route_scope_code = route_scope_function.__code__
        self.__financial_route_binding_function = financial_route_binding_function
        self.__financial_route_binding_code = financial_route_binding_function.__code__

    def _require_capability_executable_authority(self) -> None:
        issuer_function = self.__capability_issuer_function
        if FinancialSendAuthority.__dict__.get("_require_issuer") is not issuer_function:
            raise FinancialSendAuthorityError(
                "FinancialSendAuthority issuer executable authority changed"
            )
        if issuer_function.__code__ is not self.__capability_issuer_code:
            raise FinancialSendAuthorityError(
                "FinancialSendAuthority issuer executable authority code changed"
            )
        for name, descriptor, getter, code in self.__capability_property_authorities:
            current_descriptor = FinancialSendAuthority.__dict__.get(name)
            if current_descriptor is not descriptor:
                raise FinancialSendAuthorityError(
                    f"FinancialSendAuthority {name} property authority changed"
                )
            if type(current_descriptor) is not property or current_descriptor.fget is not getter:
                raise FinancialSendAuthorityError(
                    f"FinancialSendAuthority {name} getter authority changed"
                )
            if getter.__code__ is not code:
                raise FinancialSendAuthorityError(
                    f"FinancialSendAuthority {name} getter authority code changed"
                )

    def _require_current(self) -> None:
        if type(self.__service) is not AuthorityService:
            raise FinancialSendAuthorityError("financial authority service type changed")
        if self.__service.store is not self.__journal:
            raise FinancialSendAuthorityError("financial authority journal changed")
        if self.__runtime.journal is not self.__journal:
            raise FinancialSendAuthorityError("production host journal changed")
        if (
            self.__runtime.store_identity != self.__store_identity
            or self.__journal.store_identity != self.__store_identity
        ):
            raise FinancialSendAuthorityError("production journal generation changed")
        if self.__runtime.financial_dispatcher is not self.__dispatcher:
            raise FinancialSendAuthorityError("production financial dispatcher changed")
        if AuthorityService.dispatch_guard is not self.__dispatch_guard_function:
            raise FinancialSendAuthorityError("AuthorityService dispatch guard changed")
        if self.__dispatch_guard_function.__code__ is not self.__dispatch_guard_code:
            raise FinancialSendAuthorityError("AuthorityService dispatch guard code changed")
        if AuthorityService.historical_admission is not self.__historical_function:
            raise FinancialSendAuthorityError("AuthorityService historical authority changed")
        if self.__historical_function.__code__ is not self.__historical_code:
            raise FinancialSendAuthorityError("AuthorityService historical code changed")
        if JournalStore.load_events is not self.__journal_load_events_function:
            raise FinancialSendAuthorityError("JournalStore load-events authority changed")
        if (
            self.__journal_load_events_function.__code__
            is not self.__journal_load_events_code
        ):
            raise FinancialSendAuthorityError("JournalStore load-events code changed")
        if _risk_payload is not self.__risk_payload_function:
            raise FinancialSendAuthorityError("durable risk-payload authority changed")
        if self.__risk_payload_function.__code__ is not self.__risk_payload_code:
            raise FinancialSendAuthorityError("durable risk-payload authority code changed")
        if _require_binding_matches_durable_admission is not self.__durable_binding_function:
            raise FinancialSendAuthorityError("durable admission-binding authority changed")
        if self.__durable_binding_function.__code__ is not self.__durable_binding_code:
            raise FinancialSendAuthorityError(
                "durable admission-binding authority code changed"
            )
        if submission_attempt_aggregate_id is not self.__submission_attempt_id_function:
            raise FinancialSendAuthorityError("submission attempt identity authority changed")
        if (
            self.__submission_attempt_id_function.__code__
            is not self.__submission_attempt_id_code
        ):
            raise FinancialSendAuthorityError("submission attempt identity code changed")
        if _require_durable_prepared_financial_request is not self.__prepared_request_function:
            raise FinancialSendAuthorityError("durable Prepared financial authority changed")
        if self.__prepared_request_function.__code__ is not self.__prepared_request_code:
            raise FinancialSendAuthorityError("durable Prepared financial authority code changed")
        if compose_selected_provider_route_authority is not self.__route_authority_function:
            raise FinancialSendAuthorityError("provider route authority composer changed")
        if self.__route_authority_function.__code__ is not self.__route_authority_code:
            raise FinancialSendAuthorityError("provider route authority composer code changed")
        if bind_selected_provider_route_submission_scope is not self.__route_scope_function:
            raise FinancialSendAuthorityError("provider route submission scope authority changed")
        if self.__route_scope_function.__code__ is not self.__route_scope_code:
            raise FinancialSendAuthorityError(
                "provider route submission scope authority code changed"
            )
        if (
            require_financial_binding_matches_selected_route
            is not self.__financial_route_binding_function
        ):
            raise FinancialSendAuthorityError("financial/provider route binding authority changed")
        if (
            self.__financial_route_binding_function.__code__
            is not self.__financial_route_binding_code
        ):
            raise FinancialSendAuthorityError(
                "financial/provider route binding authority code changed"
            )
        selected_route = self.__selected_route
        if selected_route is None:
            if (
                self.__capability_registry is not None
                or self.__qualification_registry is not None
            ):
                raise FinancialSendAuthorityError("provider route authority binding changed")
        else:
            if type(selected_route) is not SelectedProviderRoute:
                raise FinancialSendAuthorityError("selected provider route authority changed")
            if type(self.__capability_registry) is not DurableCapabilityRegistry:
                raise FinancialSendAuthorityError("provider capability authority changed")
            if type(self.__qualification_registry) is not DurableProviderQualificationRegistry:
                raise FinancialSendAuthorityError("provider qualification authority changed")
            if (
                self.__capability_registry.store is not self.__journal
                or self.__qualification_registry.store is not self.__journal
            ):
                raise FinancialSendAuthorityError(
                    "provider route authority journal changed"
                )
            if selected_route.candidate.account_id != self.__dispatcher.account_id:
                raise FinancialSendAuthorityError(
                    "selected provider route account changed"
                )
            if (
                selected_route.qualification.scope.provider_scope.runtime_environment
                != self.__dispatcher.environment
            ):
                raise FinancialSendAuthorityError(
                    "selected provider route environment changed"
                )
        self._require_capability_executable_authority()

    @property
    def runtime(self) -> FinancialProductionHostRuntime:
        self._require_current()
        return self.__runtime

    @property
    def provider_route_bound(self) -> bool:
        self._require_current()
        return self.__selected_route is not None

    def issue(
        self,
        *,
        admission_id: str,
        intent_id: str,
        intent_hash: str,
        action: str,
        binding: FinancialRequestBindingMaterial,
    ) -> FinancialSendAuthority:
        self._require_current()
        if type(binding) is not FinancialRequestBindingMaterial:
            raise TypeError("binding must be exact FinancialRequestBindingMaterial")
        durable_binding_function = self.__durable_binding_function
        admission = durable_binding_function(
            service=self.__service,
            journal=self.__journal,
            historical_admission=self.__historical_function,
            load_events=self.__journal_load_events_function,
            risk_payload_function=self.__risk_payload_function,
            admission_id=_exact_text(admission_id, name="admission_id"),
            intent_hash=_exact_text(intent_hash, name="intent_hash"),
            action=_exact_text(action, name="action"),
            binding=binding,
        )
        canonical_intent_id = _exact_text(intent_id, name="intent_id")
        if admission.get("intent_id") != canonical_intent_id:
            raise FinancialSendAuthorityError(
                "financial send intent id differs from durable admission"
            )
        return FinancialSendAuthority(
            issuer_identity=self.__issuer_identity,
            binding=binding,
            admission_id=admission_id,
            intent_id=canonical_intent_id,
            intent_hash=intent_hash,
            action=action,
            _factory_token=_CAPABILITY_FACTORY_TOKEN,
        )

    def _require_capability(self, authority: FinancialSendAuthority) -> None:
        self._require_current()
        if type(authority) is not FinancialSendAuthority:
            raise FinancialSendAuthorityError(
                "send requires exact FinancialSendAuthority capability"
            )
        self.__capability_issuer_function(authority, self.__issuer_identity)

    def _capability_material(
        self,
        authority: FinancialSendAuthority,
    ) -> tuple[FinancialRequestBindingMaterial, str, str, str, str]:
        self._require_capability(authority)
        values = {
            name: getter(authority)
            for name, _descriptor, getter, _code in self.__capability_property_authorities
        }
        binding = values["binding"]
        if type(binding) is not FinancialRequestBindingMaterial:
            raise FinancialSendAuthorityError(
                "financial capability binding authority is malformed"
            )
        return (
            binding,
            values["admission_id"],
            values["intent_id"],
            values["intent_hash"],
            values["action"],
        )

    def _dispatch_material_for(
        self,
        authority: FinancialSendAuthority,
        *,
        attempt_id: str | None = None,
    ) -> tuple[
        Callable[[str, str], tuple[bool, str]],
        FinancialRequestBindingMaterial,
        str,
        str,
    ]:
        """Re-derive one financial + exact-current provider C/Q send guard."""

        binding, admission_id, intent_id, intent_hash, action = self._capability_material(
            authority
        )
        durable_binding_function = self.__durable_binding_function
        admission = durable_binding_function(
            service=self.__service,
            journal=self.__journal,
            historical_admission=self.__historical_function,
            load_events=self.__journal_load_events_function,
            risk_payload_function=self.__risk_payload_function,
            admission_id=admission_id,
            intent_hash=intent_hash,
            action=action,
            binding=binding,
        )
        if admission.get("intent_id") != intent_id:
            raise FinancialSendAuthorityError(
                "financial send intent id differs from durable admission"
            )
        guard = self.__dispatch_guard_function(
            self.__service,
            admission_id,
            account_id=binding.account_id,
            environment=binding.runtime_environment,
            instrument_id=binding.instrument_id,
            instrument_version=binding.instrument_version,
            action=action,
            capability_snapshot_id=binding.capability_snapshot_id,
        )

        selected_route = self.__selected_route
        if selected_route is None:
            raise FinancialSendAuthorityError(
                "financial send requires selected provider route authority"
            )
        financial_route_binding = self.__financial_route_binding_function
        financial_route_binding(binding, selected_route)
        route_scope = self.__route_scope_function(
            selected_route,
            {
                "provider_id": binding.provider_id,
                "account_id": binding.account_id,
                "environment": binding.runtime_environment,
                "provider_environment": binding.provider_environment,
                "capability_snapshot_id": binding.capability_snapshot_id,
                "endpoint": binding.endpoint,
                "prepared_request_sha256": binding.request_sha256,
                "capability_snapshot_ids": [binding.capability_snapshot_id],
                "instrument_versions": [str(binding.instrument_version)],
            },
        )
        if payload_digest(route_scope) != binding.submission_scope_digest:
            raise FinancialSendAuthorityError(
                "financial binding submission scope does not retain selected provider route authority"
            )
        route_authority = self.__route_authority_function(
            store=self.__journal,
            environment=binding.runtime_environment,
            account_id=binding.account_id,
            route=selected_route,
            capability_registry=self.__capability_registry,
            qualification_registry=self.__qualification_registry,
            authority_check=guard,
        )
        if attempt_id is None:
            return route_authority, binding, intent_id, intent_hash

        canonical_attempt_id = _exact_text(attempt_id, name="attempt_id")
        route_scope_snapshot = _detached_mapping_snapshot(
            route_scope,
            name="selected_route_scope",
        )
        journal = self.__journal
        load_events = self.__journal_load_events_function
        aggregate_id_function = self.__submission_attempt_id_function
        prepared_request_function = self.__prepared_request_function

        def durable_prepared_route_authority(
            checked_intent_hash: str,
            at_text: str,
        ) -> tuple[bool, str]:
            self._require_current()
            prepared_request_function(
                journal=journal,
                load_events=load_events,
                aggregate_id_function=aggregate_id_function,
                binding=binding,
                attempt_id=canonical_attempt_id,
                intent_id=intent_id,
                intent_hash=intent_hash,
                checked_intent_hash=checked_intent_hash,
                submission_scope=route_scope_snapshot,
            )
            return route_authority(checked_intent_hash, at_text)

        return durable_prepared_route_authority, binding, intent_id, intent_hash

    def _dispatch_guard_for(
        self,
        authority: FinancialSendAuthority,
    ) -> Callable[[str, str], tuple[bool, str]]:
        """Compatibility helper retaining the previous internal guard surface."""

        guard, _binding, _intent_id, _intent_hash = self._dispatch_material_for(authority)
        return guard


def build_financial_send_authority_issuer(
    service: AuthorityService,
    runtime: FinancialProductionHostRuntime,
    *,
    selected_route: SelectedProviderRoute | None = None,
    capability_registry: DurableCapabilityRegistry | None = None,
    qualification_registry: DurableProviderQualificationRegistry | None = None,
) -> FinancialSendAuthorityIssuer:
    """Bind one financial host generation and, when supplied, one sealed C/Q route."""

    return FinancialSendAuthorityIssuer(
        service,
        runtime,
        selected_route=selected_route,
        capability_registry=capability_registry,
        qualification_registry=qualification_registry,
        _factory_token=_ISSUER_FACTORY_TOKEN,
    )


class FinanciallyBoundBybitOrderSender:
    """Canonical Bybit product sender requiring one sealed financial capability."""

    __slots__ = (
        "__sender",
        "__sender_dispatch",
        "__sender_dispatch_function",
        "__sender_dispatch_code",
        "__issuer",
        "__runtime",
        "__provider_environment",
    )

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("FinanciallyBoundBybitOrderSender is sealed")

    def __init__(
        self,
        sender: ProductionBybitOrderSender,
        issuer: FinancialSendAuthorityIssuer,
        *,
        _factory_token: object = None,
    ) -> None:
        if _factory_token is not _BOUND_BYBIT_FACTORY_TOKEN:
            raise FinancialSendAuthorityError(
                "FinanciallyBoundBybitOrderSender must come from canonical composition"
            )
        if type(sender) is not ProductionBybitOrderSender:
            raise TypeError("sender must be exact ProductionBybitOrderSender")
        if type(issuer) is not FinancialSendAuthorityIssuer:
            raise TypeError("issuer must be exact FinancialSendAuthorityIssuer")
        runtime = issuer.runtime
        sender_runtime = getattr(sender, "_ProductionBybitOrderSender__runtime", None)
        if sender_runtime is not runtime:
            raise FinancialSendAuthorityError(
                "Bybit sender and financial issuer belong to different production hosts"
            )

        selected_route = getattr(
            issuer,
            "_FinancialSendAuthorityIssuer__selected_route",
            None,
        )
        if type(selected_route) is not SelectedProviderRoute:
            raise FinancialSendAuthorityError(
                "Bybit product sender requires selected provider route authority"
            )
        route_capability = selected_route.capability
        route_candidate = selected_route.candidate
        sender_provider_environment = sender.provider_environment
        sender_capability_id = getattr(
            sender,
            "_ProductionBybitOrderSender__capability_snapshot_id",
            None,
        )
        sender_capability_registry = getattr(
            sender,
            "_ProductionBybitOrderSender__capability_registry",
            None,
        )
        if (
            sender_provider_environment != route_candidate.provider_environment
            or sender_capability_id != selected_route.capability_snapshot_id
        ):
            raise FinancialSendAuthorityError(
                "Bybit sender provider scope differs from selected provider route"
            )
        if type(sender_capability_registry) is not CapabilityRegistry:
            raise FinancialSendAuthorityError(
                "Bybit sender capability registry is not canonical"
            )
        registry_state = vars(sender_capability_registry)
        if type(registry_state) is not dict:
            raise FinancialSendAuthorityError(
                "Bybit sender capability registry state is not canonical"
            )
        by_id = registry_state.get("_by_id")
        by_identity = registry_state.get("_by_identity")
        if type(by_id) is not dict or type(by_identity) is not dict:
            raise FinancialSendAuthorityError(
                "Bybit sender capability registry state is not canonical"
            )
        history = by_identity.get(route_capability.identity)
        if (
            by_id.get(route_capability.snapshot_id) is not route_capability
            or type(history) is not list
            or not any(item is route_capability for item in history)
        ):
            raise FinancialSendAuthorityError(
                "Bybit sender does not retain exact selected provider capability"
            )

        sender_dispatch_function = ProductionBybitOrderSender.dispatch
        sender_dispatch = sender.dispatch
        if (
            not callable(sender_dispatch_function)
            or not hasattr(sender_dispatch_function, "__code__")
            or getattr(sender_dispatch, "__self__", None) is not sender
            or getattr(sender_dispatch, "__func__", None) is not sender_dispatch_function
        ):
            raise FinancialSendAuthorityError(
                "Bybit sender dispatch executable authority is unavailable"
            )
        self.__sender = sender
        self.__sender_dispatch = sender_dispatch
        self.__sender_dispatch_function = sender_dispatch_function
        self.__sender_dispatch_code = sender_dispatch_function.__code__
        self.__issuer = issuer
        self.__runtime = runtime
        self.__provider_environment = sender.provider_environment

    @property
    def provider_environment(self) -> str:
        return self.__provider_environment

    def _require_sender_dispatch_authority(self) -> None:
        sender_dispatch_function = self.__sender_dispatch_function
        if ProductionBybitOrderSender.dispatch is not sender_dispatch_function:
            raise FinancialSendAuthorityError(
                "Bybit sender dispatch executable authority changed"
            )
        if sender_dispatch_function.__code__ is not self.__sender_dispatch_code:
            raise FinancialSendAuthorityError(
                "Bybit sender dispatch executable authority code changed"
            )
        sender_dispatch = self.__sender_dispatch
        if (
            getattr(sender_dispatch, "__self__", None) is not self.__sender
            or getattr(sender_dispatch, "__func__", None) is not sender_dispatch_function
        ):
            raise FinancialSendAuthorityError(
                "Bybit sender dispatch binding authority changed"
            )

    def dispatch(
        self,
        *,
        authority: FinancialSendAuthority,
        attempt_id: str,
        intent_id: str,
        intent_hash: str,
        request: Mapping[str, Any],
        now: str,
        client_id_max_length: int = 36,
        client_id_format: str = "TOKEN",
        submission_scope: Mapping[str, Any] | None = None,
    ) -> DispatchOutcome:
        self._require_sender_dispatch_authority()
        request_snapshot = _detached_mapping_snapshot(request, name="request")
        submission_scope_snapshot = (
            {}
            if submission_scope is None
            else _detached_mapping_snapshot(
                submission_scope,
                name="submission_scope",
            )
        )

        issuer = self.__issuer
        (
            authority_check,
            authority_binding,
            authority_intent_id,
            authority_intent_hash,
        ) = issuer._dispatch_material_for(authority, attempt_id=attempt_id)
        if issuer.runtime is not self.__runtime:
            raise FinancialSendAuthorityError("financial issuer production host changed")
        if authority_intent_id != intent_id or authority_intent_hash != intent_hash:
            raise FinancialSendAuthorityError(
                "dispatch intent differs from sealed financial authority"
            )
        require_exact_bybit_financial_request(
            authority_binding,
            request_snapshot,
            submission_scope_snapshot,
            provider_environment=self.__provider_environment,
        )
        self._require_sender_dispatch_authority()
        sender_dispatch = self.__sender_dispatch
        return sender_dispatch(
            attempt_id=attempt_id,
            intent_id=intent_id,
            intent_hash=intent_hash,
            request=request_snapshot,
            now=now,
            authority_check=authority_check,
            client_id_max_length=client_id_max_length,
            client_id_format=client_id_format,
            submission_scope=submission_scope_snapshot,
        )


def bind_financial_bybit_order_sender(
    sender: ProductionBybitOrderSender,
    issuer: FinancialSendAuthorityIssuer,
) -> FinanciallyBoundBybitOrderSender:
    """Make the product-facing Bybit sender reject caller-supplied callbacks."""

    return FinanciallyBoundBybitOrderSender(
        sender,
        issuer,
        _factory_token=_BOUND_BYBIT_FACTORY_TOKEN,
    )
