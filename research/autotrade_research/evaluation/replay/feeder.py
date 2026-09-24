"""Causal historical data feeder for AutoTrade scientific replay.

This module enforces an in-process causal publication boundary and deterministic
checkpoint/resume semantics. It is deliberately not a hostile-code sandbox:
untrusted strategies still require OS/process/filesystem/network isolation as
specified by the replay architecture.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHECKPOINT_SCHEMA_VERSION = 1


class CausalReplayError(ValueError):
    """Raised when replay evidence or causal state is invalid."""


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CausalReplayError(f"{name} is required")
    return value.strip()


def _digest(value: str, *, name: str) -> str:
    result = _text(value, name=name).lower()
    if _SHA256.fullmatch(result) is None:
        raise CausalReplayError(f"{name} must be canonical sha256:<64 lowercase hex>")
    return result


def _utc(value: datetime | str, *, name: str) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise CausalReplayError(f"{name} must be an ISO timestamp") from error
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError(f"{name} must be a datetime or ISO timestamp")
    if parsed.tzinfo is None:
        raise CausalReplayError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _nonnegative_int(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CausalReplayError(f"{name} must be a non-negative integer")
    return value


def _freeze_json(value: object, *, path: str = "payload") -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise TypeError(f"{path} must not contain binary floating-point values")
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            key = _text(raw_key, name=f"{path} key")
            if key in frozen:
                raise CausalReplayError(f"{path} has duplicate keys after normalization")
            frozen[key] = _freeze_json(raw_value, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json(item, path=f"{path}[]") for item in value)
    raise TypeError(f"{path} contains unsupported value type {type(value).__name__}")


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(value[key]) for key in sorted(value)}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _plain_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class CausalEvent:
    """One immutable observation with explicit event and availability times."""

    event_id: str
    kind: str
    event_time: datetime
    available_at: datetime
    source_priority: int
    source_sequence: int
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _text(self.event_id, name="event_id"))
        object.__setattr__(self, "kind", _text(self.kind, name="kind").upper())
        event_time = _utc(self.event_time, name="event_time")
        available_at = _utc(self.available_at, name="available_at")
        if available_at < event_time:
            raise CausalReplayError(
                "available_at cannot precede event_time; finalized/revised data "
                "must become visible no earlier than its evidenced availability"
            )
        object.__setattr__(self, "event_time", event_time)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(
            self,
            "source_priority",
            _nonnegative_int(self.source_priority, name="source_priority"),
        )
        object.__setattr__(
            self,
            "source_sequence",
            _nonnegative_int(self.source_sequence, name="source_sequence"),
        )
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        object.__setattr__(self, "payload", _freeze_json(self.payload))

    @classmethod
    def create(
        cls,
        *,
        event_id: str,
        kind: str,
        event_time: datetime | str,
        available_at: datetime | str,
        source_priority: int,
        source_sequence: int,
        payload: Mapping[str, object],
    ) -> "CausalEvent":
        return cls(
            event_id=event_id,
            kind=kind,
            event_time=_utc(event_time, name="event_time"),
            available_at=_utc(available_at, name="available_at"),
            source_priority=source_priority,
            source_sequence=source_sequence,
            payload=payload,
        )

    @property
    def ordering_key(self) -> tuple[datetime, int, int, str]:
        return (
            self.available_at,
            self.source_priority,
            self.source_sequence,
            self.event_id,
        )

    @property
    def digest(self) -> str:
        payload = {
            "event_id": self.event_id,
            "kind": self.kind,
            "event_time": self.event_time.isoformat(),
            "available_at": self.available_at.isoformat(),
            "source_priority": self.source_priority,
            "source_sequence": self.source_sequence,
            "payload": self.payload,
        }
        return "sha256:" + sha256(_canonical_bytes(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class CausalDataset:
    """Frozen event set bound to an external immutable dataset manifest."""

    manifest_sha256: str
    events: tuple[CausalEvent, ...]
    dataset_sha256: str

    @classmethod
    def create(
        cls,
        *,
        manifest_sha256: str,
        events: Iterable[CausalEvent],
    ) -> "CausalDataset":
        manifest = _digest(manifest_sha256, name="manifest_sha256")
        if isinstance(events, (str, bytes, bytearray)):
            raise TypeError("events must be an iterable of CausalEvent")
        materialized = tuple(events)
        if any(not isinstance(item, CausalEvent) for item in materialized):
            raise TypeError("events must contain only CausalEvent")
        ordered = tuple(sorted(materialized, key=lambda item: item.ordering_key))
        ids: set[str] = set()
        for item in ordered:
            if item.event_id in ids:
                raise CausalReplayError(f"duplicate event_id: {item.event_id}")
            ids.add(item.event_id)
        identity = {
            "manifest_sha256": manifest,
            "event_digests": [item.digest for item in ordered],
        }
        dataset_digest = "sha256:" + sha256(_canonical_bytes(identity)).hexdigest()
        return cls(
            manifest_sha256=manifest,
            events=ordered,
            dataset_sha256=dataset_digest,
        )


@dataclass(frozen=True, slots=True)
class CausalDataView:
    """Strategy-facing immutable view containing only already-published events."""

    simulation_time: datetime
    events: tuple[CausalEvent, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "simulation_time",
            _utc(self.simulation_time, name="simulation_time"),
        )
        if not isinstance(self.events, tuple):
            raise TypeError("events must be a tuple")
        for item in self.events:
            if not isinstance(item, CausalEvent):
                raise TypeError("events must contain CausalEvent")
            if item.available_at > self.simulation_time:
                raise CausalReplayError("causal view contains a future event")

    def by_kind(self, kind: str) -> tuple[CausalEvent, ...]:
        target = _text(kind, name="kind").upper()
        return tuple(item for item in self.events if item.kind == target)


@dataclass(frozen=True, slots=True)
class FeederCheckpoint:
    """Deterministic replay cursor bound to one exact frozen dataset."""

    schema_version: int
    manifest_sha256: str
    dataset_sha256: str
    simulation_time: datetime
    cursor: int
    published_prefix_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != _CHECKPOINT_SCHEMA_VERSION:
            raise CausalReplayError("unsupported checkpoint schema_version")
        object.__setattr__(
            self,
            "manifest_sha256",
            _digest(self.manifest_sha256, name="manifest_sha256"),
        )
        object.__setattr__(
            self,
            "dataset_sha256",
            _digest(self.dataset_sha256, name="dataset_sha256"),
        )
        object.__setattr__(
            self,
            "published_prefix_sha256",
            _digest(self.published_prefix_sha256, name="published_prefix_sha256"),
        )
        object.__setattr__(
            self,
            "simulation_time",
            _utc(self.simulation_time, name="simulation_time"),
        )
        object.__setattr__(self, "cursor", _nonnegative_int(self.cursor, name="cursor"))


def _prefix_digest(events: Sequence[CausalEvent], cursor: int) -> str:
    return "sha256:" + sha256(
        _canonical_bytes([item.digest for item in events[:cursor]])
    ).hexdigest()


class CausalFeeder:
    """Privileged deterministic feeder; strategy code should receive only view()."""

    def __init__(
        self,
        dataset: CausalDataset,
        *,
        start_time: datetime | str,
    ) -> None:
        if not isinstance(dataset, CausalDataset):
            raise TypeError("dataset must be CausalDataset")
        self._dataset = dataset
        self._clock = _utc(start_time, name="start_time")
        self._cursor = 0
        while (
            self._cursor < len(self._dataset.events)
            and self._dataset.events[self._cursor].available_at <= self._clock
        ):
            self._cursor += 1

    @classmethod
    def restore(
        cls,
        *,
        dataset: CausalDataset,
        checkpoint: FeederCheckpoint,
    ) -> "CausalFeeder":
        if not isinstance(dataset, CausalDataset):
            raise TypeError("dataset must be CausalDataset")
        if not isinstance(checkpoint, FeederCheckpoint):
            raise TypeError("checkpoint must be FeederCheckpoint")
        if checkpoint.manifest_sha256 != dataset.manifest_sha256:
            raise CausalReplayError("checkpoint manifest does not match dataset")
        if checkpoint.dataset_sha256 != dataset.dataset_sha256:
            raise CausalReplayError("checkpoint dataset digest does not match dataset")
        if checkpoint.cursor > len(dataset.events):
            raise CausalReplayError("checkpoint cursor exceeds dataset length")
        expected_prefix = _prefix_digest(dataset.events, checkpoint.cursor)
        if checkpoint.published_prefix_sha256 != expected_prefix:
            raise CausalReplayError("checkpoint published prefix digest is invalid")
        if checkpoint.cursor:
            last = dataset.events[checkpoint.cursor - 1]
            if last.available_at > checkpoint.simulation_time:
                raise CausalReplayError("checkpoint consumed an event from the future")
        if checkpoint.cursor < len(dataset.events):
            next_item = dataset.events[checkpoint.cursor]
            if next_item.available_at <= checkpoint.simulation_time:
                raise CausalReplayError(
                    "checkpoint cursor omits an event already available at simulation_time"
                )
        feeder = cls(dataset, start_time=checkpoint.simulation_time)
        if feeder._cursor != checkpoint.cursor:
            raise CausalReplayError(
                "checkpoint cursor does not equal the complete causal prefix"
            )
        return feeder

    @property
    def simulation_time(self) -> datetime:
        return self._clock

    @property
    def published_count(self) -> int:
        return self._cursor

    def advance_to(self, target_time: datetime | str) -> tuple[CausalEvent, ...]:
        target = _utc(target_time, name="target_time")
        if target < self._clock:
            raise CausalReplayError("simulation clock cannot move backwards")
        start = self._cursor
        events = self._dataset.events
        while self._cursor < len(events) and events[self._cursor].available_at <= target:
            self._cursor += 1
        self._clock = target
        return events[start:self._cursor]

    def advance_next_time(self) -> tuple[CausalEvent, ...]:
        """Privileged runtime helper; do not expose it to strategy/research code."""

        events = self._dataset.events
        if self._cursor >= len(events):
            return ()
        next_time = events[self._cursor].available_at
        return self.advance_to(next_time)

    def view(self) -> CausalDataView:
        return CausalDataView(
            simulation_time=self._clock,
            events=self._dataset.events[: self._cursor],
        )

    def checkpoint(self) -> FeederCheckpoint:
        return FeederCheckpoint(
            schema_version=_CHECKPOINT_SCHEMA_VERSION,
            manifest_sha256=self._dataset.manifest_sha256,
            dataset_sha256=self._dataset.dataset_sha256,
            simulation_time=self._clock,
            cursor=self._cursor,
            published_prefix_sha256=_prefix_digest(self._dataset.events, self._cursor),
        )
