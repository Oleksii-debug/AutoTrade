from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


class ReplayError(ValueError):
    pass


def _instant(value: str, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ReplayError(f"{field} must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ReplayError(f"{field} must be an ISO-8601 timestamp") from error
    return parsed.astimezone(timezone.utc)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _canonical_payload(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("payload must be a mapping")
    normalized = json.loads(
        json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )
    if not isinstance(normalized, dict):
        raise TypeError("payload must serialize as an object")
    frozen = _deep_freeze(normalized)
    if not isinstance(frozen, Mapping):
        raise TypeError("payload must freeze as an object")
    return frozen


@dataclass(frozen=True)
class ReplayEvent:
    sequence: int
    available_at: str
    source_version: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise TypeError("sequence must be an integer")
        if self.sequence < 0:
            raise ReplayError("sequence must be non-negative")
        _instant(self.available_at, field="available_at")
        if not isinstance(self.source_version, str) or not self.source_version.strip():
            raise ReplayError("source_version must be non-empty")
        object.__setattr__(self, "payload", _canonical_payload(self.payload))


_SHA256_HEX = frozenset("0123456789abcdef")
REQUIRED_RUNTIME_COMPONENTS = frozenset(
    {
        "pending_event_queue",
        "rng_state",
        "strategy_state",
        "portfolio_accounting_state",
        "execution_state",
        "accrual_state",
        "policy_state",
        "instrument_state",
        "provider_state",
        "experiment_state",
    }
)


def _sha256_hex(value: str, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA256_HEX for character in value)
    ):
        raise ReplayError(f"{field} must be a canonical lowercase SHA-256 hex digest")
    return value


def _build_sha(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) not in {40, 64}
        or any(character not in _SHA256_HEX for character in value)
    ):
        raise ReplayError("build_sha must be a canonical lowercase Git object hash")
    return value


def _component_bindings(
    values: Mapping[str, str],
) -> Mapping[str, str]:
    if not isinstance(values, Mapping):
        raise TypeError("runtime component bindings must be a mapping")
    normalized: dict[str, str] = {}
    for raw_name, raw_digest in values.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ReplayError("runtime component names must be non-empty")
        name = raw_name.strip()
        if name != raw_name:
            raise ReplayError("runtime component names must be canonical text")
        if name in normalized:
            raise ReplayError("runtime component names must be unique")
        normalized[name] = _sha256_hex(
            raw_digest,
            field=f"runtime component {name}",
        )
    missing = sorted(REQUIRED_RUNTIME_COMPONENTS - set(normalized))
    if missing:
        raise ReplayError(
            "composite replay checkpoint is missing required runtime components: "
            + ", ".join(missing)
        )
    return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(frozen=True)
class CompositeReplayCheckpoint:
    """Immutable whole-runtime resume gate over existing component authorities.

    Component digests are references to canonical state owned elsewhere; this
    envelope does not become a second ledger, scheduler, strategy store or RNG
    authority. Resume is permitted only when every bound component, build and
    experiment/protocol identity matches before another replay event is exposed.
    """

    replay: "ReplayCheckpoint"
    runtime_components: Mapping[str, str]
    build_sha: str
    protocol_ref: str
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if not isinstance(self.replay, ReplayCheckpoint):
            raise TypeError("replay must be ReplayCheckpoint")
        components = _component_bindings(self.runtime_components)
        build = _build_sha(self.build_sha)
        if not isinstance(self.protocol_ref, str) or not self.protocol_ref.strip():
            raise ReplayError("protocol_ref must be non-empty")
        protocol = self.protocol_ref.strip()
        if protocol != self.protocol_ref:
            raise ReplayError("protocol_ref must be canonical text")
        if self.schema_version != "1.0.0":
            raise ReplayError("unsupported composite replay checkpoint schema")
        object.__setattr__(self, "runtime_components", components)
        object.__setattr__(self, "build_sha", build)
        object.__setattr__(self, "protocol_ref", protocol)

    @property
    def fingerprint(self) -> str:
        material = {
            "schema_version": self.schema_version,
            "replay": {
                "dataset_digest": self.replay.dataset_digest,
                "cursor": self.replay.cursor,
                "clock": self.replay.clock,
            },
            "runtime_components": dict(self.runtime_components),
            "build_sha": self.build_sha,
            "protocol_ref": self.protocol_ref,
        }
        return sha256(
            json.dumps(
                material,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class ReplayCheckpoint:
    dataset_digest: str
    cursor: int
    clock: str

    def __post_init__(self) -> None:
        if not isinstance(self.dataset_digest, str) or len(self.dataset_digest) != 64:
            raise ReplayError("dataset_digest must be a SHA-256 hex digest")
        try:
            int(self.dataset_digest, 16)
        except ValueError as error:
            raise ReplayError("dataset_digest must be hexadecimal") from error
        if isinstance(self.cursor, bool) or not isinstance(self.cursor, int):
            raise TypeError("cursor must be an integer")
        if self.cursor < 0:
            raise ReplayError("cursor must be non-negative")
        _instant(self.clock, field="clock")


def _event_record(event: ReplayEvent) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "available_at": event.available_at,
        "source_version": event.source_version,
        "payload": _plain_json(event.payload),
    }


def dataset_digest(events: Sequence[ReplayEvent]) -> str:
    encoded = json.dumps(
        [_event_record(event) for event in events],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class CausalReplay:
    """Deterministic replay that exposes only data available by the replay clock."""

    def __init__(
        self,
        events: Iterable[ReplayEvent],
        *,
        start_at: str,
        checkpoint: ReplayCheckpoint | None = None,
    ) -> None:
        records = tuple(events)
        for event in records:
            if not isinstance(event, ReplayEvent):
                raise TypeError("events must contain ReplayEvent values")

        ordered = tuple(
            sorted(
                records,
                key=lambda item: (
                    _instant(item.available_at, field="available_at"),
                    item.sequence,
                ),
            )
        )
        if len({event.sequence for event in ordered}) != len(ordered):
            raise ReplayError("event sequence values must be unique")

        self._events = ordered
        self._digest = dataset_digest(ordered)
        start = _instant(start_at, field="start_at")

        if checkpoint is None:
            self._cursor = 0
            self._clock = start
            return

        if checkpoint.dataset_digest != self._digest:
            raise ReplayError("checkpoint dataset digest does not match replay dataset")
        if checkpoint.cursor > len(self._events):
            raise ReplayError("checkpoint cursor exceeds dataset length")
        restored_clock = _instant(checkpoint.clock, field="clock")
        if restored_clock < start:
            raise ReplayError("checkpoint clock precedes requested start")
        causally_visible = sum(
            _instant(event.available_at, field="available_at") <= restored_clock
            for event in self._events
        )
        if checkpoint.cursor > causally_visible:
            raise ReplayError(
                "checkpoint cursor consumes events unavailable at checkpoint clock"
            )
        self._cursor = checkpoint.cursor
        self._clock = restored_clock

    @property
    def clock(self) -> str:
        return self._clock.isoformat().replace("+00:00", "Z")

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def digest(self) -> str:
        return self._digest

    def advance_to(self, instant: str) -> tuple[ReplayEvent, ...]:
        target = _instant(instant, field="instant")
        if target < self._clock:
            raise ReplayError("replay clock cannot move backwards")

        visible: list[ReplayEvent] = []
        while self._cursor < len(self._events):
            event = self._events[self._cursor]
            if _instant(event.available_at, field="available_at") > target:
                break
            visible.append(event)
            self._cursor += 1

        self._clock = target
        return tuple(visible)

    def peek_next(self) -> ReplayEvent | None:
        if self._cursor >= len(self._events):
            return None
        event = self._events[self._cursor]
        if _instant(event.available_at, field="available_at") > self._clock:
            return None
        return event

    def checkpoint(self) -> ReplayCheckpoint:
        return ReplayCheckpoint(
            dataset_digest=self._digest,
            cursor=self._cursor,
            clock=self.clock,
        )

    def remaining(self) -> int:
        return len(self._events) - self._cursor

    def composite_checkpoint(
        self,
        *,
        runtime_components: Mapping[str, str],
        build_sha: str,
        protocol_ref: str,
    ) -> CompositeReplayCheckpoint:
        """Bind this source cursor to the exact externally owned runtime cut."""

        return CompositeReplayCheckpoint(
            replay=self.checkpoint(),
            runtime_components=runtime_components,
            build_sha=build_sha,
            protocol_ref=protocol_ref,
        )


def resume_from_composite_checkpoint(
    events: Iterable[ReplayEvent],
    *,
    start_at: str,
    checkpoint: CompositeReplayCheckpoint,
    runtime_components: Mapping[str, str],
    build_sha: str,
    protocol_ref: str,
) -> CausalReplay:
    """Validate the whole runtime cut before exposing the next source event."""

    if not isinstance(checkpoint, CompositeReplayCheckpoint):
        raise TypeError("checkpoint must be CompositeReplayCheckpoint")
    current = CompositeReplayCheckpoint(
        replay=checkpoint.replay,
        runtime_components=runtime_components,
        build_sha=build_sha,
        protocol_ref=protocol_ref,
    )
    if current.fingerprint != checkpoint.fingerprint:
        raise ReplayError(
            "composite replay checkpoint does not match current runtime state cut"
        )
    return CausalReplay(
        events,
        start_at=start_at,
        checkpoint=checkpoint.replay,
    )
