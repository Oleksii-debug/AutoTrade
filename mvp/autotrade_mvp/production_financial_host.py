"""Financial-authority composition inside the canonical production-host lifetime.

This module does not create a second host, journal, server, recovery engine or
process lock.  It composes the existing ``build_production_host`` lifecycle with
the existing durable ``RecoveryController`` and ``SecurityBoundary`` so canonical
recovery-owner and provider-secret lease authority exists only while the
production-host instance fence is held.

The first durable owner is intentionally the only automatic path here.  A
journal that already has a recovery owner fails closed and must use the canonical
issued takeover protocol before a successor can become owner.
"""

from __future__ import annotations

from contextlib import contextmanager
import ssl
from threading import Condition
from typing import Callable

from .dispatch import GuardedDispatcher
from .host_network import PrincipalResolver, SnapshotProvider
from .persistence import JournalStore
from .production_host import (
    ProductionHostConfig,
    ProductionHostRuntime,
    build_production_host,
)
from .recovery import OwnerFence, RecoveryController
from .security import SecurityBoundary


# Executable authorities are captured once and revalidated before trust use.
# This remains trusted-process composition provenance rather than a hostile-code
# sandbox, but later class rebinding or same-function code mutation must not
# silently redefine the product's terminal sender/credential authority.
_CANONICAL_VALIDATE_SENDER = RecoveryController.validate_sender
_CANONICAL_VALIDATE_SENDER_CODE = RecoveryController.validate_sender.__code__
_CANONICAL_SECURITY_LEASE = SecurityBoundary.lease_for_execution
_CANONICAL_SECURITY_LEASE_CODE = SecurityBoundary.lease_for_execution.__code__
_CANONICAL_GUARDED_DISPATCH = GuardedDispatcher.dispatch
_CANONICAL_GUARDED_DISPATCH_CODE = GuardedDispatcher.dispatch.__code__


def _require_recovery_sender_executable() -> None:
    if RecoveryController.validate_sender is not _CANONICAL_VALIDATE_SENDER:
        raise PermissionError("recovery sender validator authority changed")
    if _CANONICAL_VALIDATE_SENDER.__code__ is not _CANONICAL_VALIDATE_SENDER_CODE:
        raise PermissionError("recovery sender validator code changed")


def _require_guarded_dispatch_executable() -> None:
    if GuardedDispatcher.dispatch is not _CANONICAL_GUARDED_DISPATCH:
        raise PermissionError("guarded dispatcher executable authority changed")
    if _CANONICAL_GUARDED_DISPATCH.__code__ is not _CANONICAL_GUARDED_DISPATCH_CODE:
        raise PermissionError("guarded dispatcher executable code changed")


def _require_security_lease_executable() -> None:
    if SecurityBoundary.lease_for_execution is not _CANONICAL_SECURITY_LEASE:
        raise PermissionError("security credential lease authority changed")
    if _CANONICAL_SECURITY_LEASE.__code__ is not _CANONICAL_SECURITY_LEASE_CODE:
        raise PermissionError("security credential lease code changed")


def _require_owner(
    owner: OwnerFence,
    *,
    expected_id: str | None = None,
    expected_epoch: int | None = None,
) -> None:
    if type(owner) is not OwnerFence:
        raise PermissionError("recovery owner authority changed")
    if (
        type(owner.owner_id) is not str
        or not owner.owner_id
        or owner.owner_id != owner.owner_id.strip()
    ):
        raise PermissionError("recovery owner identity is not canonical exact text")
    if type(owner.epoch) is not int or owner.epoch < 1:
        raise PermissionError("recovery owner epoch is not a positive exact integer")
    if expected_id is not None and owner.owner_id != expected_id:
        raise PermissionError("recovery owner identity changed after composition")
    if expected_epoch is not None and owner.epoch != expected_epoch:
        raise PermissionError("recovery owner epoch changed after composition")


