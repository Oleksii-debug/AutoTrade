"""Production PAPER/LIVE composition for host lifetime and sender authority.

This module is deliberately a narrow composition seam.  It reuses the exact
``ProductionHostRuntime``/``JournalStore`` created by :mod:`production_host` and
binds that same store to the durable recovery owner and
``RecoveryBoundDispatcher``.  It does not create another journal, provider path,
or authority model.

The process-instance fence remains the outer lifetime fence.  Recovery sender
authority is revoked before that fence is released, including terminal teardown
initiated internally by ``ProductionHostRuntime.serve_forever``.
"""

from __future__ import annotations

from dataclasses import dataclass
import ssl
from typing import Callable

from .host_network import PrincipalResolver, SnapshotProvider
from .production_host import (
    ProductionHostConfig,
    ProductionHostRuntime,
    build_production_host,
)
from .recovery import HostState, OwnerFence, RecoveryController
from .recovery_dispatch import RecoveryBoundDispatcher
from .recovery_takeover import execute_durable_takeover
from .security import SecurityBoundary
from .windows_secrets import PersistentCredentialHandle, ProtectedCredentialVault


@dataclass(frozen=True)
class DurableTakeoverInputs:
    """Existing-owner proof inputs required for a clean process restart."""

    vault: ProtectedCredentialVault
    handle: PersistentCredentialHandle
    execution_identity: str
    reconciliation_id: str
    provider_id: str


def _stage_durable_source_for_immediate_takeover(
    controller: RecoveryController,
    source: OwnerFence,
) -> None:
    """Rehydrate a durable source only inside a non-escaping takeover build.

    There is intentionally no public "resume old sender" operation.  The staged
    controller is never returned or bound to a dispatcher: the builder proceeds
    directly into ``execute_durable_takeover`` and stops the controller on every
    failure path.  This closes the clean-restart gap without reviving prior send
    authority as a product capability.
    """

    controller.owner = source
    controller.provider_reconciled = False
    controller.reason_codes = {"startup_reconciliation_required"}
    controller.state = HostState.RECOVERING


class ProductionTradingHostRuntime:
    """One production host plus its exact recovery-bound outbound authority."""

    __slots__ = ("host", "recovery", "dispatcher")

    def __init__(
        self,
        host: ProductionHostRuntime,
        *,
        recovery: RecoveryController | None,
        dispatcher: RecoveryBoundDispatcher | None,
    ) -> None:
        self.host = host
        self.recovery = recovery
        self.dispatcher = dispatcher

    @property
    def config(self) -> ProductionHostConfig:
        return self.host.config

    @property
    def journal(self):
        return self.host.journal

    @property
    def store_identity(self):
        return self.host.store_identity

    @property
    def application(self):
        return self.host.application

    @property
    def server(self):
        return self.host.server

    @property
    def closed(self) -> bool:
        return self.host.closed

    @property
    def shutdown_requested(self) -> bool:
        return self.host.shutdown_requested

    @property
    def serving(self) -> bool:
        return self.host.serving

    def serve_forever(self, *, poll_interval: float = 0.5) -> None:
        self.host.serve_forever(poll_interval=poll_interval)

    def close(self) -> None:
        self.host.close()

    def __enter__(self) -> "ProductionTradingHostRuntime":
        self.host.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.host.__exit__(exc_type, exc, traceback)


def build_production_trading_host(
    config: ProductionHostConfig,
    *,
    security_boundary: SecurityBoundary,
    principal_resolver: PrincipalResolver,
    snapshot_provider: SnapshotProvider,
    recovery_owner_id: str,
    takeover: DurableTakeoverInputs | None = None,
    prepared_lease_seconds: int = 60,
    tls_context: ssl.SSLContext | None = None,
    now: Callable[[], str] | None = None,
) -> ProductionTradingHostRuntime:
    """Bind production host lifetime to durable PAPER/LIVE sender ownership.

    Ordering is fail closed:

    1. acquire the canonical process fence and construct its exact JournalStore;
    2. construct recovery on that same JournalStore and canonical account scope;
    3. start a first owner, or perform explicit durable takeover of an existing
       owner before exposing any dispatcher;
    4. bind ``RecoveryBoundDispatcher`` to that exact controller/store pair;
    5. install recovery stop immediately before process-fence release.

    REPLAY/SIMULATION intentionally expose no recovery-bound external sender.
    """

    if not isinstance(config, ProductionHostConfig):
        raise TypeError("config must be ProductionHostConfig")
    if not isinstance(recovery_owner_id, str) or not recovery_owner_id.strip():
        raise ValueError("recovery_owner_id is required")
    normalized_owner = recovery_owner_id.strip()
    if (
        isinstance(prepared_lease_seconds, bool)
        or not isinstance(prepared_lease_seconds, int)
        or prepared_lease_seconds <= 0
    ):
        raise ValueError("prepared_lease_seconds must be a positive integer")

    host = build_production_host(
        config,
        security_boundary=security_boundary,
        principal_resolver=principal_resolver,
        snapshot_provider=snapshot_provider,
        tls_context=tls_context,
        now=now,
    )

    if config.environment not in {"PAPER", "LIVE"}:
        if takeover is not None:
            host.close()
            raise ValueError("durable takeover is only valid for PAPER/LIVE")
        return ProductionTradingHostRuntime(
            host,
            recovery=None,
            dispatcher=None,
        )

    controller: RecoveryController | None = None
    try:
        owner_scope = f"{config.environment}:{config.account_id}"
        controller = RecoveryController(
            owner_store=host.journal,
            owner_scope=owner_scope,
        )
        durable_chain = controller.durable_owner_chain()
        if durable_chain:
            if takeover is None:
                raise PermissionError(
                    "existing durable recovery owner requires explicit takeover inputs"
                )
            latest_durable = durable_chain[-1]
            # The latest durable owner may already be the requested target when
            # a prior process died after RecoveryOwnerChanged but before the
            # takeover completion event.  The canonical takeover engine owns the
            # distinction between a resumable pending transition and an invalid
            # same-owner request; do not pre-reject that crash-resume state here.
            _stage_durable_source_for_immediate_takeover(controller, latest_durable)
            execute_durable_takeover(
                controller,
                new_owner_id=normalized_owner,
                vault=takeover.vault,
                handle=takeover.handle,
                execution_identity=takeover.execution_identity,
                reconciliation_id=takeover.reconciliation_id,
                provider_id=takeover.provider_id,
            )
            owner = controller.owner
            durable_after = controller.durable_owner_chain()
            if (
                owner is None
                or not durable_after
                or owner != durable_after[-1]
                or owner.owner_id != normalized_owner
                or controller.state is not HostState.RECOVERING
            ):
                raise RuntimeError(
                    "durable takeover did not establish the expected recovering owner"
                )
        else:
            if takeover is not None:
                raise ValueError(
                    "takeover inputs require an existing durable recovery owner"
                )
            controller.start(normalized_owner)

        dispatcher = RecoveryBoundDispatcher(
            controller,
            host.journal,
            environment=config.environment,
            account_id=config.account_id,
            prepared_lease_seconds=prepared_lease_seconds,
        )

        # The canonical host owns terminal teardown. Revoke sender authority
        # after command drain/join but before listener close or fence release.
        host.bind_terminal_finalizer(controller.stop)
        return ProductionTradingHostRuntime(
            host,
            recovery=controller,
            dispatcher=dispatcher,
        )
    except BaseException:
        if controller is not None:
            controller.stop()
        host.close()
        raise
