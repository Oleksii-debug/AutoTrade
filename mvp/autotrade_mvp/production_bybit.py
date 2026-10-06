"""Canonical Bybit write composition for the current production financial host.

This module owns no second dispatcher, provider transport, credential authority,
recovery state machine, or journal. It binds the existing Bybit V5 transport to
the exact current financial-host lifetime, recovery-issued dispatcher, host
SecurityBoundary, provider domain, account, origin, owner generation and TRADE
credential selected by product composition.

Provider qualification, PAPER/LIVE campaign acceptance, release readiness and
economic edge remain separate authorities. This seam only prevents application
callers from retargeting an otherwise-authorized host send to a different broker
scope or credential authority.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Mapping

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
    ProviderEndpointPolicy,
    ProviderWireClient,
    QuotaGate,
)
from .recovery import RecoveryController
from .security import SecurityBoundary
from .windows_secrets import PersistentCredentialHandle

if TYPE_CHECKING:
    from .financial_send_authority import (
        FinancialSendAuthorityIssuer,
        FinanciallyBoundBybitOrderSender,
    )


_PRODUCTION_BYBIT_FACTORY_TOKEN = object()


_BYBIT_POLICY_IDENTITIES: Mapping[str, tuple[object, ...]] = {
    "MAINNET": (
        "BYBIT",
        "LIVE",
        "https://api.bybit.com",
        frozenset({"api.bybit.com"}),
        15,
    ),
    "TESTNET": (
        "BYBIT",
        "PAPER",
        "https://api-testnet.bybit.com",
        frozenset({"api-testnet.bybit.com"}),
        15,
    ),
    "DEMO": (
        "BYBIT",
        "PAPER",
        "https://api-demo.bybit.com",
        frozenset({"api-demo.bybit.com"}),
        15,
    ),
}


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be canonical exact text")
    return value


def _bybit_policy_identity(
    policy: ProviderEndpointPolicy,
    *,
    provider_environment: str,
) -> tuple[object, ...]:
    if type(policy) is not ProviderEndpointPolicy:
        raise PermissionError("Bybit provider policy authority is not exact")
    expected = _BYBIT_POLICY_IDENTITIES.get(provider_environment)
    if expected is None:
        raise PermissionError("Bybit provider environment authority is not canonical")
    state = vars(policy)
    if type(state) is not dict:
        raise PermissionError("Bybit provider policy state changed")
    provider_id = state.get("provider_id")
    environment = state.get("environment")
    base_url = state.get("base_url")
    allowed_hosts = state.get("allowed_hosts")
    timeout_seconds = state.get("timeout_seconds")
    if (
        type(provider_id) is not str
        or type(environment) is not str
        or type(base_url) is not str
        or type(allowed_hosts) is not frozenset
        or any(type(host) is not str for host in allowed_hosts)
        or type(timeout_seconds) is not int
    ):
        raise PermissionError("Bybit provider policy scalar authority changed")
    identity = (
        provider_id,
        environment,
        base_url,
        allowed_hosts,
        timeout_seconds,
    )
    if identity != expected:
        raise PermissionError("Bybit provider policy values changed")
    return identity


def _credential_identity(handle: PersistentCredentialHandle) -> tuple[object, ...]:
    if type(handle) is not PersistentCredentialHandle:
        raise PermissionError("Bybit credential handle authority is not exact")
    state = vars(handle)
    if type(state) is not dict:
        raise PermissionError("Bybit credential handle state changed")
    fields = (
        state.get("handle_id"),
        state.get("account_id"),
        state.get("provider"),
        state.get("environment"),
        state.get("provider_environment"),
        state.get("purpose"),
        state.get("generation"),
    )
    for value in fields[:6]:
        if type(value) is not str or not value or value != value.strip():
            raise PermissionError("Bybit credential handle scalar authority changed")
    if type(fields[6]) is not int or fields[6] < 1:
        raise PermissionError("Bybit credential generation authority changed")
    return fields


class _ProductionBybitSecretResolver:
    """ProviderSecretResolver adapter over the host's exact SecurityBoundary."""

    __slots__ = (
        "__runtime",
        "__application",
        "__security_boundary",
        "__security_lease",
        "__security_lease_code",
        "__security_lease_generator",
        "__security_lease_generator_code",
        "__recovery",
        "__dispatcher",
        "__owner",
        "__account_id",
        "__environment",
        "__origin",
        "__provider_environment",
        "__credential_handle",
        "__credential_identity",
        "__credential_identity_reader",
        "__credential_identity_reader_code",
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
        credential_identity_reader = _credential_identity
        credential_identity_reader_code = credential_identity_reader.__code__
        credential_identity = credential_identity_reader(credential_handle)

        application = runtime.application
        if type(application) is not AuthenticatedHostApplication:
            raise TypeError(
                "production financial host must retain exact AuthenticatedHostApplication"
            )
        security_boundary = application.security_boundary
        if type(security_boundary) is not SecurityBoundary:
            raise TypeError("production host SecurityBoundary authority is not exact")
        security_lease = SecurityBoundary.lease_for_execution
        security_lease_code = security_lease.__code__
        security_lease_generator = getattr(security_lease, "__wrapped__", None)
        if (
            not callable(security_lease_generator)
            or not hasattr(security_lease_generator, "__code__")
        ):
            raise TypeError("production credential lease implementation is not canonical")
        security_lease_generator_code = security_lease_generator.__code__
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
        self.__security_lease = security_lease
        self.__security_lease_code = security_lease_code
        self.__security_lease_generator = security_lease_generator
        self.__security_lease_generator_code = security_lease_generator_code
        self.__recovery = recovery
        self.__dispatcher = dispatcher
        self.__owner = owner
        self.__account_id = config.account_id
        self.__environment = config.environment
        self.__origin = config.public_origin
        self.__provider_environment = provider_environment
        self.__credential_handle = credential_handle
        self.__credential_identity = credential_identity
        self.__credential_identity_reader = credential_identity_reader
        self.__credential_identity_reader_code = credential_identity_reader_code
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
        security_lease = self.__security_lease
        if SecurityBoundary.lease_for_execution is not security_lease:
            raise PermissionError("production credential lease authority changed")
        if security_lease.__code__ is not self.__security_lease_code:
            raise PermissionError("production credential lease authority code changed")
        if (
            getattr(security_lease, "__wrapped__", None)
            is not self.__security_lease_generator
        ):
            raise PermissionError("production credential lease implementation changed")
        if (
            self.__security_lease_generator.__code__
            is not self.__security_lease_generator_code
        ):
            raise PermissionError(
                "production credential lease implementation code changed"
            )
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
        credential_identity_reader = self.__credential_identity_reader
        if _credential_identity is not credential_identity_reader:
            raise PermissionError("Bybit credential identity authority changed")
        if credential_identity_reader.__code__ is not self.__credential_identity_reader_code:
            raise PermissionError("Bybit credential identity authority code changed")
        if (
            credential_identity_reader(self.__credential_handle)
            != self.__credential_identity
        ):
            raise PermissionError("Bybit credential handle changed after composition")

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

        security_lease = self.__security_lease
        with security_lease(
            self.__security_boundary,
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
        "__transport_call",
        "__transport_call_code",
        "__wire_client",
        "__wire_send",
        "__wire_send_function",
        "__wire_send_code",
        "__dispatcher",
        "__recovery",
        "__owner",
        "__resolver",
        "__policy_registry",
        "__policy",
        "__policy_identity",
        "__policy_identity_reader",
        "__policy_identity_reader_code",
        "__account_id",
        "__environment",
        "__origin",
        "__provider_environment",
        "__capability_snapshot_id",
        "__capability_registry",
        "__credential_handle",
        "__credential_identity",
        "__credential_identity_reader",
        "__credential_identity_reader_code",
        "__session_token",
        "__clock_utc",
        "__clock_utc_code",
    )

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("ProductionBybitOrderSender is sealed")

    def __init__(
        self,
        *,
        runtime: FinancialProductionHostRuntime,
        transport: BybitV5HttpTransport,
        _factory_token: object = None,
    ) -> None:
        if _factory_token is not _PRODUCTION_BYBIT_FACTORY_TOKEN:
            raise PermissionError(
                "raw ProductionBybitOrderSender requires internal financial composition"
            )
        if type(runtime) is not FinancialProductionHostRuntime:
            raise TypeError("runtime must be exact FinancialProductionHostRuntime")
        if type(transport) is not BybitV5HttpTransport:
            raise TypeError("transport must be exact BybitV5HttpTransport")
        transport_call = BybitV5HttpTransport.__call__
        transport_call_code = transport_call.__code__
        wire_client = transport.wire_client
        wire_send = getattr(wire_client, "send", None)
        if not callable(wire_send):
            raise TypeError("Bybit wire client must expose a callable send")
        wire_send_function = getattr(wire_send, "__func__", None)
        if wire_send_function is not None:
            if getattr(wire_send, "__self__", None) is not wire_client:
                raise TypeError("Bybit wire send binding is inconsistent")
            wire_send_code = getattr(wire_send_function, "__code__", None)
        else:
            wire_send_code = getattr(wire_send, "__code__", None)
        dispatcher = runtime.financial_dispatcher
        recovery = runtime.recovery_controller
        owner = dispatcher.owner
        if recovery.owner is not owner:
            raise PermissionError("production recovery owner is not current sender owner")
        resolver = transport.secret_resolver
        if type(resolver) is not _ProductionBybitSecretResolver:
            raise RuntimeError("Bybit sender secret resolver is not production-bound")

        config = runtime.config
        provider_environment = transport.provider_environment
        policy_registry = BYBIT_V5_ENDPOINT_POLICIES
        policy_identity_reader = _bybit_policy_identity
        policy_identity_reader_code = policy_identity_reader.__code__
        credential_identity_reader = _credential_identity
        credential_identity_reader_code = credential_identity_reader.__code__
        policy = policy_registry.get(provider_environment)
        if policy is None or transport.policy is not policy:
            raise RuntimeError("Bybit sender policy is not canonical provider policy")
        policy_identity = policy_identity_reader(
            policy,
            provider_environment=provider_environment,
        )
        credential_identity = credential_identity_reader(transport.credential_handle)
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
        self.__transport_call = transport_call
        self.__transport_call_code = transport_call_code
        self.__wire_client = wire_client
        self.__wire_send = wire_send
        self.__wire_send_function = wire_send_function
        self.__wire_send_code = wire_send_code
        self.__dispatcher = dispatcher
        self.__recovery = recovery
        self.__owner = owner
        self.__resolver = resolver
        self.__policy_registry = policy_registry
        self.__policy = policy
        self.__policy_identity = policy_identity
        self.__policy_identity_reader = policy_identity_reader
        self.__policy_identity_reader_code = policy_identity_reader_code
        self.__account_id = config.account_id
        self.__environment = config.environment
        self.__origin = config.public_origin
        self.__provider_environment = provider_environment
        self.__capability_snapshot_id = transport.capability_snapshot_id
        self.__capability_registry = transport.capability_registry
        self.__credential_handle = transport.credential_handle
        self.__credential_identity = credential_identity
        self.__credential_identity_reader = credential_identity_reader
        self.__credential_identity_reader_code = credential_identity_reader_code
        self.__session_token = transport.session_token
        self.__clock_utc = transport.clock_utc
        self.__clock_utc_code = getattr(transport.clock_utc, "__code__", None)

    @property
    def provider_environment(self) -> str:
        return self.__provider_environment

    def _require_transport_executable(self) -> None:
        transport_call = self.__transport_call
        if BybitV5HttpTransport.__call__ is not transport_call:
            raise PermissionError("Bybit transport executable authority changed")
        if transport_call.__code__ is not self.__transport_call_code:
            raise PermissionError("Bybit transport executable authority code changed")

    def _require_wire_authority(self) -> None:
        transport = self.__transport
        wire_client = self.__wire_client
        if transport.wire_client is not wire_client:
            raise PermissionError("Bybit wire client authority changed")
        current_send = getattr(wire_client, "send", None)
        wire_send_function = self.__wire_send_function
        if wire_send_function is not None:
            if (
                getattr(current_send, "__self__", None) is not wire_client
                or getattr(current_send, "__func__", None) is not wire_send_function
            ):
                raise PermissionError("Bybit wire send authority changed")
            if (
                self.__wire_send_code is not None
                and getattr(wire_send_function, "__code__", None)
                is not self.__wire_send_code
            ):
                raise PermissionError("Bybit wire send authority code changed")
        else:
            if current_send is not self.__wire_send:
                raise PermissionError("Bybit wire send authority changed")
            if (
                self.__wire_send_code is not None
                and getattr(current_send, "__code__", None) is not self.__wire_send_code
            ):
                raise PermissionError("Bybit wire send authority code changed")

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
        self._require_transport_executable()
        self._require_wire_authority()
        if BYBIT_V5_ENDPOINT_POLICIES is not self.__policy_registry:
            raise PermissionError("Bybit provider policy registry authority changed")
        canonical_policy = self.__policy_registry.get(self.__provider_environment)
        if canonical_policy is not self.__policy or transport.policy is not self.__policy:
            raise PermissionError("Bybit provider policy changed after composition")
        policy_identity_reader = self.__policy_identity_reader
        if _bybit_policy_identity is not policy_identity_reader:
            raise PermissionError("Bybit provider policy identity authority changed")
        if policy_identity_reader.__code__ is not self.__policy_identity_reader_code:
            raise PermissionError("Bybit provider policy identity authority code changed")
        if (
            policy_identity_reader(
                self.__policy,
                provider_environment=self.__provider_environment,
            )
            != self.__policy_identity
        ):
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
        credential_identity_reader = self.__credential_identity_reader
        if _credential_identity is not credential_identity_reader:
            raise PermissionError("Bybit credential identity authority changed")
        if credential_identity_reader.__code__ is not self.__credential_identity_reader_code:
            raise PermissionError("Bybit credential identity authority code changed")
        if (
            credential_identity_reader(self.__credential_handle)
            != self.__credential_identity
        ):
            raise PermissionError("Bybit credential handle changed after composition")
        if transport.session_token != self.__session_token:
            raise PermissionError("Bybit session authority changed after composition")
        if transport.clock_utc is not self.__clock_utc:
            raise PermissionError("Bybit capability clock authority changed after composition")
        if (
            self.__clock_utc_code is not None
            and getattr(self.__clock_utc, "__code__", None) is not self.__clock_utc_code
        ):
            raise PermissionError("Bybit capability clock authority code changed")
        self.__resolver._require_runtime_authority()

    def _transport_send(
        self,
        client_order_id: str,
        request: Mapping[str, Any],
        final_guard: Callable[[], None],
    ):
        # Guard before transport work and once more after the dispatcher's final
        # authority callback. BybitV5HttpTransport performs wire_client.send()
        # immediately after final_guard(), so the second wire check closes the
        # caller-callback retargeting window at the terminal I/O boundary.
        self._require_send_authority()

        def terminal_guard() -> None:
            # A quota gate may block for long enough that host/recovery/domain
            # authority changes before the dispatcher's irreversible
            # SubmissionSending barrier.  Recheck product wire authority first
            # so a known-unsent revoked action remains pre-barrier/zero-wire.
            self._require_wire_authority()
            final_guard()
            # The dispatcher callback itself is caller-reachable composition.
            # Recheck once more immediately after it so callback-time retargeting
            # cannot cross the terminal I/O boundary.
            self._require_wire_authority()

        transport_call = self.__transport_call
        return transport_call(self.__transport, client_order_id, request, terminal_guard)

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

        if final_barrier_clock is None:
            clock_utc = self.__clock_utc
            clock_utc_code = self.__clock_utc_code

            def trusted_final_barrier_clock() -> str:
                if self.__transport.clock_utc is not clock_utc:
                    raise PermissionError(
                        "Bybit capability clock authority changed after composition"
                    )
                if (
                    clock_utc_code is not None
                    and getattr(clock_utc, "__code__", None) is not clock_utc_code
                ):
                    raise PermissionError(
                        "Bybit capability clock authority code changed"
                    )
                point = clock_utc()
                if (
                    type(point) is not datetime
                    or point.tzinfo is None
                    or point.utcoffset() is None
                ):
                    raise ValueError(
                        "Bybit capability clock must return exact timezone-aware datetime"
                    )
                return point.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

            selected_barrier_clock = trusted_final_barrier_clock
        else:
            selected_barrier_clock = final_barrier_clock

        return self.__dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id=intent_id,
            intent_hash=intent_hash,
            provider="BYBIT",
            request=request,
            now=now,
            authority_check=authority_check,
            transport_send=self._transport_send,
            client_id_max_length=client_id_max_length,
            client_id_format=client_id_format,
            final_barrier_clock=selected_barrier_clock,
            submission_scope=submission_scope,
        )


