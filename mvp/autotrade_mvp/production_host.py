"""Production lifecycle composition for the canonical authenticated AutoTrade host.

This module owns process composition only. It deliberately reuses JournalStore,
AuthenticatedHostApplication, AuthenticatedHostServer and SecurityBoundary and does
not create a second journal, API server, authentication authority or trading path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import ssl
from threading import Lock
from typing import Callable
from urllib.parse import urlsplit

from .host_network import (
    AuthenticatedHostApplication,
    AuthenticatedHostServer,
    PrincipalResolver,
    SnapshotProvider,
)
from .persistence import JournalStore
from .security import SecurityBoundary, _authenticated_origin


_ALLOWED_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})


@dataclass(frozen=True)
class ProductionHostConfig:
    """Immutable identity and listener configuration for one installed host."""

    journal_path: str | Path
    account_id: str
    environment: str
    host_id: str
    bind_host: str
    bind_port: int
    public_origin: str

    def __post_init__(self) -> None:
        journal_path = Path(self.journal_path)
        if not journal_path.is_absolute():
            raise ValueError("production journal_path must be absolute")
        if not self.account_id or self.account_id != self.account_id.strip():
            raise ValueError("account_id must be canonical non-empty text")
        if self.environment not in _ALLOWED_ENVIRONMENTS:
            raise ValueError("environment is not a canonical AutoTrade environment")
        if not self.host_id or self.host_id != self.host_id.strip():
            raise ValueError("host_id must be canonical non-empty text")
        if not self.bind_host or self.bind_host != self.bind_host.strip():
            raise ValueError("bind_host must be canonical non-empty text")
        if isinstance(self.bind_port, bool) or not isinstance(self.bind_port, int):
            raise TypeError("bind_port must be an integer")
        if self.bind_port <= 0 or self.bind_port > 65535:
            raise ValueError("bind_port is outside the TCP port range")

        canonical_origin = _authenticated_origin(self.public_origin)
        parsed = urlsplit(canonical_origin)
        origin_host = parsed.hostname
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if origin_host is None:
            raise ValueError("public_origin host is required")
        if origin_host.lower() != self.bind_host.lower():
            raise ValueError("public_origin host must match bind_host")
        if origin_port != self.bind_port:
            raise ValueError("public_origin port must match bind_port")

        object.__setattr__(self, "journal_path", journal_path)
        object.__setattr__(self, "public_origin", canonical_origin)


class ProductionHostRuntime:
    """Own one canonical host listener from construction through shutdown."""

    def __init__(
        self,
        *,
        config: ProductionHostConfig,
        journal: JournalStore,
        application: AuthenticatedHostApplication,
        server: AuthenticatedHostServer,
    ) -> None:
        self.config = config
        self.journal = journal
        self.application = application
        self.server = server
        self._closed = False
        self._serving = False
        self._lifecycle_lock = Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def serving(self) -> bool:
        return self._serving

    def serve_forever(self, *, poll_interval: float = 0.5) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("production host runtime is closed")
            if self._serving:
                raise RuntimeError("production host runtime is already serving")
            self._serving = True
        try:
            self.server.serve_forever(poll_interval=poll_interval)
        finally:
            with self._lifecycle_lock:
                self._serving = False

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            serving = self._serving
        try:
            # BaseServer.shutdown() waits for serve_forever(). Calling it before
            # serving can block indefinitely, so pre-serve teardown closes the
            # listener directly while an active server is asked to stop first.
            if serving:
                self.server.shutdown()
        finally:
            self.server.server_close()

    def __enter__(self) -> "ProductionHostRuntime":
        if self._closed:
            raise RuntimeError("production host runtime is closed")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.close()


def build_production_host(
    config: ProductionHostConfig,
    *,
    security_boundary: SecurityBoundary,
    principal_resolver: PrincipalResolver,
    snapshot_provider: SnapshotProvider,
    tls_context: ssl.SSLContext | None = None,
    now: Callable[[], str] | None = None,
) -> ProductionHostRuntime:
    """Compose the existing durable host authorities into one runnable process seam.

    Authentication/session authority is injected. This function never creates a
    session, credential vault, financial authority or provider sender.
    """

    if not isinstance(config, ProductionHostConfig):
        raise TypeError("config must be ProductionHostConfig")
    if not isinstance(security_boundary, SecurityBoundary):
        raise TypeError("security_boundary must be SecurityBoundary")
    if not callable(principal_resolver):
        raise TypeError("principal_resolver must be callable")
    if not callable(snapshot_provider):
        raise TypeError("snapshot_provider must be callable")

    scheme = urlsplit(config.public_origin).scheme
    if tls_context is None and scheme != "http":
        raise ValueError("HTTPS public_origin requires tls_context")
    if tls_context is not None and scheme != "https":
        raise ValueError("TLS listener requires HTTPS public_origin")

    journal = JournalStore(config.journal_path)
    application = AuthenticatedHostApplication(
        journal,
        security_boundary=security_boundary,
        account_id=config.account_id,
        environment=config.environment,
        host_id=config.host_id,
        public_origin=config.public_origin,
        principal_resolver=principal_resolver,
        snapshot_provider=snapshot_provider,
        now=now,
    )
    server = AuthenticatedHostServer(
        (config.bind_host, config.bind_port),
        application,
        tls_context=tls_context,
    )
    return ProductionHostRuntime(
        config=config,
        journal=journal,
        application=application,
        server=server,
    )