class HostLifetimeProviderSecretResolver:
    """Canonical provider-secret lease seam bounded by one host lifetime.

    Provider transports receive this resolver instead of a raw SecurityBoundary.
    New leases are rejected after shutdown begins, and shutdown waits for every
    admitted lease to leave its underlying vault context before the production
    host can release its process-lifetime instance fence.
    """

    def __init__(
        self,
        security_boundary: SecurityBoundary,
        *,
        account_id: str,
        environment: str,
    ) -> None:
        lease = getattr(security_boundary, "lease_for_execution", None)
        if not callable(lease):
            raise TypeError("security_boundary must provide lease_for_execution")
        if (
            type(account_id) is not str
            or not account_id
            or account_id != account_id.strip()
        ):
            raise ValueError("provider credential account_id must be canonical text")
        if (
            type(environment) is not str
            or environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}
        ):
            raise ValueError("provider credential environment is not canonical")

        # Production uses the exact SecurityBoundary implementation. Focused
        # unit tests may supply a deterministic duck-typed lease boundary; in
        # either case retain the construction-selected callable instead of later
        # resolving a mutable instance attribute at credential-use time.
        self._security_boundary = security_boundary
        self._canonical_security_boundary = type(security_boundary) is SecurityBoundary
        if self._canonical_security_boundary:
            _require_security_lease_executable()
            lease = _CANONICAL_SECURITY_LEASE.__get__(
                security_boundary,
                SecurityBoundary,
            )
        self._lease_for_execution = lease
        self._account_id = account_id
        self._environment = environment
        self._condition = Condition()
        self._accepting = True
        self._active = 0

    @property
    def accepting(self) -> bool:
        with self._condition:
            return self._accepting

    @property
    def active_leases(self) -> int:
        with self._condition:
            return self._active

    def _require_lease_authority(self) -> None:
        lease = self._lease_for_execution
        if not callable(lease):
            raise PermissionError("provider credential lease callable changed")
        if self._canonical_security_boundary:
            if type(self._security_boundary) is not SecurityBoundary:
                raise PermissionError("production SecurityBoundary authority changed")
            _require_security_lease_executable()
            if getattr(lease, "__self__", None) is not self._security_boundary:
                raise PermissionError("bound security credential lease authority changed")
            if getattr(lease, "__func__", None) is not _CANONICAL_SECURITY_LEASE:
                raise PermissionError("bound security credential lease executable changed")

    @contextmanager
    def lease_for_execution(
        self,
        token: str,
        *,
        origin: str,
        handle,
        execution_identity: str,
        provider: str,
        provider_environment: str | None = None,
    ):
        self._require_lease_authority()
        with self._condition:
            if not self._accepting:
                raise PermissionError("production host provider credential leases are closed")
            self._active += 1

        try:
            # Use the exact callable selected at construction. Instance/class
            # rebinding after composition therefore cannot redirect plaintext
            # credential resolution to a different authority.
            with self._lease_for_execution(
                token,
                origin=origin,
                handle=handle,
                execution_identity=execution_identity,
                account_id=self._account_id,
                provider=provider,
                environment=self._environment,
                purpose="TRADE",
                provider_environment=provider_environment,
            ) as plaintext:
                yield plaintext
        finally:
            with self._condition:
                self._active -= 1
                if self._active == 0:
                    self._condition.notify_all()

    def stop_and_drain(self) -> None:
        """Close admission and wait until every admitted secret lease is gone."""

        with self._condition:
            self._accepting = False
            while self._active:
                self._condition.wait()


