"""Canonical Bybit write composition for the fenced production financial host.

This module adds no second dispatcher, provider transport, credential authority or
recovery state.  It binds the existing ``BybitV5HttpTransport`` to the existing
``ProductionFinancialHostRuntime`` so account/environment, sender owner,
authenticated origin and TRADE-secret lifetime are construction-owned rather than
caller-selected.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Mapping

from .capabilities import CapabilityRegistry
from .dispatch import AuthorityCheck, DispatchOutcome
from .production_financial_host import ProductionFinancialHostRuntime
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
    """Protocol adapter over the host's already-owned TRADE lease resolver."""

    def __init__(self, runtime: ProductionFinancialHostRuntime) -> None:
        if not isinstance(runtime, ProductionFinancialHostRuntime):
            raise TypeError("runtime must be ProductionFinancialHostRuntime")
        self._runtime = runtime

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
        runtime = self._runtime
        if (
            account_id != runtime.config.account_id
            or environment != runtime.config.environment
            or purpose != "TRADE"
            or provider != "BYBIT"
            or execution_identity != runtime.owner.owner_id
            or origin != runtime.config.public_origin
        ):
            raise PermissionError(
                "Bybit credential request does not match production host authority"
            )
        if runtime.recovery_controller.owner != runtime.owner:
            raise PermissionError("production recovery owner is no longer current")
        if not runtime.provider_secret_resolver.accepting:
            raise PermissionError("production host provider credential leases are closed")

        # The underlying host resolver already pins account/environment/purpose
        # when it delegates to SecurityBoundary.  This adapter supplies the exact
        # ProviderSecretResolver signature required by existing provider transports
        # without creating another credential store or lease authority.
        with runtime.provider_secret_resolver.lease_for_execution(
            token,
            origin=origin,
            handle=handle,
            execution_identity=execution_identity,
            provider=provider,
            provider_environment=provider_environment,
        ) as plaintext:
            yield plaintext


class ProductionBybitOrderSender:
    """Construction-bound Bybit transport plus host-owned guarded dispatcher."""

    def __init__(
        self,
        *,
        runtime: ProductionFinancialHostRuntime,
        transport: BybitV5HttpTransport,
    ) -> None:
        if not isinstance(runtime, ProductionFinancialHostRuntime):
            raise TypeError("runtime must be ProductionFinancialHostRuntime")
        if not isinstance(transport, BybitV5HttpTransport):
            raise TypeError("transport must be BybitV5HttpTransport")
        if transport.account_id != runtime.config.account_id:
            raise RuntimeError("Bybit sender account does not match production host")
        if transport.policy.environment != runtime.config.environment:
            raise RuntimeError("Bybit sender environment does not match production host")
        if transport.execution_identity != runtime.owner.owner_id:
            raise RuntimeError("Bybit sender execution owner does not match recovery owner")
        if transport.origin != runtime.config.public_origin:
            raise RuntimeError("Bybit sender origin does not match production host")
        if not isinstance(transport.secret_resolver, _ProductionBybitSecretResolver):
            raise RuntimeError("Bybit sender secret resolver is not production-bound")

        self._runtime = runtime
        self._transport = transport

    @property
    def provider_environment(self) -> str:
        return self._transport.provider_environment

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
        runtime = self._runtime
        if runtime.closed or runtime.host.shutdown_requested:
            raise RuntimeError("production financial host is closing or closed")
        if runtime.recovery_controller.owner != runtime.owner:
            raise PermissionError("production recovery owner is no longer current")
        return runtime.dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id=intent_id,
            intent_hash=intent_hash,
            provider="BYBIT",
            request=request,
            now=now,
            authority_check=authority_check,
            transport_send=self._transport,
            client_id_max_length=client_id_max_length,
            client_id_format=client_id_format,
            final_barrier_clock=final_barrier_clock,
            submission_scope=submission_scope,
        )


def build_production_bybit_order_sender(
    runtime: ProductionFinancialHostRuntime,
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
    """Compose the existing Bybit writer under one production host authority.

    TESTNET and DEMO are PAPER only; MAINNET is LIVE only.  The caller cannot
    choose account id, product environment, authenticated origin, execution owner,
    credential purpose or secret resolver.  Those are all derived from ``runtime``.
    """

    if not isinstance(runtime, ProductionFinancialHostRuntime):
        raise TypeError("runtime must be ProductionFinancialHostRuntime")
    if runtime.closed or runtime.host.shutdown_requested:
        raise RuntimeError("production financial host is closing or closed")
    if runtime.recovery_controller.owner != runtime.owner:
        raise PermissionError("production recovery owner is no longer current")
    if not runtime.dispatcher.accepting:
        raise PermissionError("production host provider dispatch is closed")
    if not runtime.provider_secret_resolver.accepting:
        raise PermissionError("production host provider credential leases are closed")
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

    transport = BybitV5HttpTransport(
        policy=policy,
        provider_environment=provider_env,
        account_id=runtime.config.account_id,
        capability_snapshot_id=capability_snapshot_id,
        capability_registry=capability_registry,
        secret_resolver=_ProductionBybitSecretResolver(runtime),
        credential_handle=credential_handle,
        session_token=session_token,
        origin=runtime.config.public_origin,
        execution_identity=runtime.owner.owner_id,
        clock_millis=clock_millis,
        clock_utc=clock_utc,
        quota_gate=quota_gate,
        wire_client=wire_client,
        recv_window_ms=recv_window_ms,
    )
    return ProductionBybitOrderSender(runtime=runtime, transport=transport)
