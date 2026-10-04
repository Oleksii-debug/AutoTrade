"""Fail-closed identity/calendar blinding for causal historical replay.

The privileged replay side may use this module to derive a strategy-facing
historical dataset that preserves causal/economic timing while removing direct
identity and absolute-calendar cues.  This is not a hostile-code sandbox: the
raw dataset and this transformation must remain outside an untrusted strategy
process, as required by the replay architecture.

Version 1 deliberately permits only identity-preserving economic values.
Non-identity price scaling is rejected until an instrument-specific transform
can present complete external invariant evidence for price, size, multiplier,
ticks, strikes, fees, currency conversion, funding, liquidity and execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Mapping, Sequence

from .feeder import CausalDataset, CausalEvent

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_NAMESPACE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ABSOLUTE_DATE = re.compile(r"(?<!\d)(?:19|20)\d{2}-\d{2}-\d{2}(?!\d)")
_SCHEMA_VERSION = 1
_PRICE_SCALE_MODE = "IDENTITY"

_NAMESPACE_LABELS = {
    "INSTRUMENT": "Instrument",
    "PROVIDER": "Provider",
    "COMPANY": "Company",
    "COUNTRY": "Country",
    "POLITICIAN": "Person",
    "VENUE": "Venue",
    "SOURCE": "Source",
}


class BlindingError(ValueError):
    """Raised when a replay cannot be blinded without leaking declared cues."""


def _text(value: str, *, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise BlindingError(f"{name} is required")
    return value.strip()


def _digest(value: str, *, name: str) -> str:
    result = _text(value, name=name)
    if _SHA256.fullmatch(result) is None:
        raise BlindingError(f"{name} must be canonical sha256:<64 lowercase hex>")
    return result


def _path(value: tuple[str, ...], *, name: str) -> tuple[str, ...]:
    if type(value) is not tuple or not value:
        raise TypeError(f"{name} must be a non-empty exact tuple")
    result: list[str] = []
    for index, raw in enumerate(value):
        result.append(_text(raw, name=f"{name}[{index}]"))
    return tuple(result)


def _freeze(value: object, *, path: str = "payload") -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise TypeError(f"{path} must not contain binary floating-point values")
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            key = _text(raw_key, name=f"{path} key")
            if key in frozen:
                raise BlindingError(f"{path} has duplicate keys after normalization")
            frozen[key] = _freeze(raw_value, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze(item, path=f"{path}[]") for item in value)
    raise TypeError(f"{path} contains unsupported value type {type(value).__name__}")


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(value[key]) for key in sorted(value)}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _plain(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _microseconds(delta) -> int:
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def _calendar_value(value: object, *, path: str) -> datetime:
    if type(value) is not str:
        raise BlindingError(f"{path} calendar value must be an ISO string")
    raw = value.strip()
    if not raw:
        raise BlindingError(f"{path} calendar value is empty")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            parsed_date = date.fromisoformat(raw)
        except ValueError as error:
            raise BlindingError(f"{path} must be a valid ISO date") from error
        return datetime(
            parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=timezone.utc
        )
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise BlindingError(f"{path} must be a valid ISO date/time") from error
    if parsed.tzinfo is None:
        raise BlindingError(f"{path} date/time must include a timezone")
    return parsed.astimezone(timezone.utc)


def _lookup(payload: Mapping[str, object], path: tuple[str, ...]) -> tuple[bool, object]:
    current: object = payload
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _replace(payload: Mapping[str, object], path: tuple[str, ...], value: object) -> Mapping[str, object]:
    head = path[0]
    if head not in payload:
        raise BlindingError("cannot replace a missing declared path")
    copy = dict(payload)
    if len(path) == 1:
        copy[head] = value
        return copy
    child = payload[head]
    if not isinstance(child, Mapping):
        raise BlindingError("declared nested path crosses a non-mapping value")
    copy[head] = _replace(child, path[1:], value)
    return copy


def _format_offset(value: datetime, anchor: datetime) -> str:
    delta = _microseconds(value - anchor)
    sign = "+" if delta >= 0 else "-"
    return f"T{sign}{abs(delta)}us"


def _label_prefix(namespace: str) -> str:
    if namespace in _NAMESPACE_LABELS:
        return _NAMESPACE_LABELS[namespace]
    return namespace.replace("_", " ").title()


def _sort_key(*, shuffle_key: str, experiment_id: str, namespace: str, raw: str) -> bytes:
    material = "\x1f".join((shuffle_key, experiment_id, namespace, raw)).encode("utf-8")
    return sha256(material).digest()


def _scan_payload(
    value: object,
    *,
    raw_identities: frozenset[str],
    identity_paths: frozenset[tuple[str, ...]],
    calendar_paths: frozenset[tuple[str, ...]],
    path: tuple[str, ...] = (),
) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = path + (key,)
            key_fold = key.casefold()
            for raw in raw_identities:
                if raw and raw.casefold() in key_fold:
                    raise BlindingError(
                        f"payload key at {'.'.join(child_path)} contains a declared raw identity"
                    )
            if _ABSOLUTE_DATE.search(key):
                raise BlindingError(
                    f"payload key at {'.'.join(child_path)} contains an absolute calendar date"
                )
            _scan_payload(
                child,
                raw_identities=raw_identities,
                identity_paths=identity_paths,
                calendar_paths=calendar_paths,
                path=child_path,
            )
        return
    if isinstance(value, tuple):
        for index, child in enumerate(value):
            _scan_payload(
                child,
                raw_identities=raw_identities,
                identity_paths=identity_paths,
                calendar_paths=calendar_paths,
                path=path + (f"[{index}]",),
            )
        return
    if type(value) is not str:
        return
    if path not in identity_paths:
        folded = value.casefold()
        for raw in raw_identities:
            if raw and raw.casefold() in folded:
                raise BlindingError(
                    f"payload value at {'.'.join(path)} repeats a declared raw identity"
                )
    if path not in calendar_paths and _ABSOLUTE_DATE.search(value):
        raise BlindingError(
            f"payload value at {'.'.join(path)} exposes an undeclared absolute date"
        )


@dataclass(frozen=True, slots=True)
class IdentityField:
    """One structured payload path whose string identity must be pseudonymized."""

    path: tuple[str, ...]
    namespace: str
    required: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _path(self.path, name="identity path"))
        namespace = _text(self.namespace, name="namespace").upper()
        if _NAMESPACE.fullmatch(namespace) is None:
            raise BlindingError("namespace must match [A-Z][A-Z0-9_]*")
        object.__setattr__(self, "namespace", namespace)
        if type(self.required) is not bool:
            raise TypeError("required must be an exact bool")


@dataclass(frozen=True, slots=True)
class CalendarField:
    """One structured payload path containing an absolute date/date-time."""

    path: tuple[str, ...]
    required: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _path(self.path, name="calendar path"))
        if type(self.required) is not bool:
            raise TypeError("required must be an exact bool")


@dataclass(frozen=True, slots=True)
class BlindingProfile:
    """Registered structured blinding policy.

    V1 intentionally rejects non-identity price scaling.  Economic values are
    copied exactly; a later scaling implementation must be a separately
    qualified instrument/execution transformation.
    """

    identity_fields: tuple[IdentityField, ...]
    calendar_fields: tuple[CalendarField, ...] = ()
    strict_text_scan: bool = True
    price_scale_mode: str = _PRICE_SCALE_MODE

    def __post_init__(self) -> None:
        if type(self.identity_fields) is not tuple:
            raise TypeError("identity_fields must be an exact tuple")
        if type(self.calendar_fields) is not tuple:
            raise TypeError("calendar_fields must be an exact tuple")
        identities = tuple(
            item if type(item) is IdentityField else _raise_exact("IdentityField")
            for item in self.identity_fields
        )
        calendars = tuple(
            item if type(item) is CalendarField else _raise_exact("CalendarField")
            for item in self.calendar_fields
        )
        object.__setattr__(self, "identity_fields", identities)
        object.__setattr__(self, "calendar_fields", calendars)
        if type(self.strict_text_scan) is not bool:
            raise TypeError("strict_text_scan must be an exact bool")
        if self.price_scale_mode != _PRICE_SCALE_MODE:
            raise BlindingError(
                "non-identity price scaling is unsupported without complete "
                "instrument/execution invariant qualification"
            )
        paths = [item.path for item in identities] + [item.path for item in calendars]
        if len(paths) != len(set(paths)):
            raise BlindingError("identity/calendar paths must be unique and non-overlapping")

    @property
    def digest(self) -> str:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "identity_fields": [
                {
                    "path": list(item.path),
                    "namespace": item.namespace,
                    "required": item.required,
                }
                for item in self.identity_fields
            ],
            "calendar_fields": [
                {"path": list(item.path), "required": item.required}
                for item in self.calendar_fields
            ],
            "strict_text_scan": self.strict_text_scan,
            "price_scale_mode": self.price_scale_mode,
        }
        return "sha256:" + sha256(_canonical_bytes(payload)).hexdigest()


def _raise_exact(name: str):
    raise TypeError(f"items must be exact {name}")


@dataclass(frozen=True, slots=True)
class BlindedEvent:
    """Strategy-facing replay event with no raw identity or absolute timestamp."""

    event_id: str
    kind: str
    event_time_us: int
    available_at_us: int
    ingested_at_us: int
    session_index: int
    moment_index: int
    source_priority: int
    source_sequence: int
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _text(self.event_id, name="event_id"))
        object.__setattr__(self, "kind", _text(self.kind, name="kind").upper())
        for name in (
            "event_time_us",
            "available_at_us",
            "ingested_at_us",
            "session_index",
            "moment_index",
            "source_priority",
            "source_sequence",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise BlindingError(f"{name} must be a non-negative exact integer")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        object.__setattr__(self, "payload", _freeze(self.payload))

    @property
    def digest(self) -> str:
        return "sha256:" + sha256(
            _canonical_bytes(
                {
                    "schema_version": _SCHEMA_VERSION,
                    "event_id": self.event_id,
                    "kind": self.kind,
                    "event_time_us": self.event_time_us,
                    "available_at_us": self.available_at_us,
                    "ingested_at_us": self.ingested_at_us,
                    "session_index": self.session_index,
                    "moment_index": self.moment_index,
                    "source_priority": self.source_priority,
                    "source_sequence": self.source_sequence,
                    "payload": self.payload,
                }
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class BlindedReplayDataset:
    """Immutable blinded artifact and privileged audit commitments."""

    source_dataset_sha256: str
    experiment_id: str
    profile_sha256: str
    mapping_sha256: str
    shuffle_key_sha256: str
    training_cutoff_uncertainty: str
    price_scale_mode: str
    events: tuple[BlindedEvent, ...]
    blinded_dataset_sha256: str

    def __post_init__(self) -> None:
        source = _digest(self.source_dataset_sha256, name="source_dataset_sha256")
        profile = _digest(self.profile_sha256, name="profile_sha256")
        mapping = _digest(self.mapping_sha256, name="mapping_sha256")
        shuffle = _digest(self.shuffle_key_sha256, name="shuffle_key_sha256")
        experiment = _text(self.experiment_id, name="experiment_id")
        uncertainty = _text(
            self.training_cutoff_uncertainty, name="training_cutoff_uncertainty"
        )
        if self.price_scale_mode != _PRICE_SCALE_MODE:
            raise BlindingError("blinded artifact must preserve original economics")
        if type(self.events) is not tuple or any(type(item) is not BlindedEvent for item in self.events):
            raise TypeError("events must be an exact tuple of exact BlindedEvent")
        expected = _blinded_dataset_digest(
            source_dataset_sha256=source,
            experiment_id=experiment,
            profile_sha256=profile,
            mapping_sha256=mapping,
            shuffle_key_sha256=shuffle,
            training_cutoff_uncertainty=uncertainty,
            events=self.events,
        )
        supplied = _digest(self.blinded_dataset_sha256, name="blinded_dataset_sha256")
        if supplied != expected:
            raise BlindingError("blinded_dataset_sha256 does not match blinded content")
        object.__setattr__(self, "source_dataset_sha256", source)
        object.__setattr__(self, "profile_sha256", profile)
        object.__setattr__(self, "mapping_sha256", mapping)
        object.__setattr__(self, "shuffle_key_sha256", shuffle)
        object.__setattr__(self, "experiment_id", experiment)
        object.__setattr__(self, "training_cutoff_uncertainty", uncertainty)
        object.__setattr__(self, "blinded_dataset_sha256", supplied)

    @property
    def schema_version(self) -> int:
        return _SCHEMA_VERSION

    @property
    def forward_evidence_required(self) -> bool:
        return True


def _blinded_dataset_digest(
    *,
    source_dataset_sha256: str,
    experiment_id: str,
    profile_sha256: str,
    mapping_sha256: str,
    shuffle_key_sha256: str,
    training_cutoff_uncertainty: str,
    events: tuple[BlindedEvent, ...],
) -> str:
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "source_dataset_sha256": source_dataset_sha256,
        "experiment_id": experiment_id,
        "profile_sha256": profile_sha256,
        "mapping_sha256": mapping_sha256,
        "shuffle_key_sha256": shuffle_key_sha256,
        "training_cutoff_uncertainty": training_cutoff_uncertainty,
        "price_scale_mode": _PRICE_SCALE_MODE,
        "forward_evidence_required": True,
        "event_digests": [item.digest for item in events],
    }
    return "sha256:" + sha256(_canonical_bytes(payload)).hexdigest()


def blind_dataset(
    *,
    dataset: CausalDataset,
    experiment_id: str,
    shuffle_key_sha256: str,
    profile: BlindingProfile,
    training_cutoff_uncertainty: str,
) -> BlindedReplayDataset:
    """Derive a deterministic, experiment-specific blinded replay artifact."""

    if type(dataset) is not CausalDataset:
        raise TypeError("dataset must be exact CausalDataset")
    if type(profile) is not BlindingProfile:
        raise TypeError("profile must be exact BlindingProfile")
    experiment = _text(experiment_id, name="experiment_id")
    shuffle_key = _digest(shuffle_key_sha256, name="shuffle_key_sha256")
    uncertainty = _text(
        training_cutoff_uncertainty, name="training_cutoff_uncertainty"
    )

    # Re-materialization both snapshots the caller-owned object graph and detects
    # object.__setattr__ tampering after CausalDataset construction.
    snapshot = CausalDataset.create(
        manifest_sha256=dataset.manifest_sha256,
        events=dataset.events,
    )
    if snapshot.dataset_sha256 != dataset.dataset_sha256:
        raise BlindingError("source dataset content no longer matches its committed digest")
    if not snapshot.events:
        raise BlindingError("blinded replay requires at least one causal event")

    identity_paths = frozenset(item.path for item in profile.identity_fields)
    calendar_paths = frozenset(item.path for item in profile.calendar_fields)

    raw_by_namespace: dict[str, set[str]] = {}
    calendar_values: list[datetime] = []
    for event in snapshot.events:
        for field in profile.identity_fields:
            present, value = _lookup(event.payload, field.path)
            if not present:
                if field.required:
                    raise BlindingError(
                        f"required identity path {'.'.join(field.path)} is missing"
                    )
                continue
            if type(value) is not str or not value.strip():
                raise BlindingError(
                    f"identity path {'.'.join(field.path)} must contain non-empty text"
                )
            raw_by_namespace.setdefault(field.namespace, set()).add(value.strip())
        for field in profile.calendar_fields:
            present, value = _lookup(event.payload, field.path)
            if not present:
                if field.required:
                    raise BlindingError(
                        f"required calendar path {'.'.join(field.path)} is missing"
                    )
                continue
            calendar_values.append(
                _calendar_value(value, path=".".join(field.path))
            )

    raw_identities = frozenset(
        raw for values in raw_by_namespace.values() for raw in values
    )
    if profile.strict_text_scan:
        for event in snapshot.events:
            _scan_payload(
                event.payload,
                raw_identities=raw_identities,
                identity_paths=identity_paths,
                calendar_paths=calendar_paths,
            )

    identity_map: dict[tuple[str, str], str] = {}
    for namespace in sorted(raw_by_namespace):
        ordered = sorted(
            raw_by_namespace[namespace],
            key=lambda raw: (
                _sort_key(
                    shuffle_key=shuffle_key,
                    experiment_id=experiment,
                    namespace=namespace,
                    raw=raw,
                ),
                raw,
            ),
        )
        width = max(3, len(str(len(ordered))))
        prefix = _label_prefix(namespace)
        for index, raw in enumerate(ordered, start=1):
            identity_map[(namespace, raw)] = f"{prefix} {index:0{width}d}"

    mapping_commitment = [
        {"namespace": namespace, "raw": raw, "blind": blind}
        for (namespace, raw), blind in sorted(identity_map.items())
    ]
    mapping_sha256 = "sha256:" + sha256(
        (shuffle_key + "\n").encode("utf-8") + _canonical_bytes(mapping_commitment)
    ).hexdigest()

    anchor = min(
        [event.event_time for event in snapshot.events] + calendar_values
    )
    sessions = {
        day: index
        for index, day in enumerate(
            sorted({event.available_at.date() for event in snapshot.events}),
            start=1,
        )
    }
    moments = {
        moment: index
        for index, moment in enumerate(
            sorted({event.available_at for event in snapshot.events}),
            start=1,
        )
    }

    blinded_events: list[BlindedEvent] = []
    for event_index, event in enumerate(snapshot.events, start=1):
        payload: Mapping[str, object] = dict(event.payload)
        for field in profile.identity_fields:
            present, value = _lookup(event.payload, field.path)
            if not present:
                continue
            payload = _replace(
                payload,
                field.path,
                identity_map[(field.namespace, value.strip())],
            )
        for field in profile.calendar_fields:
            present, value = _lookup(event.payload, field.path)
            if not present:
                continue
            payload = _replace(
                payload,
                field.path,
                _format_offset(
                    _calendar_value(value, path=".".join(field.path)),
                    anchor,
                ),
            )

        blinded_events.append(
            BlindedEvent(
                event_id=f"Event {event_index:06d}",
                kind=event.kind,
                event_time_us=_microseconds(event.event_time - anchor),
                available_at_us=_microseconds(event.available_at - anchor),
                ingested_at_us=_microseconds(event.ingested_at - anchor),
                session_index=sessions[event.available_at.date()],
                moment_index=moments[event.available_at],
                source_priority=event.source_priority,
                source_sequence=event.source_sequence,
                payload=payload,
            )
        )

    events_tuple = tuple(blinded_events)
    digest = _blinded_dataset_digest(
        source_dataset_sha256=snapshot.dataset_sha256,
        experiment_id=experiment,
        profile_sha256=profile.digest,
        mapping_sha256=mapping_sha256,
        shuffle_key_sha256=shuffle_key,
        training_cutoff_uncertainty=uncertainty,
        events=events_tuple,
    )
    return BlindedReplayDataset(
        source_dataset_sha256=snapshot.dataset_sha256,
        experiment_id=experiment,
        profile_sha256=profile.digest,
        mapping_sha256=mapping_sha256,
        shuffle_key_sha256=shuffle_key,
        training_cutoff_uncertainty=uncertainty,
        price_scale_mode=_PRICE_SCALE_MODE,
        events=events_tuple,
        blinded_dataset_sha256=digest,
    )
