"""Production lifecycle composition for the canonical authenticated AutoTrade host.

This module owns process composition only. It deliberately reuses JournalStore,
AuthenticatedHostApplication, AuthenticatedHostServer and SecurityBoundary and does
not create a second journal, API server, authentication authority or trading path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
import ssl
from threading import Condition, RLock, Thread, current_thread
from typing import Callable
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary

from research.autotrade_research.artifacts.resource_lock import (
    ResourceLock,
    ResourceLockBusyError,
)
from research.autotrade_research.io.strict_json import strict_json_loads

from .host_network import (
    AuthenticatedHostApplication,
    AuthenticatedHostServer,
    PrincipalResolver,
    SnapshotProvider,
    TransportResponse,
)
from .persistence import (
    JournalStore,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)
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
_RUNTIME_OCCURRENCE_SCHEMA_VERSION = "1.0.0"
_RUNTIME_OCCURRENCE_AGGREGATE_TYPE = "production_host_runtime"
_RUNTIME_OCCURRENCE_EVENT_TYPE = "ProductionHostRuntimeOccurrenceIssued"


def _runtime_occurrence_binding_authority():
    """Create one process-private runtime-object occurrence selector authority."""

    bindings = WeakKeyDictionary()
    lock = RLock()

    def bind(runtime: object, occurrence_id: str) -> None:
        if type(occurrence_id) is not str or not occurrence_id:
            raise TypeError("runtime occurrence binding id must be exact non-empty str")
        with lock:
            if runtime in bindings:
                raise RuntimeError("production host runtime occurrence is already bound")
            bindings[runtime] = occurrence_id

    def read(runtime: object) -> str | None:
        with lock:
            return bindings.get(runtime)

    return bind, read


_RUNTIME_OCCURRENCE_BIND, _RUNTIME_OCCURRENCE_READ = (
    _runtime_occurrence_binding_authority()
)

_RUNTIME_OCCURRENCE_PAYLOAD_FIELDS = frozenset(
    {
        "account_id",
        "environment",
        "host_id",
        "runtime_occurrence_id",
        "schema_version",
    }
)


def _product_artifact_root(journal_path: Path) -> Path:
    """Select the product artifact root independently from caller publication stores."""

    if not isinstance(journal_path, Path) or not journal_path.is_absolute():
        raise ValueError("product artifact root requires canonical absolute journal path")
    return Path(str(journal_path) + ".artifacts")
_RUNTIME_CONFIG_BINDINGS = WeakKeyDictionary()
_RUNTIME_CONFIG_BINDINGS_LOCK = RLock()
_RUNTIME_ISSUANCE_TOKEN = object()


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


def _readmit_production_host_config(
    config: ProductionHostConfig,
) -> ProductionHostConfig:
    """Detach one exact canonical config snapshot before host side effects.

    frozen dataclasses prevent ordinary assignment but are not an authority
    boundary: object.__setattr__ can still replace fields after initial
    construction. Reject executable scalar/path subclasses before invoking
    normalization, URL parsing, comparison, hashing, or filesystem code.
    """

    if type(config) is not ProductionHostConfig:
        raise TypeError("config must be exact ProductionHostConfig")
    source_state = vars(config)
    if type(source_state) is not dict:
        raise TypeError("production host config state must be an exact dict")
    state = dict.copy(source_state)
    if any(type(key) is not str for key in state):
        raise TypeError("production host config field names must be exact strings")
    if set(state) != _CONFIG_FIELDS:
        raise ValueError("production host config state is not canonical")

    journal_path = state["journal_path"]
    if type(journal_path) is not type(Path()):
        raise TypeError("production host journal_path must be an exact platform Path")
    for field in (
        "account_id",
        "environment",
        "host_id",
        "bind_host",
        "public_origin",
    ):
        if type(state[field]) is not str:
            raise TypeError(
                f"production host config field {field} must be exact text"
            )
    if type(state["bind_port"]) is not int:
        raise TypeError(
            "production host config field bind_port must be an exact integer"
        )

    return ProductionHostConfig(
        journal_path=journal_path,
        account_id=state["account_id"],
        environment=state["environment"],
        host_id=state["host_id"],
        bind_host=state["bind_host"],
        bind_port=state["bind_port"],
        public_origin=state["public_origin"],
    )


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


@dataclass(frozen=True, slots=True)
class ProductionHostRuntimeOccurrence:
    """One durable product-host process/bootstrap occurrence.

    This is an occurrence selector only. Journal committed_at is storage metadata,
    not independent UTC chronology authority, and this object grants no readiness,
    provider, release, or trading authority.
    """

    runtime_occurrence_id: str
    host_id: str
    account_id: str
    environment: str
    aggregate_version: int
    journal_sequence: int

    def __post_init__(self) -> None:
        for field in (
            "runtime_occurrence_id",
            "host_id",
            "account_id",
            "environment",
        ):
            value = getattr(self, field)
            if type(value) is not str or not value or value != value.strip():
                raise TypeError(f"{field} must be exact canonical non-empty str")
        try:
            parsed = UUID(self.runtime_occurrence_id)
        except (ValueError, AttributeError, TypeError) as error:
            raise ValueError("runtime_occurrence_id must be a canonical UUID") from error
        if str(parsed) != self.runtime_occurrence_id:
            raise ValueError("runtime_occurrence_id must be a canonical lowercase UUID")
        for field in ("aggregate_version", "journal_sequence"):
            value = getattr(self, field)
            if type(value) is not int or value <= 0:
                raise TypeError(f"{field} must be a positive exact int")


def _runtime_occurrence_from_scoped_event(
    event: object,
    *,
    host_id: str,
    account_id: str,
    environment: str,
    expected_version: int,
) -> ProductionHostRuntimeOccurrence:
    if type(event) is not dict:
        raise RuntimeError("production host runtime occurrence row is non-canonical")
    if event.get("event_type") != _RUNTIME_OCCURRENCE_EVENT_TYPE:
        raise RuntimeError("production host runtime occurrence event type is invalid")
    if event.get("aggregate_type") != _RUNTIME_OCCURRENCE_AGGREGATE_TYPE:
        raise RuntimeError("production host runtime occurrence aggregate type is invalid")
    if event.get("aggregate_id") != host_id:
        raise RuntimeError("production host runtime occurrence host identity is invalid")
    if event.get("aggregate_version") != expected_version:
        raise RuntimeError("production host runtime occurrence version chain is invalid")
    journal_sequence = event.get("journal_sequence")
    if type(journal_sequence) is not int or journal_sequence <= 0:
        raise RuntimeError("production host runtime occurrence journal sequence is invalid")
    payload = event.get("payload")
    if type(payload) is not dict or frozenset(payload) != _RUNTIME_OCCURRENCE_PAYLOAD_FIELDS:
        raise RuntimeError("production host runtime occurrence payload is non-canonical")
    if payload.get("schema_version") != _RUNTIME_OCCURRENCE_SCHEMA_VERSION:
        raise RuntimeError("production host runtime occurrence schema version is invalid")
    for field, expected in (
        ("host_id", host_id),
        ("account_id", account_id),
        ("environment", environment),
    ):
        value = payload.get(field)
        if type(value) is not str or value != expected:
            raise RuntimeError(
                f"production host runtime occurrence {field} changed for installed host"
            )
    occurrence_id = payload.get("runtime_occurrence_id")
    if type(occurrence_id) is not str:
        raise RuntimeError("production host runtime occurrence id is non-canonical")
    if event.get("event_id") != occurrence_id:
        raise RuntimeError("production host runtime occurrence event/id binding is invalid")
    try:
        return ProductionHostRuntimeOccurrence(
            runtime_occurrence_id=occurrence_id,
            host_id=host_id,
            account_id=account_id,
            environment=environment,
            aggregate_version=expected_version,
            journal_sequence=journal_sequence,
        )
    except (TypeError, ValueError) as error:
        raise RuntimeError("production host runtime occurrence payload is invalid") from error


def _runtime_occurrence_from_event(
    event: object,
    *,
    config: ProductionHostConfig,
    expected_version: int,
) -> ProductionHostRuntimeOccurrence:
    return _runtime_occurrence_from_scoped_event(
        event,
        host_id=config.host_id,
        account_id=config.account_id,
        environment=config.environment,
        expected_version=expected_version,
    )

def _load_production_host_runtime_occurrences(
    journal: JournalStore,
    config: ProductionHostConfig,
) -> tuple[ProductionHostRuntimeOccurrence, ...]:
    config = _readmit_production_host_config(config)
    identity = require_exact_journal_store_authority(
        journal,
        subject="production host runtime occurrence JournalStore",
    )
    with journal_store_authority_scope(journal, identity):
        events = JournalStore.load_events(
            journal,
            _RUNTIME_OCCURRENCE_AGGREGATE_TYPE,
            config.host_id,
        )
    occurrences: list[ProductionHostRuntimeOccurrence] = []
    seen: set[str] = set()
    previous_sequence = 0
    for expected_version, event in enumerate(events, start=1):
        occurrence = _runtime_occurrence_from_event(
            event,
            config=config,
            expected_version=expected_version,
        )
        if occurrence.runtime_occurrence_id in seen:
            raise RuntimeError("production host runtime occurrence id is duplicated")
        if occurrence.journal_sequence <= previous_sequence:
            raise RuntimeError(
                "production host runtime occurrence journal order is invalid"
            )
        seen.add(occurrence.runtime_occurrence_id)
        previous_sequence = occurrence.journal_sequence
        occurrences.append(occurrence)
    return tuple(occurrences)



def require_current_production_host_runtime_occurrence(
    *,
    journal: JournalStore,
    occurrence: ProductionHostRuntimeOccurrence,
) -> ProductionHostRuntimeOccurrence:
    """Return a detached exact snapshot only if occurrence is current in journal.

    This proves durable runtime-occurrence identity and ordering only. It does not
    prove UTC chronology, release authenticity, readiness, or trading authority.
    """

    if type(occurrence) is not ProductionHostRuntimeOccurrence:
        raise TypeError(
            "occurrence must be exact ProductionHostRuntimeOccurrence"
        )
    snapshot = ProductionHostRuntimeOccurrence(
        runtime_occurrence_id=occurrence.runtime_occurrence_id,
        host_id=occurrence.host_id,
        account_id=occurrence.account_id,
        environment=occurrence.environment,
        aggregate_version=occurrence.aggregate_version,
        journal_sequence=occurrence.journal_sequence,
    )
    identity = require_exact_journal_store_authority(
        journal,
        subject="production host runtime occurrence JournalStore",
    )
    with journal_store_authority_scope(journal, identity):
        events = JournalStore.load_events(
            journal,
            _RUNTIME_OCCURRENCE_AGGREGATE_TYPE,
            snapshot.host_id,
        )

    if not events:
        raise PermissionError("production host runtime occurrence is not durable")
    seen: set[str] = set()
    previous_sequence = 0
    current: ProductionHostRuntimeOccurrence | None = None
    for expected_version, event in enumerate(events, start=1):
        item = _runtime_occurrence_from_scoped_event(
            event,
            host_id=snapshot.host_id,
            account_id=snapshot.account_id,
            environment=snapshot.environment,
            expected_version=expected_version,
        )
        if item.runtime_occurrence_id in seen:
            raise RuntimeError("production host runtime occurrence id is duplicated")
        if item.journal_sequence <= previous_sequence:
            raise RuntimeError(
                "production host runtime occurrence journal order is invalid"
            )
        seen.add(item.runtime_occurrence_id)
        previous_sequence = item.journal_sequence
        current = item

    if current != snapshot:
        raise PermissionError(
            "production host runtime occurrence is no longer current"
        )
    return snapshot

def _issue_production_host_runtime_occurrence(
    journal: JournalStore,
    config: ProductionHostConfig,
) -> ProductionHostRuntimeOccurrence:
    """Append one durable restart-distinguishing host occurrence.

    The timestamp required by the generic journal envelope remains storage
    metadata only. Independent UTC chronology is owned by WP-48/#1018.
    """

    config = _readmit_production_host_config(config)
    identity = require_exact_journal_store_authority(
        journal,
        subject="production host runtime occurrence JournalStore",
    )
    with journal_store_authority_scope(journal, identity):
        existing_events = JournalStore.load_events(
            journal,
            _RUNTIME_OCCURRENCE_AGGREGATE_TYPE,
            config.host_id,
        )
        existing = tuple(
            _runtime_occurrence_from_event(
                event,
                config=config,
                expected_version=index,
            )
            for index, event in enumerate(existing_events, start=1)
        )
        if len({item.runtime_occurrence_id for item in existing}) != len(existing):
            raise RuntimeError("production host runtime occurrence id is duplicated")
        occurrence_id = str(uuid4())
        payload = {
            "account_id": config.account_id,
            "environment": config.environment,
            "host_id": config.host_id,
            "runtime_occurrence_id": occurrence_id,
            "schema_version": _RUNTIME_OCCURRENCE_SCHEMA_VERSION,
        }
        next_version = len(existing) + 1
        JournalStore.append_event(
            journal,
            {
                "event_id": occurrence_id,
                "event_type": _RUNTIME_OCCURRENCE_EVENT_TYPE,
                "aggregate_type": _RUNTIME_OCCURRENCE_AGGREGATE_TYPE,
                "aggregate_id": config.host_id,
                "aggregate_version": str(next_version),
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "committed_at": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                ),
            },
        )
        final_events = JournalStore.load_events(
            journal,
            _RUNTIME_OCCURRENCE_AGGREGATE_TYPE,
            config.host_id,
        )
        if len(final_events) != next_version:
            raise RuntimeError(
                "production host runtime occurrence chain changed during issuance"
            )
        issued = _runtime_occurrence_from_event(
            final_events[-1],
            config=config,
            expected_version=next_version,
        )
        if issued.runtime_occurrence_id != occurrence_id:
            raise RuntimeError(
                "production host runtime occurrence round-trip identity mismatch"
            )
        return issued

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
        issuance_token: object | None = None,
    ) -> None:
        if issuance_token is not _RUNTIME_ISSUANCE_TOKEN:
            raise PermissionError(
                "ProductionHostRuntime must be issued by canonical host composition"
            )
        canonical_config = _readmit_production_host_config(config)
        with _RUNTIME_CONFIG_BINDINGS_LOCK:
            if self in _RUNTIME_CONFIG_BINDINGS:
                raise RuntimeError("production host config is already bound")
            _RUNTIME_CONFIG_BINDINGS[self] = canonical_config
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
        self._terminal_finalizer: Callable[[], None] | None = None

    @property
    def config(self) -> ProductionHostConfig:
        with _RUNTIME_CONFIG_BINDINGS_LOCK:
            bound = _RUNTIME_CONFIG_BINDINGS.get(self)
        if type(bound) is not ProductionHostConfig:
            raise RuntimeError("production host config authority is not bound")
        return _readmit_production_host_config(bound)

    def _bind_runtime_occurrence(
        self,
        occurrence: ProductionHostRuntimeOccurrence,
        *,
        _bind=_RUNTIME_OCCURRENCE_BIND,
    ) -> None:
        if type(occurrence) is not ProductionHostRuntimeOccurrence:
            raise TypeError(
                "runtime occurrence must be exact ProductionHostRuntimeOccurrence"
            )
        # Keep the selector outside both caller-writable runtime instance state
        # and replaceable module-global container state. The captured closure
        # owns the one-shot runtime-object binding for this process.
        _bind(self, occurrence.runtime_occurrence_id)

    @property

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

    def bind_terminal_finalizer(self, finalizer: Callable[[], None]) -> None:
        """Bind one authority finalizer that must succeed before fence release."""

        if not callable(finalizer):
            raise TypeError("terminal finalizer must be callable")
        with self._lifecycle_condition:
            if self._serve_state != "IDLE":
                raise RuntimeError(
                    "terminal finalizer must be bound before serving or shutdown"
                )
            if self._terminal_finalizer is not None:
                raise RuntimeError("terminal finalizer is already bound")
            self._terminal_finalizer = finalizer

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
            stage = "terminal authority finalizer"
            finalizer = self._terminal_finalizer
            if finalizer is not None:
                finalizer()
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
    application_factory: Callable[..., AuthenticatedHostApplication] | None = None,
) -> ProductionHostRuntime:
    """Compose the existing durable host authorities into one runnable process seam."""

    config = _readmit_production_host_config(config)
    # A provider-free local simulation may supply a specialized product UI
    # dispatcher, but no alternative Host authority for PAPER or LIVE exists.
    if application_factory is not None:
        if config.environment != "SIMULATION" or config.account_id != "canonical-sim-account":
            raise PermissionError("custom host application is restricted to ZERO simulation")
        if not callable(application_factory):
            raise TypeError("application_factory must be callable")
    if type(security_boundary) is not SecurityBoundary:
        raise TypeError("security_boundary must be exact SecurityBoundary")
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
    server: AuthenticatedHostServer | None = None
    try:
        journal = JournalStore(config.journal_path)
        factory = AuthenticatedHostApplication if application_factory is None else application_factory
        application = factory(
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
        if not isinstance(application, AuthenticatedHostApplication):
            raise TypeError("host application must retain authenticated host authority")
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
        runtime = ProductionHostRuntime(
            config=config,
            journal=journal,
            application=application,
            server=server,
            instance_fence=instance_fence,
            admission_gate=admission_gate,
            issuance_token=_RUNTIME_ISSUANCE_TOKEN,
        )
        runtime_occurrence = _issue_production_host_runtime_occurrence(
            journal,
            config,
        )
        runtime._bind_runtime_occurrence(runtime_occurrence)
        return runtime
    except BaseException as error:
        cleanup_errors: list[tuple[str, BaseException]] = []
        if server is not None:
            try:
                server.server_close()
            except BaseException as cleanup_error:
                cleanup_errors.append(("listener close", cleanup_error))
        try:
            instance_fence.release()
        except BaseException as cleanup_error:
            cleanup_errors.append(("instance fence release", cleanup_error))
        for stage, cleanup_error in cleanup_errors:
            error.add_note(
                f"production host bootstrap cleanup failed during {stage}: "
                f"{cleanup_error!r}"
            )
        raise
