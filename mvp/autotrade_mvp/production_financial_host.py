"""Host-lifetime composition of recovery-owned financial send authority.

This module is a convergence adapter over the existing production host (#1117)
and recovery-issued dispatcher lineage (#1453/#1462).  It deliberately creates
no second listener, journal, recovery state machine, sender lock, or provider
transport.

The important ordering contract is:

* ``build_production_host`` acquires the canonical host instance fence first;
* only then is a RecoveryController bound to that exact JournalStore generation;
* a financial dispatcher is issued only for a current durable recovery owner;
* every financial dispatch holds the production runtime's existing lifecycle
  condition for its complete dispatch, so terminal teardown cannot cross the
  host-fence release while a provider send is in flight;
* once the host enters CLOSING/CLOSED/FAILED, no new financial dispatch can
  enter even if process-local recovery state has not yet been cleared.

Fresh journals may create epoch 1 automatically.  Existing durable ownership is
never silently reused: the runtime starts fail-closed and requires the explicit
crash-resumable takeover issuer before a financial dispatcher is available.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .dispatch import AuthorityCheck, DispatchOutcome, SenderCheck, TransportSend
from .production_host import (
    ProductionHostConfig,
    ProductionHostRuntime,
    _STOPPING_STATES,
    build_production_host,
)
from .recovery import HostState, RecoveryController
from .recovery_dispatch import (
    RecoveryIssuedDispatcher,
    build_recovery_issued_dispatcher,
)
from .recovery_takeover import DurableTakeoverResult, execute_durable_takeover
from .windows_secrets import PersistentCredentialHandle, ProtectedCredentialVault


@dataclass(frozen=True)
class _HostFinancialAuthority:
    """Exact product composition selected while the host fence is held."""

    config: ProductionHostConfig
    journal: object
    store_identity: object
    lifecycle_condition: object
    instance_fence: object
    environment: str
    account_id: str
    host_id: str


def _capture_host_financial_authority(
    host: ProductionHostRuntime,
) -> _HostFinancialAuthority:
    if type(host) is not ProductionHostRuntime:
        raise TypeError("host must be exact ProductionHostRuntime")
    config = host.config
    if type(config) is not ProductionHostConfig:
        raise PermissionError("production host config authority changed")
    for field_name in ("environment", "account_id", "host_id"):
        value = getattr(config, field_name)
        if type(value) is not str or not value:
            raise PermissionError(
                f"production host {field_name} must be exact non-empty text"
            )
    journal = host.journal
    store_identity = host.store_identity
    if journal.store_identity != store_identity:
        raise PermissionError("production host journal identity is inconsistent")
    return _HostFinancialAuthority(
        config=config,
        journal=journal,
        store_identity=store_identity,
        lifecycle_condition=host._lifecycle_condition,
        instance_fence=host._instance_fence,
        environment=config.environment,
        account_id=config.account_id,
        host_id=config.host_id,
    )


def _require_host_financial_authority(
    host: ProductionHostRuntime,
    authority: _HostFinancialAuthority,
) -> None:
    if type(host) is not ProductionHostRuntime:
        raise PermissionError("production host type changed")
    if type(authority) is not _HostFinancialAuthority:
        raise PermissionError("production host financial authority changed")
    config = host.config
    if config is not authority.config or type(config) is not ProductionHostConfig:
        raise PermissionError("production host config authority changed")
    current_values = (config.environment, config.account_id, config.host_id)
    if any(type(value) is not str for value in current_values) or current_values != (
        authority.environment,
        authority.account_id,
        authority.host_id,
    ):
        raise PermissionError("production host identity changed after composition")
    if host.journal is not authority.journal:
        raise PermissionError("production host journal changed after composition")
    if (
        host.store_identity != authority.store_identity
        or authority.journal.store_identity != authority.store_identity
    ):
        raise PermissionError("production host journal generation changed")
    if host._lifecycle_condition is not authority.lifecycle_condition:
        raise PermissionError("production host lifecycle authority changed")
    if host._instance_fence is not authority.instance_fence:
        raise PermissionError("production host instance-fence authority changed")


class HostBoundFinancialDispatcher:
    """Financial dispatch capability bounded by the canonical host lifetime."""

    __slots__ = ("__host", "__issued", "__host_authority")

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("HostBoundFinancialDispatcher is sealed")

    def __init__(
        self,
        host: ProductionHostRuntime,
        issued: RecoveryIssuedDispatcher,
        host_authority: _HostFinancialAuthority,
    ) -> None:
        if type(host) is not ProductionHostRuntime:
            raise TypeError("host must be exact ProductionHostRuntime")
        if type(issued) is not RecoveryIssuedDispatcher:
            raise TypeError("issued must be exact RecoveryIssuedDispatcher")
        if type(host_authority) is not _HostFinancialAuthority:
            raise TypeError("host_authority must be exact _HostFinancialAuthority")
        _require_host_financial_authority(host, host_authority)
        if issued.environment != host_authority.environment:
            raise PermissionError("issued dispatcher environment does not match host")
        if issued.account_id != host_authority.account_id:
            raise PermissionError("issued dispatcher account does not match host")
        self.__host = host
        self.__issued = issued
        self.__host_authority = host_authority

    @property
    def environment(self) -> str:
        return self.__issued.environment

    @property
    def account_id(self) -> str:
        return self.__issued.account_id

    @property
    def owner(self):
        return self.__issued.owner

    def dispatch(
        self,
        *,
        attempt_id: str,
        intent_id: str,
        intent_hash: str,
        provider: str,
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
        # This is the same lifecycle condition used by ProductionHostRuntime to
        # commit CLOSING before listener/fence teardown. Holding it across the
        # send makes the ordering race-free rather than a check-then-release.
        authority = self.__host_authority
        with authority.lifecycle_condition:
            _require_host_financial_authority(self.__host, authority)
            if (
                self.__host._serve_state in _STOPPING_STATES
                or authority.instance_fence.released
            ):
                raise PermissionError(
                    "production host lifetime no longer permits financial sends"
                )
            return self.__issued.dispatch(
                attempt_id=attempt_id,
                intent_id=intent_id,
                intent_hash=intent_hash,
                provider=provider,
                request=request,
                now=now,
                authority_check=authority_check,
                transport_send=transport_send,
                client_id_max_length=client_id_max_length,
                client_id_format=client_id_format,
                final_barrier_clock=final_barrier_clock,
                sender_check=sender_check,
                submission_scope=submission_scope,
            )


class FinancialProductionHostRuntime:
    """One production host with fail-closed recovery-owned financial authority."""

    __slots__ = (
        "__host",
        "__host_authority",
        "__recovery_controller",
        "__financial_dispatcher",
    )

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("FinancialProductionHostRuntime is sealed")

    def __init__(
        self,
        host: ProductionHostRuntime,
        host_authority: _HostFinancialAuthority,
        recovery_controller: RecoveryController,
        financial_dispatcher: HostBoundFinancialDispatcher | None,
    ) -> None:
        if type(host) is not ProductionHostRuntime:
            raise TypeError("host must be exact ProductionHostRuntime")
        if type(host_authority) is not _HostFinancialAuthority:
            raise TypeError("host_authority must be exact _HostFinancialAuthority")
        _require_host_financial_authority(host, host_authority)
        if type(recovery_controller) is not RecoveryController:
            raise TypeError("recovery_controller must be exact RecoveryController")
        if (
            financial_dispatcher is not None
            and type(financial_dispatcher) is not HostBoundFinancialDispatcher
        ):
            raise TypeError(
                "financial_dispatcher must be exact HostBoundFinancialDispatcher or None"
            )
        self.__host = host
        self.__host_authority = host_authority
        self.__recovery_controller = recovery_controller
        self.__financial_dispatcher = financial_dispatcher

    @property
    def config(self) -> ProductionHostConfig:
        _require_host_financial_authority(self.__host, self.__host_authority)
        return self.__host_authority.config

    @property
    def journal(self):
        _require_host_financial_authority(self.__host, self.__host_authority)
        return self.__host_authority.journal

    @property
    def store_identity(self):
        _require_host_financial_authority(self.__host, self.__host_authority)
        return self.__host_authority.store_identity

    @property
    def recovery_controller(self) -> RecoveryController:
        return self.__recovery_controller

    @property
    def application(self):
        _require_host_financial_authority(self.__host, self.__host_authority)
        return self.__host.application

    @property
    def server(self):
        _require_host_financial_authority(self.__host, self.__host_authority)
        return self.__host.server

    @property
    def closed(self) -> bool:
        return self.__host.closed

    @property
    def shutdown_requested(self) -> bool:
        return self.__host.shutdown_requested

    @property
    def serving(self) -> bool:
        return self.__host.serving

    @property
    def financial_dispatcher(self) -> HostBoundFinancialDispatcher:
        _require_host_financial_authority(self.__host, self.__host_authority)
        dispatcher = self.__financial_dispatcher
        if dispatcher is None:
            raise PermissionError(
                "financial sender unavailable until explicit durable takeover completes"
            )
        return dispatcher

    @property
    def takeover_required(self) -> bool:
        _require_host_financial_authority(self.__host, self.__host_authority)
        return self.__financial_dispatcher is None

    def takeover_financial_authority(
        self,
        *,
        vault: ProtectedCredentialVault,
        handle: PersistentCredentialHandle,
        execution_identity: str,
        reconciliation_id: str,
        provider_id: str,
    ) -> DurableTakeoverResult:
        """Complete/resume explicit owner takeover while the host fence is held."""

        host = self.__host
        authority = self.__host_authority
        with authority.lifecycle_condition:
            _require_host_financial_authority(host, authority)
            if (
                host._serve_state in _STOPPING_STATES
                or authority.instance_fence.released
            ):
                raise PermissionError(
                    "production host lifetime no longer permits financial takeover"
                )
            if self.__financial_dispatcher is not None:
                raise PermissionError(
                    "financial sender is already issued for this host lifetime"
                )
            result = execute_durable_takeover(
                self.__recovery_controller,
                new_owner_id=authority.host_id,
                vault=vault,
                handle=handle,
                execution_identity=execution_identity,
                reconciliation_id=reconciliation_id,
                provider_id=provider_id,
            )
            if result.target_owner.owner_id != authority.host_id:
                raise RuntimeError(
                    "durable takeover target does not match production host identity"
                )
            activated = self.__recovery_controller.activate_takeover_target_recovery()
            if activated != result.target_owner:
                raise RuntimeError(
                    "activated recovery owner does not match durable takeover target"
                )
            issued = build_recovery_issued_dispatcher(
                self.__recovery_controller,
                authority.journal,
                environment=authority.environment,
                account_id=authority.account_id,
            )
            self.__financial_dispatcher = HostBoundFinancialDispatcher(
                host,
                issued,
                authority,
            )
            return result

    def serve_forever(self, *, poll_interval: float = 0.5) -> None:
        _require_host_financial_authority(self.__host, self.__host_authority)
        try:
            self.__host.serve_forever(poll_interval=poll_interval)
        finally:
            # The host lifecycle gate already prevents post-CLOSING sends. This
            # clears process-local recovery authority after terminal host exit.
            self.__recovery_controller.stop()

    def close(self) -> None:
        try:
            self.__host.close()
        finally:
            self.__recovery_controller.stop()

    def __enter__(self) -> "FinancialProductionHostRuntime":
        _require_host_financial_authority(self.__host, self.__host_authority)
        self.__host.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.close()


def compose_financial_authority(
    host: ProductionHostRuntime,
) -> FinancialProductionHostRuntime:
    """Bind financial recovery authority to an already-fenced production host.

    This helper exists so the integration can be falsified without creating a
    second listener. The exact JournalStore retained by ``host`` is always the
    owner journal and submission journal.
    """

    if type(host) is not ProductionHostRuntime:
        raise TypeError("host must be exact ProductionHostRuntime")
    with host._lifecycle_condition:
        authority = _capture_host_financial_authority(host)
        if (
            host._serve_state in _STOPPING_STATES
            or authority.instance_fence.released
        ):
            raise PermissionError(
                "production host fence must be active before financial composition"
            )
        scope = f"{authority.environment}:{authority.account_id}"
        recovery = RecoveryController(
            owner_store=authority.journal,
            owner_scope=scope,
        )
        chain = recovery.durable_owner_chain()
        dispatcher: HostBoundFinancialDispatcher | None = None
        if not chain:
            recovery.start(authority.host_id)
            issued = build_recovery_issued_dispatcher(
                recovery,
                authority.journal,
                environment=authority.environment,
                account_id=authority.account_id,
            )
            dispatcher = HostBoundFinancialDispatcher(host, issued, authority)
        else:
            # Canonical restart attachment is takeover-only. Even if the source
            # owner later receives a current reconciliation checkpoint, recovery
            # validators deny sender/admission authority until the durable owner
            # chain has advanced exactly N -> N+1 through execute_durable_takeover.
            attached = recovery.attach_current_durable_owner_for_takeover()
            if attached != chain[-1]:
                raise RuntimeError("attached takeover source does not match durable tail")
        return FinancialProductionHostRuntime(
            host,
            authority,
            recovery,
            dispatcher,
        )


def build_financial_production_host(
    config: ProductionHostConfig,
    **host_kwargs,
) -> FinancialProductionHostRuntime:
    """Build canonical host first, then compose financial authority under its fence."""

    host = build_production_host(config, **host_kwargs)
    try:
        return compose_financial_authority(host)
    except BaseException:
        host.close()
        raise