class HostLifetimeGuardedDispatcher:
    """Construction-bound provider sender for one recovery-owner generation.

    Callers never supply a sender fence callback. The wrapper retains the exact
    canonical GuardedDispatcher built from the production JournalStore and always
    routes its terminal sender check through the RecoveryController that owns the
    same durable account scope.
    """

    def __init__(
        self,
        dispatcher: GuardedDispatcher,
        *,
        recovery_controller: RecoveryController,
        owner: OwnerFence,
    ) -> None:
        if type(dispatcher) is not GuardedDispatcher:
            raise TypeError("dispatcher must be exact GuardedDispatcher")
        if type(recovery_controller) is not RecoveryController:
            raise TypeError("recovery_controller must be exact RecoveryController")
        if type(owner) is not OwnerFence:
            raise TypeError("owner must be exact OwnerFence")
        _require_owner(owner)
        _require_recovery_sender_executable()
        _require_guarded_dispatch_executable()
        if recovery_controller.owner is not owner:
            raise RuntimeError("dispatcher recovery owner is not current")
        expected_scope = f"{dispatcher.environment}:{dispatcher.account_id}"
        if (
            type(recovery_controller.owner_scope) is not str
            or recovery_controller.owner_scope != expected_scope
        ):
            raise RuntimeError("dispatcher recovery scope does not match provider scope")
        if recovery_controller.durable_owner_store_path != dispatcher.store.path:
            raise RuntimeError("dispatcher journal is not the durable recovery journal")
        if (
            dispatcher.owner_token != owner.owner_id
            or dispatcher.owner_epoch != owner.epoch
        ):
            raise RuntimeError("dispatcher sender identity does not match recovery owner")

        self._dispatcher = dispatcher
        self._store = dispatcher.store
        self._recovery_controller = recovery_controller
        self._owner = owner
        self._owner_id = owner.owner_id
        self._owner_epoch = owner.epoch
        self._environment = dispatcher.environment
        self._account_id = dispatcher.account_id
        self._owner_scope = expected_scope
        self._sender_check = _CANONICAL_VALIDATE_SENDER.__get__(
            recovery_controller,
            RecoveryController,
        )
        # Retain a canonical executable witness so class/code drift is detected.
        # Dispatch still uses the wrapped instance method to preserve the existing
        # trusted-process test/instrumentation seam; caller-selected sender_check
        # remains impossible at this wrapper boundary.
        self._dispatch = _CANONICAL_GUARDED_DISPATCH.__get__(
            dispatcher,
            GuardedDispatcher,
        )
        self._condition = Condition()
        self._accepting = True
        self._active = 0

    @property
    def accepting(self) -> bool:
        with self._condition:
            return self._accepting

    @property
    def active_dispatches(self) -> int:
        with self._condition:
            return self._active

    @property
    def owner(self) -> OwnerFence:
        self._require_dispatch_authority()
        return self._owner

    def _require_dispatch_authority(self) -> None:
        if type(self._dispatcher) is not GuardedDispatcher:
            raise PermissionError("guarded dispatcher authority changed")
        if type(self._recovery_controller) is not RecoveryController:
            raise PermissionError("recovery controller authority changed")
        _require_owner(
            self._owner,
            expected_id=self._owner_id,
            expected_epoch=self._owner_epoch,
        )
        _require_recovery_sender_executable()
        _require_guarded_dispatch_executable()
        if getattr(self._sender_check, "__self__", None) is not self._recovery_controller:
            raise PermissionError("bound recovery sender authority changed")
        if getattr(self._sender_check, "__func__", None) is not _CANONICAL_VALIDATE_SENDER:
            raise PermissionError("bound recovery sender validator changed")
        if getattr(self._dispatch, "__self__", None) is not self._dispatcher:
            raise PermissionError("bound guarded dispatcher authority changed")
        if getattr(self._dispatch, "__func__", None) is not _CANONICAL_GUARDED_DISPATCH:
            raise PermissionError("bound guarded dispatcher executable changed")
        if self._dispatcher.store is not self._store:
            raise PermissionError("guarded dispatcher journal changed after composition")
        if (
            type(self._dispatcher.environment) is not str
            or self._dispatcher.environment != self._environment
        ):
            raise PermissionError("guarded dispatcher environment changed after composition")
        if (
            type(self._dispatcher.account_id) is not str
            or self._dispatcher.account_id != self._account_id
        ):
            raise PermissionError("guarded dispatcher account changed after composition")
        if (
            type(self._dispatcher.owner_token) is not str
            or self._dispatcher.owner_token != self._owner_id
        ):
            raise PermissionError("guarded dispatcher owner identity changed after composition")
        if (
            type(self._dispatcher.owner_epoch) is not int
            or self._dispatcher.owner_epoch != self._owner_epoch
        ):
            raise PermissionError("guarded dispatcher owner epoch changed after composition")
        if self._recovery_controller.owner is not self._owner:
            raise PermissionError("production recovery owner changed after composition")
        owner_scope = self._recovery_controller.owner_scope
        if type(owner_scope) is not str or owner_scope != self._owner_scope:
            raise PermissionError("production recovery scope changed after composition")
        if self._recovery_controller.durable_owner_store_path != self._store.path:
            raise PermissionError("production recovery journal changed after composition")

    def dispatch(self, **kwargs):
        if "sender_check" in kwargs:
            raise TypeError("sender_check is construction-bound by the production host")
        self._require_dispatch_authority()
        with self._condition:
            if not self._accepting:
                raise PermissionError("production host provider dispatch is closed")
            self._active += 1
        try:
            return self._dispatcher.dispatch(
                sender_check=self._sender_check,
                **kwargs,
            )
        finally:
            with self._condition:
                self._active -= 1
                if self._active == 0:
                    self._condition.notify_all()

    def stop_and_drain(self) -> None:
        with self._condition:
            self._accepting = False
            while self._active:
                self._condition.wait()


