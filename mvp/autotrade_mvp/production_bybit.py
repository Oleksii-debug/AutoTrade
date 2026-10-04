"""Canonical Bybit write composition for the current production financial host.

This module owns no second dispatcher, provider transport, credential authority,
recovery state machine, or journal.  It binds the existing Bybit V5 transport to
the exact current financial-host lifetime, recovery-issued dispatcher, host
SecurityBoundary, provider domain, account, origin, owner generation and TRADE
credential selected by product composition.

Provider qualification, PAPER/LIVE campaign acceptance, release readiness and
economic edge remain separate authorities.  This seam only prevents application
callers from retargeting an otherwise-authorized host send to a different broker
scope or credential authority.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Mapping

from .capabilities import CapabilityRegistry
from .dispatch import AuthorityCheck, DispatchOutcome
from .host_network import AuthenticatedHostApplication
from .production_financial_host import (
    FinancialProductionHostRuntime,
    HostBoundFinancialDispatcher,
)
from .provider_transport import (
    BYBIT_V5_ENDPOINT_POLICIES,
    BybitV5HttpTransport,
    ClockMillis,
    ClockUtc,
    ProviderWireClient,
    QuotaGate,
)
from .recovery import RecoveryController
from .security import SecurityBoundary
from .windows_secrets import PersistentCredentialHandle


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be canonical exact text")
    return value


class _ProductionBybitSecretResolver:
    """ProviderSecretResolver adapter over the host's exact SecurityBoundary."""

    __slots__ = (
        "__runtime",
        "__application",
        "__security_boundary",
        "__recovery",
        "__dispatcher",
        "__owner",
        "__account_id",
        "__environment",
        "__origin",
        "__provider_environment",
        "__credential_handle",
        "__session_token",
    )

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("_ProductionBybitSecretResolver is sealed")

    def __init__(
        self,
        runtime: FinancialProductionHostRuntime,
        *,
        provider_environment: str,
        credential_handle: PersistentCredentialHandle,
        session_token: str,
    ) -> None:
        if type(runtime) is not FinancialProductionHostRuntime:
            raise TypeError("runtime must be exact FinancialProductionHostRuntime")
        if type(credential_handle) is not PersistentCredentialHandle:
            raise TypeError("credential_handle must be exact PersistentCredentialHandle")
        provider_environment = _exact_text(
            provider_environment,
            name="provider_environment",
        )
        session_token = _exact_text(session_token, name="session_token")

        application = runtime.application
        if type(application) is not AuthenticatedHostApplication:
            raise TypeError(
                "production financial host must retain exact AuthenticatedHostApplication"
            )
        security_boundary = application.security_boundary
        if type(security_boundary) is not SecurityBoundary:
            raise TypeError("production host SecurityBoundary authority is not exact")
        recovery = runtime.recovery_controller
        if type(recovery) is not RecoveryController:
            raise TypeError("production recovery authority is not exact")
        dispatcher = runtime.financial_dispatcher
        if type(dispatcher) is not HostBoundFinancialDispatcher:
            raise TypeError("production financial dispatcher authority is not exact")
        owner = dispatcher.owner
        if recovery.owner is not owner:
            raise PermissionError(
                "production recovery owner does not match financial dispatcher owner"
            )

        config = runtime.config
        if dispatcher.environment != config.environment:
            raise PermissionError("financial dispatcher environment does not match host")
        if dispatcher.account_id != config.account_id:
            raise PermissionError("financial dispatcher account does not match host")

        self.__runtime = runtime
        self.__application = application
        self.__security_boundary = security_boundary
        self.__recovery = recovery
        self.__dispatcher = dispatcher
        self.__owner = owner
        self.__account_id = config.account_id
        self.__environment = config.environment
        self.__origin = config.public_origin
        self.__provider_environment = provider_environment
        self.__credential_handle = credential_handle
        self.__session_token = session_token

    @property
    def security_boundary(self) -> SecurityBoundary:
        return self.__security_boundary

    def _require_runtime_authority(self) -> None:
        runtime = self.__runtime
        if type(runtime) is not FinancialProductionHostRuntime:
            raise PermissionError("production financial runtime authority changed")
        if runtime.closed or runtime.shutdown_requested:
            raise PermissionError("production financial host is closing or closed")
        if runtime.application is not self.__application:
            raise PermissionError("production host application authority changed")
        if self.__application.security_boundary is not self.__security_boundary:
            raise PermissionError("production host SecurityBoundary authority changed")
        if type(self.__security_boundary) is not SecurityBoundary:
            raise PermissionError("production host SecurityBoundary authority changed")
        if runtime.recovery_controller is not self.__recovery:
            raise PermissionError("production recovery controller authority changed")
        if runtime.financial_dispatcher is not self.__dispatcher:
            raise PermissionError("production financial dispatcher authority changed")
        if self.__recovery.owner is not self.__owner:
            raise PermissionError("production recovery owner changed")
        if self.__dispatcher.owner is not self.__owner:
            raise PermissionError("production financial sender owner changed")
        config = runtime.config
        if (
            config.account_id != self.__account_id
            or config.environment != self.__environment
            or config.public_origin != self.__origin
        ):
            raise PermissionError("production host financial scope changed")
        if self.__dispatcher.account_id != self.__account_id:
            raise PermissionError("production financial dispatcher account changed")
        if self.__dispatcher.environment != self.__environment:
            raise PermissionError("production financial dispatcher environment changed")

    @contextmanager
    def lease_for_execution(
        self,
        token: str,
        *,
        origin: str,
        handle: PersistentCredentialHandle,
        execution_identity: str,
        account_id: str,
        provider: str,
        environment: str,
        purpose: str,
        provider_environment: str | None = None,
    ):
        self._require_runtime_authority()
        if (
            type(token) is not str
            or token != self.__session_token
            or type(origin) is not str
            or origin != self.__origin
            or handle is not self.__credential_handle
            or type(execution_identity) is not str
            or execution_identity != self.__owner.owner_id
            or type(account_id) is not str
            or account_id != self.__account_id
            or type(provider) is not str
            or provider != "BYBIT"
            or type(environment) is not str
            or environment != self.__environment
            or type(purpose) is not str
            or purpose != "TRADE"
            or type(provider_environment) is not str
            or provider_environment != self.__provider_environment
        ):
            raise PermissionError(
                "Bybit credential request does not match production host authority"
            )

        with self.__security_boundary.lease_for_execution(
            self.__session_token,
            origin=self.__origin,
            handle=self.__credential_handle,
            execution_identity=self.__owner.owner_id,
            account_id=self.__account_id,
            provider="BYBIT",
            environment=self.__environment,
            purpose="TRADE",
            provider_environment=self.__provider_environment,
        ) as plaintext:
            yield plaintext