def _build_production_bybit_order_sender(
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
    """Compose the raw Bybit writer for trusted financial binding only.

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
    _bybit_policy_identity(policy, provider_environment=provider_environment)
    _credential_identity(credential_handle)

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
    return ProductionBybitOrderSender(
        runtime=runtime,
        transport=transport,
        _factory_token=_PRODUCTION_BYBIT_FACTORY_TOKEN,
    )


def build_production_bybit_order_sender(
    runtime: FinancialProductionHostRuntime,
    *,
    financial_issuer: "FinancialSendAuthorityIssuer",
    financial_binding_registry: "DurableFinancialRequestBindingRegistry",
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
) -> "DurableFinanciallyBoundBybitOrderSender":
    """Compose the product Bybit sender under persisted financial authority.

    Product composition cannot obtain the callback-taking raw sender or supply a
    detached financial capability through this public factory.  Every returned
    sender resolves the durable admitted-request binding immediately before the
    existing issuer mint and exact financially-bound provider dispatch.
    """

    from .durable_financial_bybit_sender import (
        bind_durable_financial_bybit_order_sender,
    )
    from .durable_financial_request_binding import (
        DurableFinancialRequestBindingRegistry,
    )
    from .financial_send_authority import (
        FinancialSendAuthorityIssuer,
        bind_financial_bybit_order_sender,
    )

    if type(financial_issuer) is not FinancialSendAuthorityIssuer:
        raise TypeError("financial_issuer must be exact FinancialSendAuthorityIssuer")
    if type(financial_binding_registry) is not DurableFinancialRequestBindingRegistry:
        raise TypeError(
            "financial_binding_registry must be exact DurableFinancialRequestBindingRegistry"
        )
    if financial_issuer.runtime is not runtime:
        raise PermissionError(
            "financial issuer and Bybit sender must share one production host"
        )
    sender = _build_production_bybit_order_sender(
        runtime,
        provider_environment=provider_environment,
        capability_snapshot_id=capability_snapshot_id,
        capability_registry=capability_registry,
        credential_handle=credential_handle,
        session_token=session_token,
        clock_millis=clock_millis,
        clock_utc=clock_utc,
        quota_gate=quota_gate,
        wire_client=wire_client,
        recv_window_ms=recv_window_ms,
    )
    financially_bound = bind_financial_bybit_order_sender(sender, financial_issuer)
    return bind_durable_financial_bybit_order_sender(
        financially_bound,
        financial_issuer,
        financial_binding_registry,
    )
