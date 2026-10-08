"""Host-lifetime composition of recovery-owned financial send authority.

This module is a convergence adapter over the existing production host (#1117)
and recovery-issued dispatcher lineage (#1453/#1462). It deliberately creates
no second listener, journal, recovery state machine, sender lock, or provider
transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .dispatch import AuthorityCheck, DispatchOutcome, SenderCheck, TransportSend
from .persistence import JournalStore
from .production_host import (
    ProductionHostConfig,
    ProductionHostRuntime,
    _STOPPING_STATES,
    build_production_host,
)
from .recovery import HostState, RecoveryController
from .recovery_dispatch import (
    RecoveryIssuedDispatcher,
    activate_recovery_takeover_target,
    build_recovery_issued_dispatcher,
    mark_recovery_takeover_source,
)
from .recovery_takeover import DurableTakeoverResult, execute_durable_takeover
from .windows_secrets import PersistentCredentialHandle, ProtectedCredentialVault


@dataclass(frozen=True)
class _HostFinancialAuthority:
    """Exact product composition selected while the host fence is held."""

    config_identity: tuple[object, ...]
    journal: JournalStore
    store_identity: object
    lifecycle_condition: object
    instance_fence: object
    environment: str
    account_id: str
    host_id: str


def _config_identity(config: ProductionHostConfig) -> tuple[object, ...]:
    if type(config) is not ProductionHostConfig:
        raise PermissionError("production host config authority changed")
    state = vars(config)
    if type(state) is not dict:
        raise PermissionError("production host config state changed")
    for field_name in (
        "account_id",
        "environment",
        "host_id",
        "bind_host",
        "public_origin",
    ):
        value = state.get(field_name)
        if type(value) is not str or not value:
            raise PermissionError(
                f"production host {field_name} must be exact non-empty text"
            )
    if type(state.get("bind_port")) is not int:
        raise PermissionError("production host bind_port must be an exact integer")
    journal_path = state.get("journal_path")
    if type(journal_path) is not type(config.journal_path):
        raise PermissionError("production host journal_path authority changed")
    return (
        journal_path,
        state["account_id"],
        state["environment"],
        state["host_id"],
        state["bind_host"],
        state["bind_port"],
        state["public_origin"],
    )


def _capture_host_financial_authority(
    host: ProductionHostRuntime,
) -> _HostFinancialAuthority:
    if type(host) is not ProductionHostRuntime:
        raise TypeError("host must be exact ProductionHostRuntime")
    config = host.config
    identity = _config_identity(config)
    journal = host.journal
    if type(journal) is not JournalStore:
        raise PermissionError("production host journal authority changed")
    store_identity = host.store_identity
    if journal.store_identity != store_identity:
        raise PermissionError("production host journal identity is inconsistent")
    if config.journal_path != journal.path:
        raise PermissionError("production host config journal does not match host journal")
    return _HostFinancialAuthority(
        config_identity=identity,
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
) -> ProductionHostConfig:
    if type(host) is not ProductionHostRuntime:
        raise PermissionError("production host type changed")
    if type(authority) is not _HostFinancialAuthority:
        raise PermissionError("production host financial authority changed")
    config = host.config
    if _config_identity(config) != authority.config_identity:
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
    return config


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
        return _require_host_financial_authority(
            self.__host,
            self.__host_authority,
        )

    @property
    def journal(self) -> JournalStore:
        _require_host_financial_authority(self.__host, self.__host_authority)
        return self.__host_authority.journal

    @property
    def store_identity(self):
        _require_host_financial_authority(self.__host, self.__host_authority)
        return self.__host_authority.store_identity

    @property
    def recovery_controller(self) -> RecoveryController:
        _require_host_financial_authority(self.__host, self.__host_authority)
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
            activated = activate_recovery_takeover_target(
                self.__recovery_controller,
                source=result.source_owner,
                target=result.target_owner,
                takeover=result,
                vault=vault,
            )
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
    """Bind financial recovery authority to an already-fenced production host."""

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
        # Revoke recovery-issued sender authority inside the canonical host's
        # teardown, after command drain/join and before listener/fence release.
        # Bind before any durable owner mutation so failed composition cannot
        # mint an owner without a guaranteed terminal revocation path.
        host.bind_terminal_finalizer(recovery.stop)
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
            source = chain[-1]
            recovery.owner = source
            recovery.state = HostState.RECOVERING
            recovery.provider_reconciled = False
            recovery.reason_codes = {"startup_reconciliation_required"}
            recovery._recover_scoped_submission_uncertainty_from_owner_scope()
            mark_recovery_takeover_source(recovery, source)
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