class ProductionBybitOrderSender:
    """Construction-bound Bybit transport plus current host financial dispatcher."""

    __slots__ = (
        "__runtime",
        "__transport",
        "__dispatcher",
        "__recovery",
        "__owner",
        "__resolver",
        "__policy",
        "__account_id",
        "__environment",
        "__origin",
        "__provider_environment",
        "__capability_snapshot_id",
        "__capability_registry",
        "__credential_handle",
        "__session_token",
    )

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("ProductionBybitOrderSender is sealed")

    def __init__(
        self,
        *,
        runtime: FinancialProductionHostRuntime,
        transport: BybitV5HttpTransport,
    ) -> None:
        if type(runtime) is not FinancialProductionHostRuntime:
            raise TypeError("runtime must be exact FinancialProductionHostRuntime")
        if type(transport) is not BybitV5HttpTransport:
            raise TypeError("transport must be exact BybitV5HttpTransport")
        dispatcher = runtime.financial_dispatcher
        recovery = runtime.recovery_controller
        owner = dispatcher.owner
        if recovery.owner is not owner:
            raise PermissionError("production recovery owner is not current sender owner")
        resolver = transport.secret_resolver
        if type(resolver) is not _ProductionBybitSecretResolver:
            raise RuntimeError("Bybit sender secret resolver is not production-bound")

        config = runtime.config
        policy = BYBIT_V5_ENDPOINT_POLICIES.get(transport.provider_environment)
        if policy is None or transport.policy is not policy:
            raise RuntimeError("Bybit sender policy is not canonical provider policy")
        if transport.account_id != config.account_id:
            raise RuntimeError("Bybit sender account does not match production host")
        if transport.policy.environment != config.environment:
            raise RuntimeError("Bybit sender environment does not match production host")
        if transport.execution_identity != owner.owner_id:
            raise RuntimeError("Bybit sender execution owner does not match recovery owner")
        if transport.origin != config.public_origin:
            raise RuntimeError("Bybit sender origin does not match production host")

        self.__runtime = runtime
        self.__transport = transport
        self.__dispatcher = dispatcher
        self.__recovery = recovery
        self.__owner = owner
        self.__resolver = resolver
        self.__policy = policy
        self.__account_id = config.account_id
        self.__environment = config.environment
        self.__origin = config.public_origin
        self.__provider_environment = transport.provider_environment
        self.__capability_snapshot_id = transport.capability_snapshot_id
        self.__capability_registry = transport.capability_registry
        self.__credential_handle = transport.credential_handle
        self.__session_token = transport.session_token

    @property
    def provider_environment(self) -> str:
        return self.__provider_environment

    def _require_send_authority(self) -> None:
        runtime = self.__runtime
        transport = self.__transport
        if runtime.closed or runtime.shutdown_requested:
            raise PermissionError("production financial host is closing or closed")
        if runtime.recovery_controller is not self.__recovery:
            raise PermissionError("production recovery controller authority changed")
        if runtime.financial_dispatcher is not self.__dispatcher:
            raise PermissionError("production financial dispatcher authority changed")
        if self.__recovery.owner is not self.__owner:
            raise PermissionError("production recovery owner is no longer current")
        if self.__dispatcher.owner is not self.__owner:
            raise PermissionError("production financial sender owner changed")
        if type(transport) is not BybitV5HttpTransport:
            raise PermissionError("Bybit transport authority changed")
        if transport.policy is not self.__policy:
            raise PermissionError("Bybit provider policy changed after composition")
        if transport.provider_environment != self.__provider_environment:
            raise PermissionError("Bybit provider environment changed after composition")
        if transport.account_id != self.__account_id:
            raise PermissionError("Bybit account changed after composition")
        if transport.policy.environment != self.__environment:
            raise PermissionError("Bybit runtime environment changed after composition")
        if transport.origin != self.__origin:
            raise PermissionError("Bybit authenticated origin changed after composition")
        if transport.execution_identity != self.__owner.owner_id:
            raise PermissionError("Bybit execution owner changed after composition")
        if transport.secret_resolver is not self.__resolver:
            raise PermissionError("Bybit credential resolver changed after composition")
        if transport.capability_snapshot_id != self.__capability_snapshot_id:
            raise PermissionError("Bybit capability snapshot changed after composition")
        if transport.capability_registry is not self.__capability_registry:
            raise PermissionError("Bybit capability registry changed after composition")
        if transport.credential_handle is not self.__credential_handle:
            raise PermissionError("Bybit credential handle changed after composition")
        if transport.session_token != self.__session_token:
            raise PermissionError("Bybit session authority changed after composition")
        self.__resolver._require_runtime_authority()

    def dispatch(
        self,
        *,
        attempt_id: str,
        intent_id: str,
        intent_hash: str,
        request: Mapping[str, Any],
        now: str,
        authority_check: AuthorityCheck,
        client_id_max_length: int = 36,
        client_id_format: str = "TOKEN",
        final_barrier_clock: Callable[[], str] | None = None,
        submission_scope: Mapping[str, Any] | None = None,
    ) -> DispatchOutcome:
        self._require_send_authority()
        return self.__dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id=intent_id,
            intent_hash=intent_hash,
            provider="BYBIT",
            request=request,
            now=now,
            authority_check=authority_check,
            transport_send=self.__transport,
            client_id_max_length=client_id_max_length,
            client_id_format=client_id_format,
            final_barrier_clock=final_barrier_clock,
            submission_scope=submission_scope,
        )


