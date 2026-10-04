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
from .production_host import (
    ProductionHostConfig,
    ProductionHostRuntime,
    build_production_host,
)
from .recovery import OwnerFence, RecoveryController
from .security import SecurityBoundary


# Capture the canonical class implementation once at module import.  The
# production runtime exposes its RecoveryController for reconciliation and
# takeover orchestration, so resolving ``instance.validate_sender`` on every
# dispatch would let later instance-attribute replacement retarget the final
# sender authority callback.  This is trusted-process composition provenance,
# not a Python sandbox; the point is that callers cannot replace the callback
# selected by this product composition seam.
_CANONICAL_VALIDATE_SENDER = RecoveryController.validate_sender


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
        if type(account_id) is not str or not account_id or account_id != account_id.strip():
            raise ValueError("provider credential account_id must be canonical text")
        if (
            type(environment) is not str
            or environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}
        ):
            raise ValueError("provider credential environment is not canonical")
        self._security_boundary = security_boundary
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
        with self._condition:
            if not self._accepting:
                raise PermissionError("production host provider credential leases are closed")
            self._active += 1

        try:
            with self._security_boundary.lease_for_execution(
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
        if not isinstance(dispatcher, GuardedDispatcher):
            raise TypeError("dispatcher must be GuardedDispatcher")
        if not isinstance(recovery_controller, RecoveryController):
            raise TypeError("recovery_controller must be RecoveryController")
        if not isinstance(owner, OwnerFence):
            raise TypeError("owner must be OwnerFence")
        if recovery_controller.owner != owner:
            raise RuntimeError("dispatcher recovery owner is not current")
        expected_scope = f"{dispatcher.environment}:{dispatcher.account_id}"
        if recovery_controller.owner_scope != expected_scope:
            raise RuntimeError("dispatcher recovery scope does not match provider scope")
        if recovery_controller.durable_owner_store_path != dispatcher.store.path:
            raise RuntimeError("dispatcher journal is not the durable recovery journal")
        if dispatcher.owner_token != owner.owner_id or dispatcher.owner_epoch != owner.epoch:
            raise RuntimeError("dispatcher sender identity does not match recovery owner")

        self._dispatcher = dispatcher
        self._recovery_controller = recovery_controller
        self._sender_check = _CANONICAL_VALIDATE_SENDER.__get__(
            recovery_controller,
            RecoveryController,
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
        return self._recovery_controller.owner  # type: ignore[return-value]

    def dispatch(self, **kwargs):
        if "sender_check" in kwargs:
            raise TypeError("sender_check is construction-bound by the production host")
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
        if not isinstance(host, ProductionHostRuntime):
            raise TypeError("host must be ProductionHostRuntime")
        if not isinstance(recovery_controller, RecoveryController):
            raise TypeError("recovery_controller must be RecoveryController")
        if not isinstance(owner, OwnerFence):
            raise TypeError("owner must be OwnerFence")
        if not isinstance(provider_secret_resolver, HostLifetimeProviderSecretResolver):
            raise TypeError(
                "provider_secret_resolver must be HostLifetimeProviderSecretResolver"
            )
        if not isinstance(dispatcher, HostLifetimeGuardedDispatcher):
            raise TypeError("dispatcher must be HostLifetimeGuardedDispatcher")
        if recovery_controller.owner != owner:
            raise RuntimeError("recovery owner is not current")
        if recovery_controller.durable_owner_store_path != host.journal.path:
            raise RuntimeError("recovery owner store is not the production journal")

        expected_scope = f"{host.config.environment}:{host.config.account_id}"
        if recovery_controller.owner_scope != expected_scope:
            raise RuntimeError("recovery owner scope is not the production account scope")

        self.host = host
        self.recovery_controller = recovery_controller
        self.owner = owner
        self.provider_secret_resolver = provider_secret_resolver
        self.dispatcher = dispatcher

    @property
    def config(self) -> ProductionHostConfig:
        return self.host.config

    @property
    def journal(self):
        return self.host.journal

    @property
    def closed(self) -> bool:
        return self.host.closed

    @property
    def serving(self) -> bool:
        return self.host.serving

    def serve_forever(self, *, poll_interval: float = 0.5) -> None:
        self.host.serve_forever(poll_interval=poll_interval)

    def close(self) -> None:
        """Run the host terminal path; its bound finalizer owns financial teardown."""

        self.host.close()

    def __enter__(self) -> "ProductionFinancialHostRuntime":
        self.host.__enter__()
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

    host = build_production_host(
        config,
        security_boundary=security_boundary,
        principal_resolver=principal_resolver,
        snapshot_provider=snapshot_provider,
        tls_context=tls_context,
        now=now,
    )
    try:
        provider_secret_resolver = HostLifetimeProviderSecretResolver(
            security_boundary,
            account_id=config.account_id,
            environment=config.environment,
        )
        recovery = RecoveryController(
            owner_store=host.journal,
            owner_scope=f"{config.environment}:{config.account_id}",
        )
        core_dispatcher = GuardedDispatcher(
            host.journal,
            environment=config.environment,
            account_id=config.account_id,
            owner_token=config.host_id,
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
        owner = recovery.start(config.host_id)
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
