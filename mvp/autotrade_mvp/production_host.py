"""Production lifecycle composition for the canonical authenticated AutoTrade host.

This module owns process composition only. It deliberately reuses JournalStore,
AuthenticatedHostApplication, AuthenticatedHostServer and SecurityBoundary and does
not create a second journal, API server, authentication authority or trading path.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from pathlib import Path
import ssl
from threading import Condition, Thread, current_thread
from typing import Callable
from urllib.parse import urlsplit

from autotrade_runtime.resource_lock import (
    ResourceLock,
    ResourceLockBusyError,
)
from autotrade_runtime.strict_json import strict_json_loads

from .host_network import (
    AuthenticatedHostApplication,
    AuthenticatedHostServer,
    PrincipalResolver,
    SnapshotProvider,
    TransportResponse,
)
from .persistence import JournalStore
from .store_identity import JournalStoreIdentity, require_database_identity
from .security import SecurityBoundary, _authenticated_origin


_ALLOWED_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})
_COMMAND_PATH = "/api/v1/commands"
_HEALTH_PATH = "/api/v1/health"
_SHUTTING_DOWN_BODY = b'{"error":"HOST_SHUTTING_DOWN"}'
_MAX_SERVE_POLL_SECONDS = 0.5
_MAX_CONFIG_BYTES = 64 * 1024
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
_TERMINAL_STATES = frozenset({"CLOSED", "FAILED"})
_STOPPING_STATES = frozenset({"CLOSING", "CLOSED", "FAILED"})


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
    if len(payload) > _MAX_CONFIG_BYTES:
        raise ValueError("production host config exceeds maximum size")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("production host config must be UTF-8 JSON") from error
    try:
        parsed = strict_json_loads(text)
    except ValueError as error:
        raise ValueError(f"production host config is not valid strict JSON: {error}") from error
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
    """Read one bounded config payload from an explicit absolute config path."""

    config_path = Path(path)
    if not config_path.is_absolute():
        raise ValueError("production host config path must be absolute")
    canonical_path = config_path.resolve(strict=True)
    if not canonical_path.is_file():
        raise ValueError("production host config path must reference a file")
    with canonical_path.open("rb") as handle:
        payload = handle.read(_MAX_CONFIG_BYTES + 1)
    if len(payload) > _MAX_CONFIG_BYTES:
        raise ValueError("production host config exceeds maximum size")
    return parse_production_host_config(payload)


class _InstanceFence:
    """Process-lifetime bootstrap exclusion using the shared ResourceLock TCB.

    This lock is coordination only. Physical financial-store authority remains
    JournalStore.store_identity and is checked separately on every host dispatch.
    """

    def __init__(self, lock: ResourceLock) -> None:
        self.path = lock.path
        self._lock = lock
        self._release_condition = Condition()
        self._release_state = "ACTIVE"
        self._release_error: BaseException | None = None

    @property
    def released(self) -> bool:
        with self._release_condition:
            return self._release_state == "RELEASED"

    @classmethod
    def acquire(cls, journal_path: Path) -> "_InstanceFence":
        if not journal_path.is_absolute():
            raise ValueError("instance-fence journal path must be absolute")
        lock = ResourceLock(
            Path(str(journal_path) + ".host.lock"),
            blocking=False,
        )
        try:
            lock.acquire()
        except ResourceLockBusyError as exc:
            raise RuntimeError(
                "production host instance is already owned"
            ) from exc
        return cls(lock)

    def release(self) -> None:
        with self._release_condition:
            while self._release_state == "RELEASING":
                self._release_condition.wait()
            if self._release_state == "RELEASED":
                return
            if self._release_state == "FAILED":
                assert self._release_error is not None
                raise self._release_error
            self._release_state = "RELEASING"

        try:
            self._lock.release()
        except BaseException as exc:
            with self._release_condition:
                self._release_error = exc
                self._release_state = "FAILED"
                self._release_condition.notify_all()
            raise

        with self._release_condition:
            self._release_state = "RELEASED"
            self._release_condition.notify_all()


class _StoreIdentityGate:
    """Bind every host response to the retained physical journal generation."""

    def __init__(
        self,
        application: AuthenticatedHostApplication,
        journal: JournalStore,
    ) -> None:
        self._dispatch = application.dispatch
        self._journal_path = journal.path
        self.store_identity: JournalStoreIdentity = journal.store_identity

    def dispatch(
        self,
        *,
        method: str,
        target: str,
        headers,
        body: bytes = b"",
    ) -> TransportResponse:
        require_database_identity(self._journal_path, self.store_identity)
        response = self._dispatch(
            method=method,
            target=target,
            headers=headers,
            body=body,
        )
        require_database_identity(self._journal_path, self.store_identity)
        return response


class _CommandAdmissionGate:
    """Stop new durable command admission and drain admitted dispatches."""

    def __init__(self, application: AuthenticatedHostApplication) -> None:
        self._dispatch = application.dispatch
        self._condition = Condition()
        self._accepting = True
        self._active = 0

    @staticmethod
    def _shutting_down_response() -> TransportResponse:
        return TransportResponse(
            status=503,
            content_type="application/json; charset=utf-8",
            body=_SHUTTING_DOWN_BODY,
            headers=(("Cache-Control", "no-store"),),
        )

    def dispatch(
        self,
        *,
        method: str,
        target: str,
        headers,
        body: bytes = b"",
    ) -> TransportResponse:
        path = urlsplit(target).path
        is_command = method == "POST" and path == _COMMAND_PATH
        is_health = method == "GET" and path == _HEALTH_PATH
        if not is_command:
            response = self._dispatch(
                method=method,
                target=target,
                headers=headers,
                body=body,
            )
            if not is_health or response.status != 200:
                return response
            with self._condition:
                if self._accepting:
                    return response
            return self._shutting_down_response()
        with self._condition:
            if not self._accepting:
                return self._shutting_down_response()
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

    def stop_accepting(self) -> None:
        """Commit the shutdown admission cut without waiting for active commands."""

        with self._condition:
            self._accepting = False

    def stop_and_drain(self) -> None:
        with self._condition:
            self._accepting = False
            while self._active:
                self._condition.wait()


class ProductionHostRuntime:
    """Own one canonical host listener and instance fence through terminal shutdown."""

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
        self.store_identity: JournalStoreIdentity = journal.store_identity
        self.application = application
        self.server = server
        self._instance_fence = instance_fence
        self._admission_gate = admission_gate
        self._lifecycle_condition = Condition()
        self._serve_state = "IDLE"
        self._serve_thread: Thread | None = None
        self._serve_error: BaseException | None = None
        self._terminal_error: BaseException | None = None
        self._teardown_owner: Thread | None = None
        self._serve_entry_hook: Callable[[], None] = lambda: None
        self._serve_loop_entry_hook: Callable[[], None] = lambda: None

    @property
    def closed(self) -> bool:
        with self._lifecycle_condition:
            return self._serve_state == "CLOSED"

    @property
    def shutdown_requested(self) -> bool:
        with self._lifecycle_condition:
            return self._serve_state in _STOPPING_STATES

    @property
    def serving(self) -> bool:
        with self._lifecycle_condition:
            return self._serve_state == "SERVING"

    def _raise_terminal_failure(self) -> None:
        assert self._terminal_error is not None
        raise self._terminal_error

    def _run_server(self, poll_interval: float) -> None:
        try:
            self._serve_entry_hook()
            with self._lifecycle_condition:
                if self._serve_state in _STOPPING_STATES:
                    return
                if self._serve_state != "STARTING":
                    raise RuntimeError("production host runtime startup state corrupted")
                self._serve_state = "ENTERING"
                self._lifecycle_condition.notify_all()
            self._serve_loop_entry_hook()
            with self._lifecycle_condition:
                if self._serve_state in _STOPPING_STATES:
                    return
                if self._serve_state != "ENTERING":
                    raise RuntimeError("production host runtime entry state corrupted")
                self.server.timeout = min(poll_interval, _MAX_SERVE_POLL_SECONDS)
                self._serve_state = "SERVING"
                self._lifecycle_condition.notify_all()

            while True:
                with self._lifecycle_condition:
                    if self._serve_state in _STOPPING_STATES:
                        return
                self.server.handle_request()
                self.server.service_actions()
        except BaseException as exc:
            with self._lifecycle_condition:
                if self._serve_error is None:
                    self._serve_error = exc
                if self._serve_state not in _TERMINAL_STATES:
                    self._admission_gate.stop_accepting()
                    self._serve_state = "CLOSING"
                self._lifecycle_condition.notify_all()
        finally:
            with self._lifecycle_condition:
                self._lifecycle_condition.notify_all()

    def _terminal_teardown(self, cause: BaseException | None = None) -> None:
        owner = current_thread()
        with self._lifecycle_condition:
            if cause is not None and self._serve_error is None:
                self._serve_error = cause
            while True:
                if self._serve_state == "CLOSED":
                    return
                if self._serve_state == "FAILED":
                    self._raise_terminal_failure()
                if self._serve_state == "CLOSING":
                    if self._teardown_owner is None:
                        self._teardown_owner = owner
                        break
                    if self._teardown_owner is owner:
                        break
                    self._lifecycle_condition.wait()
                    continue
                self._admission_gate.stop_accepting()
                self._serve_state = "CLOSING"
                self._teardown_owner = owner
                self._lifecycle_condition.notify_all()
                break
            worker = self._serve_thread

        stage = "command admission drain"
        cleanup_error: BaseException | None = None
        try:
            self._admission_gate.stop_and_drain()
            stage = "serve worker join"
            if worker is not None and worker is not owner:
                worker.join()
            with self._lifecycle_condition:
                terminal_cause = self._serve_error
            stage = "listener close"
            self.server.server_close()
            stage = "instance fence release"
            self._instance_fence.release()
        except BaseException as exc:
            cleanup_error = exc
            with self._lifecycle_condition:
                terminal_cause = self._serve_error

        terminal_error = terminal_cause
        if cleanup_error is not None:
            if terminal_error is None:
                terminal_error = cleanup_error
            else:
                terminal_error.add_note(
                    f"terminal teardown also failed during {stage}: {cleanup_error!r}"
                )

        with self._lifecycle_condition:
            if terminal_error is None:
                self._serve_state = "CLOSED"
                self._terminal_error = None
            else:
                self._serve_state = "FAILED"
                self._terminal_error = terminal_error
            self._teardown_owner = None
            self._lifecycle_condition.notify_all()

        if terminal_error is not None:
            raise terminal_error

    def serve_forever(self, *, poll_interval: float = 0.5) -> None:
        if (
            isinstance(poll_interval, bool)
            or not isinstance(poll_interval, (int, float))
        ):
            raise TypeError("poll_interval must be a finite positive number")
        poll_interval = float(poll_interval)
        if not isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("poll_interval must be a finite positive number")

        with self._lifecycle_condition:
            if self._serve_state == "CLOSED":
                raise RuntimeError("production host runtime is closed")
            if self._serve_state == "FAILED":
                self._raise_terminal_failure()
            if self._serve_state != "IDLE":
                raise RuntimeError("production host runtime is already serving or closing")
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

        with self._lifecycle_condition:
            error = self._serve_error
            state = self._serve_state
        if error is not None:
            self._terminal_teardown(error)
        if state in _STOPPING_STATES:
            self._terminal_teardown()
            return
        self._terminal_teardown(
            RuntimeError("production host serve worker exited without terminal shutdown")
        )

    def close(self) -> None:
        self._terminal_teardown()

    def __enter__(self) -> "ProductionHostRuntime":
        with self._lifecycle_condition:
            if self._serve_state in _STOPPING_STATES:
                if self._serve_state == "FAILED":
                    self._raise_terminal_failure()
                raise RuntimeError("production host runtime is closing or closed")
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
        identity_gate = _StoreIdentityGate(application, journal)
        admission_gate = _CommandAdmissionGate(identity_gate)
        application.dispatch = admission_gate.dispatch  # type: ignore[method-assign]
        server = AuthenticatedHostServer(
            (config.bind_host, config.bind_port),
            application,
            tls_context=tls_context,
        )
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
