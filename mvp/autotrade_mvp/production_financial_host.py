"""Financial-authority composition inside the canonical production-host lifetime.

This module does not create a second host, journal, server, recovery engine or
process lock.  It composes the existing ``build_production_host`` lifecycle with
the existing durable ``RecoveryController`` so recovery-owner authority is
created only after the canonical production-host instance fence is held and is
stopped before that host can release the fence.

The first durable owner is intentionally the only automatic path here.  A
journal that already has a recovery owner fails closed and must use the canonical
issued takeover protocol before a successor can become owner.
"""

from __future__ import annotations

import ssl
from typing import Callable

from .host_network import PrincipalResolver, SnapshotProvider
from .production_host import (
    ProductionHostConfig,
    ProductionHostRuntime,
    build_production_host,
)
from .recovery import OwnerFence, RecoveryController
from .security import SecurityBoundary


class ProductionFinancialHostRuntime:
    """Own recovery authority strictly inside one production-host lifetime."""

    def __init__(
        self,
        *,
        host: ProductionHostRuntime,
        recovery_controller: RecoveryController,
        owner: OwnerFence,
    ) -> None:
        if not isinstance(host, ProductionHostRuntime):
            raise TypeError("host must be ProductionHostRuntime")
        if not isinstance(recovery_controller, RecoveryController):
            raise TypeError("recovery_controller must be RecoveryController")
        if not isinstance(owner, OwnerFence):
            raise TypeError("owner must be OwnerFence")
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
        """Drop in-process recovery authority before releasing the host fence."""

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

        if recovery_error is not None:
            if host_error is not None:
                recovery_error.add_note(
                    f"production host teardown also failed: {host_error!r}"
                )
            raise recovery_error
        if host_error is not None:
            raise host_error

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
    """Build the canonical host, then claim its first durable recovery owner.

    The canonical host builder acquires the production ``ResourceLock`` before it
    returns.  Only then is the recovery controller constructed on that exact
    journal and ``start`` allowed to append owner epoch 1.  If durable ownership
    already exists, or any later composition step fails, the host is closed so
    the instance fence cannot leak.
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
        return ProductionFinancialHostRuntime(
            host=host,
            recovery_controller=recovery,
            owner=owner,
        )
    except BaseException:
        host.close()
        raise
