"""Production lifecycle composition for the canonical authenticated AutoTrade host.

This module owns process composition only. It deliberately reuses JournalStore,
AuthenticatedHostApplication, AuthenticatedHostServer and SecurityBoundary and does
not create a second journal, API server, authentication authority or trading path.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import ssl
from threading import Condition, Lock, Thread
from typing import BinaryIO, Callable
from urllib.parse import urlsplit

from .host_network import (
    AuthenticatedHostApplication,
    AuthenticatedHostServer,
    PrincipalResolver,
    SnapshotProvider,
    TransportResponse,
)
from .persistence import JournalStore
from .security import SecurityBoundary, _authenticated_origin


_ALLOWED_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})
_COMMAND_PATH = "/api/v1/commands"
_SHUTTING_DOWN_BODY = b'{"error":"HOST_SHUTTING_DOWN"}'
_CONFIG_FIELDS = frozenset(
    {
        "journal_path",
        "account_id",
        "environment",
        "host_id",
        "bind_host",
        "bind_port",
        "public_origin",
    }
)


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
        journal_path = journal_path.resolve(strict=False)
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
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if parsed.hostname is None:
            raise ValueError("public_origin host is required")
        if origin_port != self.bind_port:
            raise ValueError("public_origin port must match bind_port")

        object.__setattr__(self, "journal_path", journal_path)
        object.__setattr__(self, "public_origin", canonical_origin)


def _strict_json_object(payload: bytes) -> dict[str, object]:
    if not isinstance(payload, bytes):
        raise TypeError("production host config payload must be bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("production host config must be UTF-8 JSON") from error

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate production host config field: {key}")
            result[key] = value
        return result

    try:
        parsed = json.loads(text, object_pairs_hook=object_pairs)
    except json.JSONDecodeError as error:
        raise ValueError("production host config is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError("production host config must be one JSON object")
    return parsed


def parse_production_host_config(payload: bytes) -> ProductionHostConfig:
    """Parse one immutable config snapshot without startup defaults or retargeting.

    This is structural configuration validation only. It does not authenticate the
    configuration, resolve credentials, or grant provider/financial authority.
    """

    parsed = _strict_json_object(payload)
    actual = frozenset(parsed)
    missing = _CONFIG_FIELDS - actual
    unknown = actual - _CONFIG_FIELDS
    if missing:
        raise ValueError(
            "production host config is missing fields: " + ", ".join(sorted(missing))
        )
    if unknown:
        raise ValueError(
            "production host config has unknown fields: " + ", ".join(sorted(unknown))
        )

    for field in (
        "journal_path",
        "account_id",
        "environment",
        "host_id",
        "bind_host",
        "public_origin",
    ):
        if not isinstance(parsed[field], str):
            raise TypeError(f"production host config field {field} must be text")
    bind_port = parsed["bind_port"]
    if isinstance(bind_port, bool) or not isinstance(bind_port, int):
        raise TypeError("production host config field bind_port must be an integer")

    return ProductionHostConfig(
        journal_path=parsed["journal_path"],
        account_id=parsed["account_id"],
        environment=parsed["environment"],
        host_id=parsed["host_id"],
        bind_host=parsed["bind_host"],
        bind_port=bind_port,
        public_origin=parsed["public_origin"],
    )


def load_production_host_config(path: str | Path) -> ProductionHostConfig:
    """Read exactly one config payload from an explicit absolute config path."""

    config_path = Path(path)
    if not config_path.is_absolute():
        raise ValueError("production host config path must be absolute")
    canonical_path = config_path.resolve(strict=True)
    if not canonical_path.is_file():
        raise ValueError("production host config path must reference a file")
    payload = canonical_path.read_bytes()
    return parse_production_host_config(payload)


class _InstanceFence:
    """Process-lifetime exclusive ownership of one durable host journal."""

    def __init__(self, *, path: Path, handle: BinaryIO) -> None:
        self.path = path
        self._handle = handle
        self._released = False
        self._release_lock = Lock()

    @classmethod
    def acquire(cls, journal_path: Path) -> "_InstanceFence":
        if not journal_path.is_absolute():
            raise ValueError("instance-fence journal path must be absolute")
        fence_path = Path(str(journal_path) + ".host.lock")
        fence_path.parent.mkdir(parents=True, exist_ok=True)
        handle = fence_path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise RuntimeError(
                        "production host instance is already owned"
                    ) from exc
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise RuntimeError(
                        "production host instance is already owned"
                    ) from exc
        except BaseException:
            handle.close()
            raise
        return cls(path=fence_path, handle=handle)

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            try:
                if os.name == "nt":
                    import msvcrt

                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._released = True
                self._handle.close()


class _CommandAdmissionGate:
    """Stop new durable command admission and drain admitted dispatches."""

    def __init__(self, application: AuthenticatedHostApplication) -> None:
        self._dispatch = application.dispatch
        self._condition = Condition()
        self._accepting = True
        self._active = 0

    def dispatch(
        self,
        *,
        method: str,
        target: str,
        headers,
        body: bytes = b"",
    ) -> TransportResponse:
        is_command = method == "POST" and urlsplit(target).path == _COMMAND_PATH
        if not is_command:
            return self._dispatch(
                method=method,
                target=target,
                headers=headers,
                body=body,
            )
        with self._condition:
            if not self._accepting:
                return TransportResponse(
                    status=503,
                    content_type="application/json; charset=utf-8",
                    body=_SHUTTING_DOWN_BODY,
                    headers=(("Cache-Control", "no-store"),),
                )
            self._active += 1
        try:
            return self._dispatch(
                method=method,
                target=target,
                headers=headers,
                body=body,
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


class ProductionHostRuntime:
    """Own one canonical host listener and instance fence through shutdown."""

    def __init__(
        self,
        *,
        config: ProductionHostConfig,
        journal: JournalStore,
        application: AuthenticatedHostApplication,
        server: AuthenticatedHostServer,
        instance_fence: _InstanceFence,
        admission_gate: _CommandAdmissionGate,
    ) -> None:
        self.config = config
        self.journal = journal
        self.application = application
        self.server = server
        self._instance_fence = instance_fence
        self._admission_gate = admission_gate
        self._closed = False
        self._serve_state = "IDLE"
        self._serve_thread: Thread | None = None
        self._serve_error: BaseException | None = None
        self._lifecycle_lock = Lock()
        # A deliberately tiny test seam for deterministic STARTING/close races.
        self._serve_entry_hook: Callable[[], None] = lambda: None

    @property
    def closed(self) -> bool:
        with self._lifecycle_lock:
            return self._closed

    @property
    def serving(self) -> bool:
        with self._lifecycle_lock:
            return self._serve_state == "SERVING"

    def _run_server(self, poll_interval: float) -> None:
        try:
            self._serve_entry_hook()
            with self._lifecycle_lock:
                if self._closed:
                    return
                self._serve_state = "SERVING"
            self.server.serve_forever(poll_interval=poll_interval)
        except BaseException as exc:
            self._serve_error = exc
        finally:
            with self._lifecycle_lock:
                self._serve_state = "IDLE"

    def serve_forever(self, *, poll_interval: float = 0.5) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("production host runtime is closed")
            if self._serve_state != "IDLE":
                raise RuntimeError("production host runtime is already serving")
            self._serve_state = "STARTING"
            self._serve_error = None
            worker = Thread(
                target=self._run_server,
                args=(poll_interval,),
                name="autotrade-production-host",
                daemon=False,
            )
            self._serve_thread = worker
            worker.start()
        worker.join()
        error = self._serve_error
        if error is not None:
            raise error

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            state = self._serve_state
            worker = self._serve_thread

        # Atomically stop new durable command admissions first and allow any command
        # already admitted at that boundary to finish its durable dispatch.
        self._admission_gate.stop_and_drain()

        try:
            if state == "SERVING":
                # SERVING is published only by the dedicated worker after the
                # deterministic STARTING cancellation point. shutdown() therefore
                # cannot race a deliberately paused pre-entry transition.
                self.server.shutdown()
                if worker is not None:
                    worker.join()
            # STARTING is cancelled by _run_server's closed recheck. Do not call
            # BaseServer.shutdown() there: it can block before serve_forever enters.
        finally:
            try:
                self.server.server_close()
            finally:
                # AuthenticatedHostServer is configured with non-daemon request
                # workers below, so server_close() joins any handler that had already
                # been accepted, including post-ACCEPTED authority completion.
                self._instance_fence.release()

    def __enter__(self) -> "ProductionHostRuntime":
        if self.closed:
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
    """Compose the existing durable host authorities into one runnable process seam."""

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

    instance_fence = _InstanceFence.acquire(config.journal_path)
    try:
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
        admission_gate = _CommandAdmissionGate(application)
        application.dispatch = admission_gate.dispatch  # type: ignore[method-assign]
        server = AuthenticatedHostServer(
            (config.bind_host, config.bind_port),
            application,
            tls_context=tls_context,
        )
        # ThreadingMixIn otherwise leaves daemon handlers alive after server_close().
        # Production shutdown must join handlers through the post-ACCEPTED boundary.
        server.daemon_threads = False
        server.block_on_close = True
    except BaseException:
        instance_fence.release()
        raise
    return ProductionHostRuntime(
        config=config,
        journal=journal,
        application=application,
        server=server,
        instance_fence=instance_fence,
        admission_gate=admission_gate,
    )