class ProductionFinancialHostRuntime:
    """Own financial runtime authority strictly inside one host lifetime."""

    def __init__(
        self,
        *,
        host: ProductionHostRuntime,
        recovery_controller: RecoveryController,
        owner: OwnerFence,
        provider_secret_resolver: HostLifetimeProviderSecretResolver,
        dispatcher: HostLifetimeGuardedDispatcher,
    ) -> None:
        if type(host) is not ProductionHostRuntime:
            raise TypeError("host must be exact ProductionHostRuntime")
        if type(recovery_controller) is not RecoveryController:
            raise TypeError("recovery_controller must be exact RecoveryController")
        if type(owner) is not OwnerFence:
            raise TypeError("owner must be exact OwnerFence")
        if type(provider_secret_resolver) is not HostLifetimeProviderSecretResolver:
            raise TypeError(
                "provider_secret_resolver must be exact HostLifetimeProviderSecretResolver"
            )
        if type(dispatcher) is not HostLifetimeGuardedDispatcher:
            raise TypeError("dispatcher must be exact HostLifetimeGuardedDispatcher")
        _require_owner(owner)
        config = host.config
        if type(config) is not ProductionHostConfig:
            raise TypeError("production host config must be exact ProductionHostConfig")
        if type(host.journal) is not JournalStore:
            raise TypeError("production host journal must be exact JournalStore")
        if recovery_controller.owner is not owner:
            raise RuntimeError("recovery owner is not current")
        if recovery_controller.durable_owner_store_path != host.journal.path:
            raise RuntimeError("recovery owner store is not the production journal")

        expected_scope = f"{config.environment}:{config.account_id}"
        if recovery_controller.owner_scope != expected_scope:
            raise RuntimeError("recovery owner scope is not the production account scope")

        self._host = host
        self._config = config
        self._journal = host.journal
        self._store_identity = host.store_identity
        self._recovery_controller = recovery_controller
        self._owner = owner
        self._owner_id = owner.owner_id
        self._owner_epoch = owner.epoch
        self._provider_secret_resolver = provider_secret_resolver
        self._dispatcher = dispatcher
        self._host_identity = (
            config.account_id,
            config.environment,
            config.host_id,
            config.public_origin,
        )

    def _require_host_authority(self) -> None:
        if type(self._host) is not ProductionHostRuntime:
            raise PermissionError("production host authority changed")
        if (
            self._host.config is not self._config
            or type(self._config) is not ProductionHostConfig
        ):
            raise PermissionError("production host config authority changed")
        current_identity = (
            self._config.account_id,
            self._config.environment,
            self._config.host_id,
            self._config.public_origin,
        )
        if (
            any(type(value) is not str for value in current_identity)
            or current_identity != self._host_identity
        ):
            raise PermissionError("production host identity changed after composition")
        if (
            self._host.journal is not self._journal
            or type(self._journal) is not JournalStore
        ):
            raise PermissionError("production host journal changed after composition")
        if (
            self._host.store_identity != self._store_identity
            or self._journal.store_identity != self._store_identity
        ):
            raise PermissionError("production host journal generation changed")
        if type(self._recovery_controller) is not RecoveryController:
            raise PermissionError("production recovery controller authority changed")
        if type(self._provider_secret_resolver) is not HostLifetimeProviderSecretResolver:
            raise PermissionError("production credential resolver authority changed")
        if type(self._dispatcher) is not HostLifetimeGuardedDispatcher:
            raise PermissionError("production financial dispatcher authority changed")
        _require_owner(
            self._owner,
            expected_id=self._owner_id,
            expected_epoch=self._owner_epoch,
        )
        if self._owner_id != self._host_identity[2]:
            raise PermissionError("production owner no longer matches host identity")

    @property
    def host(self) -> ProductionHostRuntime:
        self._require_host_authority()
        return self._host

    @property
    def recovery_controller(self) -> RecoveryController:
        self._require_host_authority()
        return self._recovery_controller

    @property
    def owner(self) -> OwnerFence:
        self._require_host_authority()
        return self._owner

    @property
    def provider_secret_resolver(self) -> HostLifetimeProviderSecretResolver:
        self._require_host_authority()
        return self._provider_secret_resolver

    @property
    def dispatcher(self) -> HostLifetimeGuardedDispatcher:
        self._require_host_authority()
        return self._dispatcher

    @property
    def config(self) -> ProductionHostConfig:
        self._require_host_authority()
        return self._config

    @property
    def journal(self) -> JournalStore:
        self._require_host_authority()
        return self._journal

    @property
    def closed(self) -> bool:
        self._require_host_authority()
        return self._host.closed

    @property
    def serving(self) -> bool:
        self._require_host_authority()
        return self._host.serving

    def serve_forever(self, *, poll_interval: float = 0.5) -> None:
        self._require_host_authority()
        self._host.serve_forever(poll_interval=poll_interval)

    def close(self) -> None:
        """Run the host terminal path; its bound finalizer owns financial teardown."""

        self._require_host_authority()
        self._host.close()

    def __enter__(self) -> "ProductionFinancialHostRuntime":
        self._require_host_authority()
        self._host.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.close()


