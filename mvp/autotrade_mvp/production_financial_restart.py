"""Explicit same-owner restart for the fenced production financial host.

A process restart is not a recovery-owner takeover.  This module therefore does
not mint a new owner epoch, revoke credentials, or claim cross-host fencing.
It reacquires the canonical production-host instance fence first, then permits
attachment only when the current durable recovery owner is the configured host
identity and no takeover freeze is pending.

The resumed controller is always RECOVERING, reconstructs durable ambiguous
submission state, and requires fresh owner-bound provider reconciliation before
the existing guarded dispatcher can cross its final sender barrier.
"""

from __future__ import annotations

import ssl
from typing import Callable

from .dispatch import GuardedDispatcher
from .host_network import PrincipalResolver, SnapshotProvider
from .production_financial_host import (
    HostLifetimeGuardedDispatcher,
    HostLifetimeProviderSecretResolver,
    ProductionFinancialHostRuntime,
)
from .production_host import ProductionHostConfig, build_production_host
from .recovery import HostState, RecoveryController
from .security import SecurityBoundary
from .sender_authority import sender_authority_window


def resume_production_financial_host(
    config: ProductionHostConfig,
    *,
    security_boundary: SecurityBoundary,
    principal_resolver: PrincipalResolver,
    snapshot_provider: SnapshotProvider,
    tls_context: ssl.SSLContext | None = None,
    now: Callable[[], str] | None = None,
) -> ProductionFinancialHostRuntime:
    """Resume the same durable owner inside a newly acquired local host fence.

    This is deliberately narrower than takeover:
    - an empty owner journal must use the first-owner builder;
    - a different current owner must use the explicit durable takeover issuer;
    - a pending takeover must be resumed/finished by that issuer;
    - no durable owner event is appended here.
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
        owner_chain = recovery.durable_owner_chain()
        if not owner_chain:
            raise PermissionError(
                "same-owner restart requires an existing durable owner; use first-owner builder"
            )
        owner = owner_chain[-1]
        if owner.owner_id != config.host_id:
            raise PermissionError(
                "durable owner belongs to a different host; explicit takeover is required"
            )

        dispatcher_holder: list[HostLifetimeGuardedDispatcher] = []

        def finalize_financial_authority() -> None:
            if dispatcher_holder:
                dispatcher_holder[0].stop_and_drain()
            provider_secret_resolver.stop_and_drain()
            recovery.stop()

        # Bind cleanup before this process attaches any in-memory sender owner.
        # If the sender gate reports a pending takeover or recovery reconstruction
        # fails, host.close() runs this finalizer before releasing the host fence.
        host.bind_terminal_finalizer(finalize_financial_authority)

        owner_scope = f"{config.environment}:{config.account_id}"
        with sender_authority_window(host.journal, owner_scope=owner_scope):
            # Re-read under the shared sender gate so a concurrent takeover or
            # owner commit cannot be hidden by the pre-gate owner snapshot.
            current = recovery.durable_owner_chain()
            if not current or current[-1] != owner:
                raise PermissionError(
                    "durable recovery owner changed during restart attachment"
                )
            recovery.owner = owner
            recovery.state = HostState.RECOVERING
            recovery.provider_reconciled = False
            recovery.reason_codes = {"startup_reconciliation_required"}
            recovery._recover_scoped_submission_uncertainty_from_owner_scope()

        core_dispatcher = GuardedDispatcher(
            host.journal,
            environment=config.environment,
            account_id=config.account_id,
            owner_token=owner.owner_id,
            owner_epoch=owner.epoch,
        )
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
