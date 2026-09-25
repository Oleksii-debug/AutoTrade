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