def build_production_financial_host(
    config: ProductionHostConfig,
    *,
    security_boundary: SecurityBoundary,
    principal_resolver: PrincipalResolver,
    snapshot_provider: SnapshotProvider,
    tls_context: ssl.SSLContext | None = None,
    now: Callable[[], str] | None = None,
) -> ProductionFinancialHostRuntime:
    """Build the canonical host, then claim its first durable financial owner.

    The canonical host builder acquires the production ``ResourceLock`` before it
    returns.  Only then are the recovery controller and host-lifetime provider
    secret resolver admitted.  If durable ownership already exists, or any later
    composition step fails, the host is closed so the instance fence cannot leak.
    """

    if type(config) is not ProductionHostConfig:
        raise TypeError("config must be exact ProductionHostConfig")
    host = build_production_host(
        config,
        security_boundary=security_boundary,
        principal_resolver=principal_resolver,
        snapshot_provider=snapshot_provider,
        tls_context=tls_context,
        now=now,
    )
    try:
        if type(host) is not ProductionHostRuntime:
            raise TypeError("canonical host builder returned noncanonical runtime")
        if host.config is not config:
            raise PermissionError("production host config object changed during construction")
        host_config = host.config
        provider_secret_resolver = HostLifetimeProviderSecretResolver(
            security_boundary,
            account_id=host_config.account_id,
            environment=host_config.environment,
        )
        recovery = RecoveryController(
            owner_store=host.journal,
            owner_scope=f"{host_config.environment}:{host_config.account_id}",
        )
        core_dispatcher = GuardedDispatcher(
            host.journal,
            environment=host_config.environment,
            account_id=host_config.account_id,
            owner_token=host_config.host_id,
            owner_epoch=1,
        )
        dispatcher_holder: list[HostLifetimeGuardedDispatcher] = []

        def finalize_financial_authority() -> None:
            if dispatcher_holder:
                dispatcher_holder[0].stop_and_drain()
            provider_secret_resolver.stop_and_drain()
            recovery.stop()

        # Bind cleanup before any durable owner is minted. Every host teardown
        # path, including an unexpected serve failure, must run this finalizer
        # successfully before listener/fence release.
        host.bind_terminal_finalizer(finalize_financial_authority)
        owner = recovery.start(host_config.host_id)
        dispatcher = HostLifetimeGuardedDispatcher(
            core_dispatcher,
            recovery_controller=recovery,
            owner=owner,
        )
        dispatcher_holder.append(dispatcher)
        return ProductionFinancialHostRuntime(
            host=host,
            recovery_controller=recovery,
            owner=owner,
            provider_secret_resolver=provider_secret_resolver,
            dispatcher=dispatcher,
        )
    except BaseException:
        host.close()
        raise
