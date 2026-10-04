"""Canonical Bybit write composition for one fenced financial production host.

This module creates no second dispatcher, recovery state, credential store or
transport authority.  It binds the existing Bybit V5 transport to the exact
FinancialProductionHostRuntime authority selected under the production host
fence.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Mapping

from .capabilities import CapabilityRegistry
from .dispatch import AuthorityCheck, DispatchOutcome
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
from .windows_secrets import PersistentCredentialHandle


class _ProductionBybitSecretResolver:
    """Protocol adapter over the host-owned TRADE secret lease."""

    __slots__ = ("__runtime", "__dispatcher")

    def __init__(
        self,
        runtime: FinancialProductionHostRuntime,
        dispatcher: HostBoundFinancialDispatcher,
    ) -> None:
        if type(runtime) is not FinancialProductionHostRuntime:
            raise TypeError("runtime must be exact FinancialProductionHostRuntime")
        if type(dispatcher) is not HostBoundFinancialDispatcher:
            raise TypeError("dispatcher must be exact HostBoundFinancialDispatcher")
        self.__runtime = runtime
        self.__dispatcher = dispatcher

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
        runtime = self.__runtime
        dispatcher = runtime.financial_dispatcher
        if dispatcher is not self.__dispatcher:
            raise PermissionError("production financial dispatcher authority changed")
        owner = dispatcher.owner
        if (
            account_id != runtime.config.account_id
            or environment != runtime.config.environment
            or purpose != "TRADE"
            or provider != "BYBIT"
            or execution_identity != owner.owner_id
            or origin != runtime.config.public_origin
        ):
            raise PermissionError(
                "Bybit credential request does not match production host authority"
            )
        if runtime.recovery_controller.owner != owner:
            raise PermissionError("production recovery owner is no longer current")

        with runtime.lease_provider_trade_secret(
            token,
            origin=origin,
            handle=handle,
            execution_identity=execution_identity,
            provider="BYBIT",
            provider_environment=provider_environment,
        ) as plaintext:
            yield plaintext


class ProductionBybitOrderSender:
    """Construction-bound Bybit transport plus recovery-issued host dispatcher."""

    __slots__ = ("__runtime", "__dispatcher", "__transport")

    def __init__(
        self,
        *,
        runtime: FinancialProductionHostRuntime,
        dispatcher: HostBoundFinancialDispatcher,
        transport: BybitV5HttpTransport,
    ) -> None:
        if type(runtime) is not FinancialProductionHostRuntime:
            raise TypeError("runtime must be exact FinancialProductionHostRuntime")
        if type(dispatcher) is not HostBoundFinancialDispatcher:
            raise TypeError("dispatcher must be exact HostBoundFinancialDispatcher")
        if type(transport) is not BybitV5HttpTransport:
            raise TypeError("transport must be exact BybitV5HttpTransport")
        if runtime.financial_dispatcher is not dispatcher:
            raise RuntimeError("Bybit sender dispatcher is not the host dispatcher")
        if transport.account_id != runtime.config.account_id:
            raise RuntimeError("Bybit sender account does not match production host")
        if transport.policy.environment != runtime.config.environment:
            raise RuntimeError("Bybit sender environment does not match production host")
        if transport.execution_identity != dispatcher.owner.owner_id:
            raise RuntimeError("Bybit sender execution owner does not match recovery owner")
        if transport.origin != runtime.config.public_origin:
            raise RuntimeError("Bybit sender origin does not match production host")
        if not isinstance(transport.secret_resolver, _ProductionBybitSecretResolver):
            raise RuntimeError("Bybit sender secret resolver is not production-bound")
        self.__runtime = runtime
        self.__dispatcher = dispatcher
        self.__transport = transport

    @property
    def provider_environment(self) -> str:
        return self.__transport.provider_environment

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
        dispatcher = self.__runtime.financial_dispatcher
        if dispatcher is not self.__dispatcher:
            raise PermissionError("production financial dispatcher authority changed")
        if self.__runtime.recovery_controller.owner != dispatcher.owner:
            raise PermissionError("production recovery owner is no longer current")
        return dispatcher.dispatch(
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
    """Bind the existing Bybit V5 writer to exact host/recovery authority."""

    if type(runtime) is not FinancialProductionHostRuntime:
        raise TypeError("runtime must be exact FinancialProductionHostRuntime")
    dispatcher = runtime.financial_dispatcher
    owner = dispatcher.owner
    if runtime.recovery_controller.owner != owner:
        raise PermissionError("production recovery owner is no longer current")
    if type(provider_environment) is not str or not provider_environment.strip():
        raise ValueError("provider_environment must be exact non-empty text")

    provider_env = provider_environment.strip().upper()
    policy = BYBIT_V5_ENDPOINT_POLICIES.get(provider_env)
    if policy is None:
        raise ValueError("Bybit provider_environment must be MAINNET, TESTNET or DEMO")
    if policy.environment != runtime.config.environment:
        raise PermissionError(
            "Bybit provider environment does not match production host environment"
        )

    resolver = _ProductionBybitSecretResolver(runtime, dispatcher)
    transport = BybitV5HttpTransport(
        policy=policy,
        provider_environment=provider_env,
        account_id=runtime.config.account_id,
        capability_snapshot_id=capability_snapshot_id,
        capability_registry=capability_registry,
        secret_resolver=resolver,
        credential_handle=credential_handle,
        session_token=session_token,
        origin=runtime.config.public_origin,
        execution_identity=owner.owner_id,
        clock_millis=clock_millis,
        clock_utc=clock_utc,
        quota_gate=quota_gate,
        wire_client=wire_client,
        recv_window_ms=recv_window_ms,
    )
    return ProductionBybitOrderSender(
        runtime=runtime,
        dispatcher=dispatcher,
        transport=transport,
    )
