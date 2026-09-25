"""Durable provider-private-stream lifecycle and freshness coordinator.

This module is intentionally provider-neutral.  It does not open sockets, parse
provider payloads, own trading authority, or mutate financial projections.
Provider adapters own authentication/subscription/parser rules and feed exact
immutable event evidence here.  Existing authenticated-read transports own
snapshot/backfill I/O.  This coordinator owns only connection generation,
sequence continuity and whether stream evidence is safe to treat as current.

The lifecycle is journal-backed so disconnects/gaps are durable, while process
restart is deliberately fail-closed: a newly constructed object never treats a
pre-crash synchronized stream as current until a new generation completes
provider-backed recovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Mapping
from uuid import NAMESPACE_URL, uuid5

from .persistence import JournalStore, canonical_json, payload_digest
from .provider_core import (
    ProviderCoreError,
    ProviderPrivateStreamObservation,
    ProviderResponseObservation,
)
from .reconciliation_journal import require_current_reconciliation_checkpoint


_AGGREGATE_TYPE = "provider_private_stream"
_EVENT_TYPE = "ProviderPrivateStreamLifecycleCommitted"
_COMMAND_ACTOR = "autotrade-provider-stream-lifecycle"
_ENVIRONMENTS = frozenset({"PAPER", "LIVE"})
_STATUSES = frozenset({"DISCONNECTED", "RECOVERING", "LIVE", "GAPPED"})
_SEQUENCE_RE = re.compile(r"^(0|[1-9][0-9]*)$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVIDENCE_RE = re.compile(r"^[a-z0-9._-]+:sha256:[0-9a-f]{64}$")
_AUTHENTICATED_READ_EVIDENCE_RE = re.compile(
    r"^provider-read:sha256:[0-9a-f]{64}$"
)


class ProviderStreamConflict(ValueError):
    """Raised when stream history/scope cannot be reconciled safely."""


class ProviderStreamNotReady(RuntimeError):
    """Raised when a caller attempts to rely on stale stream evidence."""


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ProviderStreamConflict(f"{name} must be canonical non-empty text")
    return value


def _environment(value: object) -> str:
    environment = _text(value, name="environment").upper()
    if environment not in _ENVIRONMENTS:
        raise ProviderStreamConflict(
            "private provider stream environment must be PAPER or LIVE"
        )
    return environment


def _sequence(value: object, *, name: str) -> int:
    if not isinstance(value, str) or _SEQUENCE_RE.fullmatch(value) is None:
        raise ProviderStreamConflict(
            f"{name} must be a canonical non-negative integer sequence string"
        )
    return int(value)


def _sequence_text(value: int) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderStreamConflict("sequence must be a non-negative integer")
    return str(value)


def _digest(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _DIGEST_RE.fullmatch(text) is None:
        raise ProviderStreamConflict(f"{name} must be a canonical SHA-256 digest")
    return text


def _evidence_ref(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _EVIDENCE_RE.fullmatch(text) is None:
        raise ProviderStreamConflict(
            f"{name} must bind an immutable sha256 evidence reference"
        )
    return text


def _authenticated_read_evidence_ref(value: object) -> str:
    text = _evidence_ref(value, name="recovery_evidence_ref")
    if _AUTHENTICATED_READ_EVIDENCE_RE.fullmatch(text) is None:
        raise ProviderStreamConflict(
            "recovery_evidence_ref must come from authenticated provider-read observation"
        )
    return text


def _instant_text(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProviderStreamConflict(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or not text.endswith("Z"):
        raise ProviderStreamConflict(f"{name} must be canonical UTC text")
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != text:
        raise ProviderStreamConflict(f"{name} must be canonical UTC text")
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _scope_identity(parts: list[str]) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            "https://provider-stream.autotrade.local/" + canonical_json(parts),
        )
    )


@dataclass(frozen=True)
class ProviderPrivateStreamScope:
    """Immutable provider/account/environment identity for one private stream."""

    provider_id: str
    account_id: str
    environment: str
    provider_environment: str
    stream_name: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_id",
            _text(self.provider_id, name="provider_id").upper(),
        )
        object.__setattr__(
            self,
            "account_id",
            _text(self.account_id, name="account_id"),
        )
        object.__setattr__(self, "environment", _environment(self.environment))
        object.__setattr__(
            self,
            "provider_environment",
            _text(self.provider_environment, name="provider_environment").upper(),
        )
        object.__setattr__(
            self,
            "stream_name",
            _text(self.stream_name, name="stream_name"),
        )

    @property
    def aggregate_id(self) -> str:
        return _scope_identity(
            [
                "scope",
                self.provider_id,
                self.account_id,
                self.environment,
                self.provider_environment,
                self.stream_name,
            ]
        )

    def as_payload(self) -> dict[str, str]:
        return {
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "provider_environment": self.provider_environment,
            "stream_name": self.stream_name,
        }


@dataclass(frozen=True)
class ProviderPrivateStreamEvent:
    """Exact immutable identity of one provider-stream delivery.

    Payload bytes remain in the provider evidence store / normalizer.  The
    lifecycle retains only immutable identity and digest so it cannot become a
    second provider-data authority.
    """

    provider_event_id: str
    sequence: str
    evidence_ref: str
    payload_sha256: str
    observed_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_event_id",
            _text(self.provider_event_id, name="provider_event_id"),
        )
        _sequence(self.sequence, name="sequence")
        object.__setattr__(
            self,
            "evidence_ref",
            _evidence_ref(self.evidence_ref, name="evidence_ref"),
        )
        object.__setattr__(
            self,
            "payload_sha256",
            _digest(self.payload_sha256, name="payload_sha256"),
        )
        object.__setattr__(
            self,
            "observed_at",
            _instant_text(self.observed_at, name="observed_at"),
        )

    def as_payload(self) -> dict[str, str]:
        return {
            "provider_event_id": self.provider_event_id,
            "sequence": self.sequence,
            "evidence_ref": self.evidence_ref,
            "payload_sha256": self.payload_sha256,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class ProviderPrivateStreamSnapshot:
    scope: ProviderPrivateStreamScope
    generation: int
    connection_id: str | None
    status: str
    recovery_floor_sequence: str | None
    snapshot_sequence: str | None
    last_sequence: str | None
    last_reason: str
    recovery_evidence_refs: tuple[str, ...] = ()
    aggregate_version: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ProviderPrivateStreamScope):
            raise TypeError("scope must be ProviderPrivateStreamScope")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ProviderStreamConflict("generation must be a non-negative integer")
        if self.connection_id is not None:
            _text(self.connection_id, name="connection_id")
        if self.status not in _STATUSES:
            raise ProviderStreamConflict("unsupported private-stream status")
        for name, value in (
            ("recovery_floor_sequence", self.recovery_floor_sequence),
            ("snapshot_sequence", self.snapshot_sequence),
            ("last_sequence", self.last_sequence),
        ):
            if value is not None:
                _sequence(value, name=name)
        _text(self.last_reason, name="last_reason")
        for ref in self.recovery_evidence_refs:
            _evidence_ref(ref, name="recovery_evidence_ref")
        if (
            isinstance(self.aggregate_version, bool)
            or not isinstance(self.aggregate_version, int)
            or self.aggregate_version < 0
        ):
            raise ProviderStreamConflict(
                "aggregate_version must be a non-negative integer"
            )


@dataclass(frozen=True)
class ProviderPrivateStreamEventResult:
    snapshot: ProviderPrivateStreamSnapshot
    disposition: str
    committed: bool


@dataclass
class _Projection:
    scope: ProviderPrivateStreamScope
    generation: int = 0
    connection_id: str | None = None
    status: str = "DISCONNECTED"
    recovery_floor_sequence: int | None = None
    snapshot_sequence: int | None = None
    last_sequence: int | None = None
    last_reason: str = "UNINITIALIZED"
    recovery_evidence_refs: tuple[str, ...] = ()
    current_events: dict[int, ProviderPrivateStreamEvent] = field(default_factory=dict)
    buffered_events: dict[int, ProviderPrivateStreamEvent] = field(default_factory=dict)
    aggregate_version: int = 0

    def snapshot(self) -> ProviderPrivateStreamSnapshot:
        return ProviderPrivateStreamSnapshot(
            scope=self.scope,
            generation=self.generation,
            connection_id=self.connection_id,
            status=self.status,
            recovery_floor_sequence=(
                None
                if self.recovery_floor_sequence is None
                else _sequence_text(self.recovery_floor_sequence)
            ),
            snapshot_sequence=(
                None
                if self.snapshot_sequence is None
                else _sequence_text(self.snapshot_sequence)
            ),
            last_sequence=(
                None if self.last_sequence is None else _sequence_text(self.last_sequence)
            ),
            last_reason=self.last_reason,
            recovery_evidence_refs=self.recovery_evidence_refs,
            aggregate_version=self.aggregate_version,
        )


def _snapshot_payload(snapshot: ProviderPrivateStreamSnapshot) -> dict[str, object]:
    return {
        **snapshot.scope.as_payload(),
        "generation": snapshot.generation,
        "connection_id": snapshot.connection_id,
        "status": snapshot.status,
        "recovery_floor_sequence": snapshot.recovery_floor_sequence,
        "snapshot_sequence": snapshot.snapshot_sequence,
        "last_sequence": snapshot.last_sequence,
        "last_reason": snapshot.last_reason,
        "recovery_evidence_refs": list(snapshot.recovery_evidence_refs),
        "aggregate_version": snapshot.aggregate_version,
    }


def _event_from_payload(value: object) -> ProviderPrivateStreamEvent:
    if not isinstance(value, Mapping):
        raise ProviderStreamConflict("stream event must be an object")
    return ProviderPrivateStreamEvent(
        provider_event_id=value.get("provider_event_id"),
        sequence=value.get("sequence"),
        evidence_ref=value.get("evidence_ref"),
        payload_sha256=value.get("payload_sha256"),
        observed_at=value.get("observed_at"),
    )


def _event_from_observation(
    observation: ProviderPrivateStreamObservation,
    *,
    scope: ProviderPrivateStreamScope,
    connection_id: str,
) -> ProviderPrivateStreamEvent:
    if not isinstance(observation, ProviderPrivateStreamObservation):
        raise TypeError(
            "event observation must be ProviderPrivateStreamObservation"
        )
    try:
        observation.require_scope(
            provider_id=scope.provider_id,
            account_id=scope.account_id,
            environment=scope.environment,
            provider_environment=scope.provider_environment,
            stream_name=scope.stream_name,
            connection_id=connection_id,
        )
    except ProviderCoreError as error:
        raise ProviderStreamConflict(
            "private-stream observation scope or connection mismatch"
        ) from error
    return ProviderPrivateStreamEvent(
        provider_event_id=observation.provider_event_id,
        sequence=observation.sequence,
        evidence_ref=observation.evidence_ref,
        payload_sha256=observation.frame_sha256,
        observed_at=observation.observed_at,
    )


def _read_observation_request(
    observation: ProviderResponseObservation,
    *,
    scope: ProviderPrivateStreamScope,
) -> dict[str, str]:
    if not isinstance(observation, ProviderResponseObservation):
        raise TypeError(
            "read_observations must contain ProviderResponseObservation"
        )
    if (
        observation.provider_id != scope.provider_id
        or observation.account_id != scope.account_id
        or observation.environment != scope.environment
        or observation.provider_environment != scope.provider_environment
    ):
        raise ProviderStreamConflict(
            "authenticated-read observation scope differs from private stream"
        )
    return {
        "evidence_ref": _authenticated_read_evidence_ref(
            observation.evidence_ref
        ),
        "response_sha256": _digest(
            observation.response_sha256,
            name="response_sha256",
        ),
        "query_digest": _digest(
            observation.query_binding.query_digest,
            name="query_digest",
        ),
        "provider_environment": observation.provider_environment,
        "prepared_at": _instant_text(
            observation.query_binding.prepared_at,
            name="prepared_at",
        ),
        "observed_at": _instant_text(
            observation.observed_at,
            name="observed_at",
        ),
    }


class DurableProviderPrivateStreamLifecycle:
    """Journal-backed stream continuity/freshness authority.

    It owns no socket implementation and no financial facts.  A provider adapter
    starts a connection generation, records immutable stream event identities,
    then supplies provider-backed account snapshot/backfill evidence.  Only a
    contiguous current generation can become runtime-ready.
    """

    def __init__(self, store: JournalStore, *, scope: ProviderPrivateStreamScope):
        if not isinstance(store, JournalStore):
            raise TypeError("store must be JournalStore")
        if not isinstance(scope, ProviderPrivateStreamScope):
            raise TypeError("scope must be ProviderPrivateStreamScope")
        self.store = store
        self.scope = scope
        self._validated_generation: int | None = None
        self._projection, self._idempotency = self._replay(self._events())

    def _events(self) -> list[dict[str, object]]:
        return self.store.load_events(_AGGREGATE_TYPE, self.scope.aggregate_id)

    def _replay(
        self,
        events: list[dict[str, object]],
    ) -> tuple[_Projection, dict[str, tuple[str, dict[str, object]]]]:
        projection = _Projection(scope=self.scope)
        idempotency: dict[str, tuple[str, dict[str, object]]] = {}
        expected_version = 1
        for event in events:
            if event.get("aggregate_version") != expected_version:
                raise ProviderStreamConflict(
                    "private-stream aggregate versions are not contiguous"
                )
            expected_version += 1
            if event.get("event_type") != _EVENT_TYPE:
                raise ProviderStreamConflict(
                    "private-stream journal contains an unsupported event type"
                )
            if payload_digest(event.get("payload")) != event.get("payload_hash"):
                raise ProviderStreamConflict(
                    "private-stream journal payload hash does not match payload"
                )
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                raise ProviderStreamConflict(
                    "private-stream event payload must be an object"
                )
            if {
                key: payload.get(key)
                for key in self.scope.as_payload()
            } != self.scope.as_payload():
                raise ProviderStreamConflict(
                    "private-stream durable event scope does not match lifecycle"
                )
            operation = _text(payload.get("operation"), name="operation")
            request = payload.get("request")
            result = payload.get("snapshot")
            key = _text(payload.get("idempotency_key"), name="idempotency_key")
            request_hash = _digest(payload.get("request_hash"), name="request_hash")
            if not isinstance(request, Mapping):
                raise ProviderStreamConflict(
                    "private-stream durable request must be an object"
                )
            request_dict = dict(request)
            if payload_digest(request_dict) != request_hash:
                raise ProviderStreamConflict(
                    "private-stream request hash does not match request"
                )
            if key in idempotency:
                raise ProviderStreamConflict(
                    "duplicate private-stream idempotency event was persisted"
                )
            self._apply(projection, operation, request_dict)
            projection.aggregate_version = int(event["aggregate_version"])
            actual = _snapshot_payload(projection.snapshot())
            if actual != result:
                raise ProviderStreamConflict(
                    "private-stream replay does not match durable snapshot"
                )
            idempotency[key] = (request_hash, actual)
        return projection, idempotency

    def _reload(self) -> None:
        self._projection, self._idempotency = self._replay(self._events())

    @staticmethod
    def _same_event(
        left: ProviderPrivateStreamEvent,
        right: ProviderPrivateStreamEvent,
    ) -> bool:
        return left == right

    def _apply(
        self,
        projection: _Projection,
        operation: str,
        request: dict[str, object],
    ) -> str:
        if operation == "START_GENERATION":
            connection_id = _text(request.get("connection_id"), name="connection_id")
            expected_generation = projection.generation + 1
            requested_generation = request.get("generation")
            if requested_generation != expected_generation:
                raise ProviderStreamConflict(
                    "connection generation does not follow durable stream generation"
                )
            projection.recovery_floor_sequence = projection.last_sequence
            projection.generation = expected_generation
            projection.connection_id = connection_id
            projection.status = "RECOVERING"
            projection.snapshot_sequence = None
            projection.last_sequence = None
            projection.last_reason = "NEW_CONNECTION_REQUIRES_PROVIDER_RECOVERY"
            projection.recovery_evidence_refs = ()
            projection.current_events.clear()
            projection.buffered_events.clear()
            return "RECOVERING"

        if operation == "OBSERVE_EVENT":
            if projection.generation == 0 or projection.connection_id is None:
                raise ProviderStreamConflict(
                    "private-stream event requires an active connection generation"
                )
            generation = request.get("generation")
            if generation != projection.generation:
                raise ProviderStreamConflict(
                    "private-stream event belongs to a stale connection generation"
                )
            event = _event_from_payload(request.get("event"))
            sequence = _sequence(event.sequence, name="sequence")

            if projection.status == "RECOVERING":
                existing = projection.buffered_events.get(sequence)
                if existing is not None:
                    if self._same_event(existing, event):
                        return "DUPLICATE"
                    projection.status = "GAPPED"
                    projection.last_reason = "CONFLICTING_BUFFERED_SEQUENCE"
                    return "GAP_DETECTED"
                projection.buffered_events[sequence] = event
                return "BUFFERED"

            if projection.status != "LIVE":
                raise ProviderStreamConflict(
                    "private-stream events require RECOVERING or LIVE status"
                )
            if projection.last_sequence is None or projection.snapshot_sequence is None:
                raise ProviderStreamConflict(
                    "LIVE private stream is missing validated sequence state"
                )

            if sequence <= projection.snapshot_sequence:
                return "STALE_COVERED"

            existing = projection.current_events.get(sequence)
            if existing is not None:
                if self._same_event(existing, event):
                    return "DUPLICATE"
                projection.status = "GAPPED"
                projection.last_reason = "CONFLICTING_SEQUENCE_IDENTITY"
                return "GAP_DETECTED"

            expected = projection.last_sequence + 1
            if sequence != expected:
                projection.status = "GAPPED"
                projection.last_reason = (
                    "SEQUENCE_GAP"
                    if sequence > expected
                    else "UNEXPLAINED_OUT_OF_ORDER_EVENT"
                )
                return "GAP_DETECTED"

            projection.current_events[sequence] = event
            projection.last_sequence = sequence
            projection.last_reason = "CONTIGUOUS_EVENT"
            return "APPLIED"

        if operation == "COMPLETE_RECOVERY":
            if projection.status != "RECOVERING":
                raise ProviderStreamConflict(
                    "provider recovery can complete only for a RECOVERING stream"
                )
            generation = request.get("generation")
            if generation != projection.generation:
                raise ProviderStreamConflict(
                    "recovery completion belongs to a stale connection generation"
                )
            snapshot_sequence = _sequence(
                request.get("snapshot_sequence"),
                name="snapshot_sequence",
            )
            if (
                projection.recovery_floor_sequence is not None
                and snapshot_sequence < projection.recovery_floor_sequence
            ):
                raise ProviderStreamConflict(
                    "provider recovery snapshot regressed behind the durable pre-restart watermark"
                )
            checkpoint = request.get("checkpoint")
            if not isinstance(checkpoint, Mapping):
                raise ProviderStreamConflict(
                    "provider recovery requires a durable reconciliation checkpoint"
                )
            if (
                checkpoint.get("provider_id") != projection.scope.provider_id
                or checkpoint.get("account_id") != projection.scope.account_id
                or checkpoint.get("environment") != projection.scope.environment
            ):
                raise ProviderStreamConflict(
                    "provider recovery checkpoint scope mismatch"
                )
            _text(checkpoint.get("event_id"), name="checkpoint_event_id")
            _digest(
                checkpoint.get("payload_hash"),
                name="checkpoint_payload_hash",
            )
            journal_sequence = checkpoint.get("journal_sequence")
            if (
                isinstance(journal_sequence, bool)
                or not isinstance(journal_sequence, int)
                or journal_sequence <= 0
            ):
                raise ProviderStreamConflict(
                    "provider recovery checkpoint lacks durable journal sequence"
                )
            if (
                checkpoint.get("complete") is not True
                or checkpoint.get("snapshot_consistent") is not True
            ):
                raise ProviderStreamConflict(
                    "provider recovery checkpoint is incomplete or inconsistent"
                )
            snapshot = checkpoint.get("snapshot")
            if not isinstance(snapshot, Mapping):
                raise ProviderStreamConflict(
                    "provider recovery checkpoint lacks snapshot timing"
                )
            mode = _text(snapshot.get("mode"), name="snapshot_mode").upper()
            if mode not in {"ATOMIC", "COMPOSED"}:
                raise ProviderStreamConflict(
                    "provider recovery checkpoint has unsupported snapshot mode"
                )
            started_text = _instant_text(
                snapshot.get("query_started_at"),
                name="snapshot_query_started_at",
            )
            completed_text = _instant_text(
                snapshot.get("query_completed_at"),
                name="snapshot_query_completed_at",
            )
            started = datetime.fromisoformat(
                started_text.replace("Z", "+00:00")
            )
            completed = datetime.fromisoformat(
                completed_text.replace("Z", "+00:00")
            )
            if completed < started:
                raise ProviderStreamConflict(
                    "provider recovery snapshot completion precedes start"
                )

            raw_refs = checkpoint.get("snapshot_evidence_refs")
            if not isinstance(raw_refs, list) or not raw_refs:
                raise ProviderStreamConflict(
                    "provider recovery checkpoint lacks issued read evidence"
                )
            refs = tuple(
                _authenticated_read_evidence_ref(ref)
                for ref in raw_refs
            )
            if len(set(refs)) != len(refs):
                raise ProviderStreamConflict(
                    "provider recovery checkpoint evidence references must be unique"
                )

            raw_observations = request.get("read_observations")
            if not isinstance(raw_observations, list) or not raw_observations:
                raise ProviderStreamConflict(
                    "provider recovery requires resolved authenticated-read observations"
                )
            observed_refs: list[str] = []
            for item in raw_observations:
                if not isinstance(item, Mapping):
                    raise ProviderStreamConflict(
                        "provider recovery read observation must be an object"
                    )
                ref = _authenticated_read_evidence_ref(
                    item.get("evidence_ref")
                )
                _digest(
                    item.get("response_sha256"),
                    name="response_sha256",
                )
                _digest(item.get("query_digest"), name="query_digest")
                provider_environment = _text(
                    item.get("provider_environment"),
                    name="provider_environment",
                ).upper()
                if provider_environment != projection.scope.provider_environment:
                    raise ProviderStreamConflict(
                        "provider recovery provider-environment mismatch"
                    )
                prepared_text = _instant_text(
                    item.get("prepared_at"),
                    name="prepared_at",
                )
                observed_text = _instant_text(
                    item.get("observed_at"),
                    name="observed_at",
                )
                prepared = datetime.fromisoformat(
                    prepared_text.replace("Z", "+00:00")
                )
                observed = datetime.fromisoformat(
                    observed_text.replace("Z", "+00:00")
                )
                if not (started <= prepared <= observed <= completed):
                    raise ProviderStreamConflict(
                        "provider recovery read observation lies outside checkpoint snapshot window"
                    )
                if ref in observed_refs:
                    raise ProviderStreamConflict(
                        "provider recovery read observations must be unique"
                    )
                observed_refs.append(ref)
            if set(observed_refs) != set(refs) or len(observed_refs) != len(refs):
                raise ProviderStreamConflict(
                    "resolved read observations do not match checkpoint evidence"
                )

            after_snapshot = sorted(
                sequence
                for sequence in projection.buffered_events
                if sequence > snapshot_sequence
            )
            expected = snapshot_sequence + 1
            for sequence in after_snapshot:
                if sequence != expected:
                    raise ProviderStreamConflict(
                        "buffered private-stream events are not contiguous after recovery snapshot"
                    )
                expected += 1

            projection.snapshot_sequence = snapshot_sequence
            projection.last_sequence = (
                after_snapshot[-1] if after_snapshot else snapshot_sequence
            )
            projection.current_events = {
                sequence: projection.buffered_events[sequence]
                for sequence in after_snapshot
            }
            projection.buffered_events.clear()
            projection.recovery_evidence_refs = refs
            projection.status = "LIVE"
            projection.last_reason = "PROVIDER_RECOVERY_COMPLETE"
            return "LIVE"

        if operation == "DISCONNECT":
            generation = request.get("generation")
            if generation != projection.generation or projection.generation == 0:
                raise ProviderStreamConflict(
                    "disconnect belongs to a stale or missing connection generation"
                )
            projection.status = "DISCONNECTED"
            projection.last_reason = _text(request.get("reason"), name="reason")
            return "DISCONNECTED"

        raise ProviderStreamConflict(
            f"unsupported private-stream lifecycle operation: {operation}"
        )

    def _commit(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        operation: str,
        request: dict[str, object],
        committed_at: str | None,
    ) -> tuple[ProviderPrivateStreamSnapshot, str, bool]:
        cid = _text(command_id, name="command_id")
        idem = _text(idempotency_key, name="idempotency_key")
        when = (
            _now()
            if committed_at is None
            else _instant_text(committed_at, name="committed_at")
        )

        events = self._events()
        projection, idempotency = self._replay(events)
        request_hash = payload_digest(request)
        existing = idempotency.get(idem)
        if existing is not None:
            if existing[0] != request_hash:
                raise ProviderStreamConflict(
                    "idempotency_key was already used for a different private-stream request"
                )
            self._projection = projection
            self._idempotency = idempotency
            return projection.snapshot(), "IDEMPOTENT_REPLAY", False

        disposition = self._apply(projection, operation, request)
        next_version = 1 if not events else int(events[-1]["aggregate_version"]) + 1
        projection.aggregate_version = next_version
        snapshot = projection.snapshot()
        payload = {
            **self.scope.as_payload(),
            "operation": operation,
            "request": request,
            "idempotency_key": idem,
            "request_hash": request_hash,
            "snapshot": _snapshot_payload(snapshot),
            "disposition": disposition,
        }
        envelope = {
            "event_id": _scope_identity(
                [
                    "event",
                    self.scope.aggregate_id,
                    cid,
                    str(next_version),
                    request_hash,
                ]
            ),
            "event_type": _EVENT_TYPE,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": self.scope.aggregate_id,
            "aggregate_version": str(next_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": when,
        }
        journal_command_id = _scope_identity(
            ["command", self.scope.aggregate_id, cid]
        )
        journal_idempotency_key = _scope_identity(
            ["idempotency", self.scope.aggregate_id, idem]
        )
        try:
            _, inserted, _ = self.store.commit_command(
                command_id=journal_command_id,
                actor=_COMMAND_ACTOR,
                environment=self.scope.environment,
                idempotency_key=journal_idempotency_key,
                request={
                    **self.scope.as_payload(),
                    "operation": operation,
                    "request": request,
                },
                result={
                    "snapshot": _snapshot_payload(snapshot),
                    "disposition": disposition,
                },
                state_version=next_version,
                events=[(envelope, None)],
            )
        except Exception:
            self._reload()
            raise
        self._reload()
        return self._projection.snapshot(), disposition, inserted

    @property
    def snapshot(self) -> ProviderPrivateStreamSnapshot:
        self._reload()
        return self._projection.snapshot()

    @property
    def is_ready(self) -> bool:
        self._reload()
        return (
            self._projection.status == "LIVE"
            and self._validated_generation == self._projection.generation
        )

    def require_ready(self) -> ProviderPrivateStreamSnapshot:
        snapshot = self.snapshot
        if (
            snapshot.status != "LIVE"
            or self._validated_generation != snapshot.generation
        ):
            raise ProviderStreamNotReady(
                "private stream is not current; provider recovery/revalidation is required"
            )
        return snapshot

    def start_generation(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        connection_id: str,
        committed_at: str | None = None,
    ) -> ProviderPrivateStreamSnapshot:
        self._reload()
        connection = _text(connection_id, name="connection_id")
        if (
            self._projection.generation > 0
            and self._projection.connection_id == connection
            and self._projection.status != "DISCONNECTED"
        ):
            retry_request = {
                "generation": self._projection.generation,
                "connection_id": connection,
            }
            existing = self._idempotency.get(
                _text(idempotency_key, name="idempotency_key")
            )
            if (
                existing is not None
                and existing[0] == payload_digest(retry_request)
            ):
                return self._projection.snapshot()
            raise ProviderStreamConflict(
                "connection_id already names the current generation; "
                "exact retry requires its original idempotency key"
            )
        self._validated_generation = None
        request = {
            "generation": self._projection.generation + 1,
            "connection_id": connection,
        }
        snapshot, _, _ = self._commit(
            command_id=command_id,
            idempotency_key=idempotency_key,
            operation="START_GENERATION",
            request=request,
            committed_at=committed_at,
        )
        return snapshot

    def observe_event(
        self,
        observation: ProviderPrivateStreamObservation,
        *,
        command_id: str,
        idempotency_key: str,
        committed_at: str | None = None,
    ) -> ProviderPrivateStreamEventResult:
        if not isinstance(observation, ProviderPrivateStreamObservation):
            raise TypeError(
                "observation must be ProviderPrivateStreamObservation"
            )
        self._reload()
        projection = self._projection
        if projection.connection_id is None:
            raise ProviderStreamConflict(
                "private-stream observation requires an active connection"
            )
        event = _event_from_observation(
            observation,
            scope=self.scope,
            connection_id=projection.connection_id,
        )

        sequence = _sequence(event.sequence, name="sequence")
        if projection.status == "RECOVERING":
            prior = projection.buffered_events.get(sequence)
            if prior is not None and self._same_event(prior, event):
                return ProviderPrivateStreamEventResult(
                    snapshot=projection.snapshot(),
                    disposition="DUPLICATE",
                    committed=False,
                )
        elif projection.status == "LIVE":
            if self._validated_generation != projection.generation:
                raise ProviderStreamNotReady(
                    "process restart requires a new private-stream generation before events are trusted"
                )
            if (
                projection.snapshot_sequence is not None
                and sequence <= projection.snapshot_sequence
            ):
                return ProviderPrivateStreamEventResult(
                    snapshot=projection.snapshot(),
                    disposition="STALE_COVERED",
                    committed=False,
                )
            prior = projection.current_events.get(sequence)
            if prior is not None and self._same_event(prior, event):
                return ProviderPrivateStreamEventResult(
                    snapshot=projection.snapshot(),
                    disposition="DUPLICATE",
                    committed=False,
                )

        request = {
            "generation": projection.generation,
            "event": event.as_payload(),
        }
        snapshot, disposition, committed = self._commit(
            command_id=command_id,
            idempotency_key=idempotency_key,
            operation="OBSERVE_EVENT",
            request=request,
            committed_at=committed_at,
        )
        if disposition == "GAP_DETECTED":
            self._validated_generation = None
        return ProviderPrivateStreamEventResult(
            snapshot=snapshot,
            disposition=disposition,
            committed=committed,
        )

    def complete_recovery(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        snapshot_sequence: str,
        checkpoint_event_id: str,
        read_observations: tuple[ProviderResponseObservation, ...],
        committed_at: str | None = None,
    ) -> ProviderPrivateStreamSnapshot:
        self._reload()
        checkpoint_id = _text(
            checkpoint_event_id,
            name="checkpoint_event_id",
        )
        try:
            checkpoint = require_current_reconciliation_checkpoint(
                self.store,
                checkpoint_event_id=checkpoint_id,
                provider_id=self.scope.provider_id,
                account_id=self.scope.account_id,
                environment=self.scope.environment,
                provider_environment=self.scope.provider_environment,
            )
        except (KeyError, ValueError) as error:
            raise ProviderStreamConflict(
                "reconciliation checkpoint is not current for private-stream scope"
            ) from error
        payload = checkpoint.get("payload")
        if not isinstance(payload, Mapping):
            raise ProviderStreamConflict(
                "reconciliation checkpoint payload is unavailable"
            )
        if (
            payload.get("complete") is not True
            or payload.get("snapshot_consistent") is not True
        ):
            raise ProviderStreamConflict(
                "reconciliation checkpoint is incomplete or inconsistent"
            )
        snapshot_payload = payload.get("snapshot")
        if not isinstance(snapshot_payload, Mapping):
            raise ProviderStreamConflict(
                "reconciliation checkpoint lacks snapshot timing"
            )
        raw_refs = payload.get("snapshot_evidence_refs")
        if not isinstance(raw_refs, list) or not raw_refs:
            raise ProviderStreamConflict(
                "reconciliation checkpoint lacks issued snapshot evidence"
            )
        checkpoint_refs = tuple(
            _authenticated_read_evidence_ref(ref)
            for ref in raw_refs
        )
        if len(checkpoint_refs) != len(set(checkpoint_refs)):
            raise ProviderStreamConflict(
                "reconciliation checkpoint snapshot evidence must be unique"
            )

        observations = tuple(read_observations)
        if not observations:
            raise ProviderStreamConflict(
                "provider recovery requires resolved authenticated-read observations"
            )
        observation_payloads = [
            _read_observation_request(observation, scope=self.scope)
            for observation in observations
        ]
        observed_refs = tuple(
            item["evidence_ref"] for item in observation_payloads
        )
        if (
            len(observed_refs) != len(set(observed_refs))
            or set(observed_refs) != set(checkpoint_refs)
            or len(observed_refs) != len(checkpoint_refs)
        ):
            raise ProviderStreamConflict(
                "resolved read observations do not match checkpoint evidence"
            )

        payload_hash = _digest(
            checkpoint.get("payload_hash"),
            name="checkpoint_payload_hash",
        )
        journal_sequence = checkpoint.get("journal_sequence")
        if (
            isinstance(journal_sequence, bool)
            or not isinstance(journal_sequence, int)
            or journal_sequence <= 0
        ):
            raise ProviderStreamConflict(
                "reconciliation checkpoint lacks durable journal sequence"
            )
        request = {
            "generation": self._projection.generation,
            "snapshot_sequence": _sequence_text(
                _sequence(snapshot_sequence, name="snapshot_sequence")
            ),
            "checkpoint": {
                "event_id": checkpoint_id,
                "payload_hash": payload_hash,
                "journal_sequence": journal_sequence,
                "provider_id": self.scope.provider_id,
                "account_id": self.scope.account_id,
                "environment": self.scope.environment,
                "provider_environment": self.scope.provider_environment,
                "complete": True,
                "snapshot_consistent": True,
                "snapshot": {
                    "mode": snapshot_payload.get("mode"),
                    "query_started_at": snapshot_payload.get(
                        "query_started_at"
                    ),
                    "query_completed_at": snapshot_payload.get(
                        "query_completed_at"
                    ),
                },
                "snapshot_evidence_refs": list(checkpoint_refs),
            },
            "read_observations": observation_payloads,
        }
        snapshot, disposition, _ = self._commit(
            command_id=command_id,
            idempotency_key=idempotency_key,
            operation="COMPLETE_RECOVERY",
            request=request,
            committed_at=committed_at,
        )
        if disposition not in {"LIVE", "IDEMPOTENT_REPLAY"}:
            raise ProviderStreamConflict(
                "provider recovery did not produce a LIVE stream"
            )
        self._validated_generation = snapshot.generation
        return snapshot

    def disconnect(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        reason: str,
        committed_at: str | None = None,
    ) -> ProviderPrivateStreamSnapshot:
        self._reload()
        request = {
            "generation": self._projection.generation,
            "reason": _text(reason, name="reason"),
        }
        snapshot, _, _ = self._commit(
            command_id=command_id,
            idempotency_key=idempotency_key,
            operation="DISCONNECT",
            request=request,
            committed_at=committed_at,
        )
        self._validated_generation = None
        return snapshot