def build_production_bybit_order_sender(
    runtime: FinancialProductionHostRuntime,
    *,
    provider_environment: str,
    capability_snapshot_id: str,
    capability_registry: CapabilityRegistry,
    credential_handle: PersistentCredentialHandle,
    session_token: str,
    clock_millis: ClockMillis,
    clock_utc: ClockUtc,
    quota_gate: QuotaGate | None = None,
    wire_client: ProviderWireClient | None = None,
    recv_window_ms: int = 5000,
) -> ProductionBybitOrderSender:
    """Compose the existing Bybit writer under one current production host.

    MAINNET is LIVE only. TESTNET and DEMO are PAPER only. The caller chooses the
    already-qualified provider domain/capability/credential inputs, but cannot
    choose account id, product environment, host origin, execution owner,
    SecurityBoundary, credential purpose, dispatcher, or provider id.
    """

    if type(runtime) is not FinancialProductionHostRuntime:
        raise TypeError("runtime must be exact FinancialProductionHostRuntime")
    if runtime.closed or runtime.shutdown_requested:
        raise PermissionError("production financial host is closing or closed")
    provider_environment = _exact_text(
        provider_environment,
        name="provider_environment",
    )
    policy = BYBIT_V5_ENDPOINT_POLICIES.get(provider_environment)
    if policy is None:
        raise ValueError("Bybit provider_environment must be MAINNET, TESTNET or DEMO")

    dispatcher = runtime.financial_dispatcher
    recovery = runtime.recovery_controller
    if type(recovery) is not RecoveryController:
        raise TypeError("production recovery authority is not exact")
    if recovery.owner is not dispatcher.owner:
        raise PermissionError("production recovery owner is not current sender owner")

    config = runtime.config
    if config.environment not in {"PAPER", "LIVE"}:
        raise PermissionError("real-provider Bybit sender requires PAPER or LIVE host")
    if policy.environment != config.environment:
        raise PermissionError(
            "Bybit provider environment does not match production host environment"
        )
    if dispatcher.environment != config.environment:
        raise PermissionError("financial dispatcher environment does not match host")
    if dispatcher.account_id != config.account_id:
        raise PermissionError("financial dispatcher account does not match host")

    resolver = _ProductionBybitSecretResolver(
        runtime,
        provider_environment=provider_environment,
        credential_handle=credential_handle,
        session_token=session_token,
    )
    transport = BybitV5HttpTransport(
        policy=policy,
        provider_environment=provider_environment,
        account_id=config.account_id,
        capability_snapshot_id=capability_snapshot_id,
        capability_registry=capability_registry,
        secret_resolver=resolver,
        credential_handle=credential_handle,
        session_token=session_token,
        origin=config.public_origin,
        execution_identity=dispatcher.owner.owner_id,
        clock_millis=clock_millis,
        clock_utc=clock_utc,
        quota_gate=quota_gate,
        wire_client=wire_client,
        recv_window_ms=recv_window_ms,
    )
    return ProductionBybitOrderSender(runtime=runtime, transport=transport)
