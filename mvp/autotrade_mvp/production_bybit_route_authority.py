"""Product Bybit composition for one exact financial binding and selected C/Q route.

This adapter creates no dispatcher, recovery state machine, journal, provider
transport, risk service, capability registry, or qualification authority.  It
combines the financial guard issued by #987 with the exact-current C1/Q1 guard
from #1082 and passes that single callback into the existing production Bybit
sender.  The existing GuardedDispatcher therefore remains the one final barrier.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .capabilities import CapabilityRegistry
from .dispatch import DispatchOutcome
from .durable_capabilities import DurableCapabilityRegistry
from .durable_provider_qualification import DurableProviderQualificationRegistry
from .financial_send_authority import (
    FinancialSendAuthority,
    FinancialSendAuthorityError,
    FinancialSendAuthorityIssuer,
    _detached_mapping_snapshot,
    require_exact_bybit_financial_request,
)
from .production_bybit import (
    ProductionBybitOrderSender,
    _build_production_bybit_order_sender,
)
from .production_financial_host import FinancialProductionHostRuntime
from .provider_route_dispatch import (
    _bound_submission_scope,
    compose_selected_provider_route_authority,
)
from .provider_route_financial_binding import (
    require_financial_binding_matches_selected_route,
)
from .provider_selection import SelectedProviderRoute
from .provider_transport import ClockMillis, ClockUtc, ProviderWireClient, QuotaGate
from .windows_secrets import PersistentCredentialHandle


class ProductionBybitRouteAuthorityError(PermissionError):
    """The financial host and selected provider route cannot share send authority."""


_ROUTE_BOUND_FACTORY_TOKEN = object()


class FinanciallyRouteBoundBybitOrderSender:
    """Bybit product sender with financial + exact-current selected C/Q authority."""

    __slots__ = (
        "__sender",
        "__issuer",
        "__runtime",
        "__route",
        "__capability_registry",
        "__qualification_registry",
        "__provider_environment",
    )

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("FinanciallyRouteBoundBybitOrderSender is sealed")

    def __init__(
        self,
        sender: ProductionBybitOrderSender,
        issuer: FinancialSendAuthorityIssuer,
        route: SelectedProviderRoute,
        capability_registry: DurableCapabilityRegistry,
        qualification_registry: DurableProviderQualificationRegistry,
        *,
        _factory_token: object = None,
    ) -> None:
        if _factory_token is not _ROUTE_BOUND_FACTORY_TOKEN:
            raise ProductionBybitRouteAuthorityError(
                "route-bound Bybit sender must come from canonical composition"
            )
        if type(sender) is not ProductionBybitOrderSender:
            raise TypeError("sender must be exact ProductionBybitOrderSender")
        if type(issuer) is not FinancialSendAuthorityIssuer:
            raise TypeError("issuer must be exact FinancialSendAuthorityIssuer")
        if type(route) is not SelectedProviderRoute:
            raise TypeError("route must be exact SelectedProviderRoute")
        if type(capability_registry) is not DurableCapabilityRegistry:
            raise TypeError("capability_registry must be exact DurableCapabilityRegistry")
        if type(qualification_registry) is not DurableProviderQualificationRegistry:
            raise TypeError(
                "qualification_registry must be exact DurableProviderQualificationRegistry"
            )

        runtime = issuer.runtime
        sender_runtime = getattr(sender, "_ProductionBybitOrderSender__runtime", None)
        if sender_runtime is not runtime:
            raise ProductionBybitRouteAuthorityError(
                "Bybit sender and financial issuer belong to different production hosts"
            )
        if route.candidate.provider_id != "BYBIT":
            raise ProductionBybitRouteAuthorityError(
                "route-bound Bybit sender requires a BYBIT selected route"
            )
        if sender.provider_environment != route.candidate.provider_environment:
            raise ProductionBybitRouteAuthorityError(
                "Bybit sender provider environment differs from selected route"
            )

        # This call performs construction-time exact store/environment/account and
        # route C/Q consistency validation.  The returned closure is deliberately
        # discarded; a fresh closure is composed from the issuer-derived financial
        # guard for every dispatch.
        compose_selected_provider_route_authority(
            store=runtime.journal,
            environment=runtime.config.environment,
            account_id=runtime.config.account_id,
            route=route,
            capability_registry=capability_registry,
            qualification_registry=qualification_registry,
            authority_check=lambda _intent_hash, _at: (True, "composition_probe"),
        )

        self.__sender = sender
        self.__issuer = issuer
        self.__runtime = runtime
        self.__route = route
        self.__capability_registry = capability_registry
        self.__qualification_registry = qualification_registry
        self.__provider_environment = sender.provider_environment

    @property
    def provider_environment(self) -> str:
        return self.__provider_environment

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
        final_barrier_clock: Callable[[], str] | None = None,
        submission_scope: Mapping[str, Any] | None = None,
    ) -> DispatchOutcome:
        request_snapshot = _detached_mapping_snapshot(request, name="request")
        caller_scope_snapshot = (
            {}
            if submission_scope is None
            else _detached_mapping_snapshot(
                submission_scope,
                name="submission_scope",
            )
        )
        route = self.__route
        bound_submission_scope = _bound_submission_scope(
            route,
            caller_scope_snapshot,
        )

        issuer = self.__issuer
        (
            financial_guard,
            binding,
            authority_intent_id,
            authority_intent_hash,
        ) = issuer._dispatch_material_for(authority)
        if issuer.runtime is not self.__runtime:
            raise FinancialSendAuthorityError("financial issuer production host changed")
        if authority_intent_id != intent_id or authority_intent_hash != intent_hash:
            raise FinancialSendAuthorityError(
                "dispatch intent differs from sealed financial authority"
            )

        require_financial_binding_matches_selected_route(binding, route)
        require_exact_bybit_financial_request(
            binding,
            request_snapshot,
            bound_submission_scope,
            provider_environment=self.__provider_environment,
        )
        combined_guard = compose_selected_provider_route_authority(
            store=self.__runtime.journal,
            environment=self.__runtime.config.environment,
            account_id=self.__runtime.config.account_id,
            route=route,
            capability_registry=self.__capability_registry,
            qualification_registry=self.__qualification_registry,
            authority_check=financial_guard,
        )

        return self.__sender.dispatch(
            attempt_id=attempt_id,
            intent_id=intent_id,
            intent_hash=intent_hash,
            request=request_snapshot,
            now=now,
            authority_check=combined_guard,
            client_id_max_length=client_id_max_length,
            client_id_format=client_id_format,
            final_barrier_clock=final_barrier_clock,
            submission_scope=bound_submission_scope,
        )


def bind_financial_selected_route_bybit_sender(
    sender: ProductionBybitOrderSender,
    issuer: FinancialSendAuthorityIssuer,
    route: SelectedProviderRoute,
    capability_registry: DurableCapabilityRegistry,
    qualification_registry: DurableProviderQualificationRegistry,
) -> FinanciallyRouteBoundBybitOrderSender:
    """Seal one raw Bybit sender to financial + selected provider route authority."""

    return FinanciallyRouteBoundBybitOrderSender(
        sender,
        issuer,
        route,
        capability_registry,
        qualification_registry,
        _factory_token=_ROUTE_BOUND_FACTORY_TOKEN,
    )


def build_route_bound_production_bybit_order_sender(
    runtime: FinancialProductionHostRuntime,
    *,
    financial_issuer: FinancialSendAuthorityIssuer,
    route: SelectedProviderRoute,
    capability_registry: DurableCapabilityRegistry,
    qualification_registry: DurableProviderQualificationRegistry,
    transport_capability_registry: CapabilityRegistry,
    credential_handle: PersistentCredentialHandle,
    session_token: str,
    clock_millis: ClockMillis,
    clock_utc: ClockUtc,
    quota_gate: QuotaGate | None = None,
    wire_client: ProviderWireClient | None = None,
    recv_window_ms: int = 5000,
) -> FinanciallyRouteBoundBybitOrderSender:
    """Build the product sender without caller-selected C/Q or financial callbacks."""

    if type(runtime) is not FinancialProductionHostRuntime:
        raise TypeError("runtime must be exact FinancialProductionHostRuntime")
    if type(financial_issuer) is not FinancialSendAuthorityIssuer:
        raise TypeError("financial_issuer must be exact FinancialSendAuthorityIssuer")
    if financial_issuer.runtime is not runtime:
        raise ProductionBybitRouteAuthorityError(
            "financial issuer and Bybit sender must share one production host"
        )
    if type(route) is not SelectedProviderRoute:
        raise TypeError("route must be exact SelectedProviderRoute")
    if route.candidate.provider_id != "BYBIT":
        raise ProductionBybitRouteAuthorityError(
            "route-bound Bybit factory requires a BYBIT selected route"
        )

    sender = _build_production_bybit_order_sender(
        runtime,
        provider_environment=route.candidate.provider_environment,
        capability_snapshot_id=route.capability_snapshot_id,
        capability_registry=transport_capability_registry,
        credential_handle=credential_handle,
        session_token=session_token,
        clock_millis=clock_millis,
        clock_utc=clock_utc,
        quota_gate=quota_gate,
        wire_client=wire_client,
        recv_window_ms=recv_window_ms,
    )
    return bind_financial_selected_route_bybit_sender(
        sender,
        financial_issuer,
        route,
        capability_registry,
        qualification_registry,
    )
