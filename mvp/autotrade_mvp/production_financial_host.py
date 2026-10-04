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

from .host_network import PrincipalResolver, SnapshotProvider
from .production_host import (
    ProductionHostConfig,
    ProductionHostRuntime,
    build_production_host,
)
from .recovery import OwnerFence, RecoveryController
from .security import SecurityBoundary


class HostLifetimeProviderSecretResolver:
    """Canonical provider-secret lease seam bounded by one host lifetime.

    Provider transports receive this resolver instead of a raw SecurityBoundary.
    New leases are rejected after shutdown begins, and shutdown waits for every
    admitted lease to leave its underlying vault context before the production
    host can release its process-lifetime instance fence.
    """

    def __init__(self, security_boundary: SecurityBoundary) -> None:
        lease = getattr(security_boundary, "lease_for_execution", None)
        if not callable(lease):
            raise TypeError("security_boundary must provide lease_for_execution")
        self._security_boundary = security_boundary
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
        account_id: str,
        provider: str,
        environment: str,
        purpose: str,
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
                account_id=account_id,
                provider=provider,
                environment=environment,
                purpose=purpose,
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


class ProductionFinancialHostRuntime:
    """Own financial runtime authority strictly inside one host lifetime."""

    def __init__(
        self,
        *,
        host: ProductionHostRuntime,
        recovery_controller: RecoveryController,
        owner: OwnerFence,
        provider_secret_resolver: HostLifetimeProviderSecretResolver,
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
        """Drain provider secrets, drop recovery owner, then release host fence."""

        provider_error: BaseException | None = None
        try:
            self.provider_secret_resolver.stop_and_drain()
        except BaseException as error:
            provider_error = error

        recovery_error: BaseException | None = None
        try:
            self.recovery_controller.stop()
        except BaseException as error:
            recovery_error = error

        host_error: BaseException | None = None
        try:
            self.host.close()
        except BaseException as error:
            host_error = error

        terminal_error = provider_error or recovery_error or host_error
        if terminal_error is None:
            return
        for label, error in (
            ("provider lease drain", provider_error),
            ("recovery stop", recovery_error),
            ("production host teardown", host_error),
        ):
            if error is not None and error is not terminal_error:
                terminal_error.add_note(f"{label} also failed: {error!r}")
        raise terminal_error

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
        recovery = RecoveryController(
            owner_store=host.journal,
            owner_scope=f"{config.environment}:{config.account_id}",
        )
        owner = recovery.start(config.host_id)
        provider_secret_resolver = HostLifetimeProviderSecretResolver(security_boundary)
        return ProductionFinancialHostRuntime(
            host=host,
            recovery_controller=recovery,
            owner=owner,
            provider_secret_resolver=provider_secret_resolver,
        )
    except BaseException:
        host.close()
        raise
