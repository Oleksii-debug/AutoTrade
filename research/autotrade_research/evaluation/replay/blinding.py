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
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json
import re
from types import MappingProxyType
from typing import Mapping, Sequence

from .feeder import CausalDataset, CausalEvent

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_NAMESPACE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ABSOLUTE_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(?:19|20)\d{2}-\d{2}-\d{2}(?!\d)"),
    re.compile(
        r"(?<!\d)(?:(?:19|20)\d{2}[/.]\d{1,2}[/.]\d{1,2}"
        r"|\d{1,2}[/.]\d{1,2}[/.](?:19|20)\d{2})(?!\d)"
    ),
    re.compile(
        r"(?i)\b(?:"
        r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
        r")\s+\d{1,2}(?:st|nd|rd|th)?(?:,)?\s+(?:19|20)\d{2}\b"
    ),
    re.compile(
        r"(?i)\b\d{1,2}(?:st|nd|rd|th)?\s+(?:"
        r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
        r")(?:,)?\s+(?:19|20)\d{2}\b"
    ),
)
_SCHEMA_VERSION = 1
_PRICE_SCALE_MODE = "IDENTITY"
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))

_NAMESPACE_LABELS = {
    "INSTRUMENT": "Instrument",
    "ASSET": "Asset",
    "PROVIDER": "Provider",
    "COMPANY": "Company",
    "COUNTRY": "Country",
    "POLITICIAN": "Person",
    "VENUE": "Venue",
    "SOURCE": "Source",
    "ENTITY": "Entity",
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
    # Blinded strategy data must contain only exact built-in/frozen values.
    # Caller-defined str/int/container subclasses can otherwise evade the leak
    # scan or execute behavior after crossing the strategy boundary.
    if value is None:
        return None
    if type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        raise TypeError(f"{path} must not contain binary floating-point values")
    if type(value) in {dict, _MAPPING_PROXY_TYPE}:
        frozen: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            key = _text(raw_key, name=f"{path} key")
            if key in frozen:
                raise BlindingError(f"{path} has duplicate keys after normalization")
            frozen[key] = _freeze(raw_value, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if type(value) is tuple:
        return tuple(_freeze(item, path=f"{path}[]") for item in value)
    raise TypeError(
        f"{path} must contain only exact JSON-like blinded value types"
    )


def _plain(value: object) -> object:
    if type(value) in {dict, _MAPPING_PROXY_TYPE}:
        return {key: _plain(value[key]) for key in sorted(value)}
    if type(value) is list:
        return [_plain(item) for item in value]
    if type(value) is tuple:
        return [_plain(item) for item in value]
    if value is None or type(value) in {str, bool, int}:
        return value
    raise TypeError("canonical blinded content contains a non-exact value type")


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
    key = bytes.fromhex(shuffle_key.removeprefix("sha256:"))
    message = "\x1f".join((experiment_id, namespace, raw)).encode("utf-8")
    return hmac.new(key, message, sha256).digest()


def _contains_identity(text: str, raw: str) -> bool:
    folded = text.casefold()
    needle = raw.casefold()
    if len(needle) >= 3:
        return needle in folded
    return re.search(
        rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])",
        folded,
    ) is not None


def _contains_absolute_date(text: str) -> bool:
    return any(pattern.search(text) is not None for pattern in _ABSOLUTE_DATE_PATTERNS)


def _scan_payload(
    value: object,
    *,
    raw_identities: frozenset[str],
    identity_paths: frozenset[tuple[str, ...]],
    calendar_paths: frozenset[tuple[str, ...]],
    path: tuple[str, ...] = (),
) -> None:
    if type(value) in {dict, _MAPPING_PROXY_TYPE}:
        for key, child in value.items():
            if type(key) is not str:
                raise TypeError("blinded payload keys must be exact str")
            child_path = path + (key,)
            for raw in raw_identities:
                if raw and _contains_identity(key, raw):
                    raise BlindingError(
                        f"payload key at {'.'.join(child_path)} contains a declared raw identity"
                    )
            if _contains_absolute_date(key):
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
    if type(value) is tuple:
        for index, child in enumerate(value):
            _scan_payload(
                child,
                raw_identities=raw_identities,
                identity_paths=identity_paths,
                calendar_paths=calendar_paths,
                path=path + (f"[{index}]",),
            )
        return
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is not str:
        raise TypeError(
            f"payload value at {'.'.join(path) or '<root>'} "
            "must use an exact JSON-like blinded scalar type"
        )
    if path not in identity_paths:
        for raw in raw_identities:
            if raw and _contains_identity(value, raw):
                raise BlindingError(
                    f"payload value at {'.'.join(path)} repeats a declared raw identity"
                )
    if path not in calendar_paths and _contains_absolute_date(value):
        raise BlindingError(
            f"payload value at {'.'.join(path)} exposes an undeclared absolute date"
        )


@dataclass(frozen=True, slots=True)
class IdentityField:
    """Canonical identity path plus optional aliases sharing one pseudonym."""

    path: tuple[str, ...]
    namespace: str
    required: bool = False
    alias_paths: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        canonical = _path(self.path, name="identity path")
        object.__setattr__(self, "path", canonical)
        if type(self.alias_paths) is not tuple:
            raise TypeError("alias_paths must be an exact tuple")
        aliases = tuple(
            _path(item, name=f"alias_paths[{index}]")
            for index, item in enumerate(self.alias_paths)
        )
        if canonical in aliases or len(aliases) != len(set(aliases)):
            raise BlindingError("identity alias paths must be unique and differ from canonical path")
        object.__setattr__(self, "alias_paths", aliases)
        namespace = _text(self.namespace, name="namespace").upper()
        if _NAMESPACE.fullmatch(namespace) is None:
            raise BlindingError("namespace must match [A-Z][A-Z0-9_]*")
        if namespace not in _NAMESPACE_LABELS:
            raise BlindingError(
                "namespace must be a registered neutral blinding namespace"
            )
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
        if self.strict_text_scan is not True:
            raise BlindingError("strict_text_scan cannot be disabled for blinded replay")
        price_scale_mode = _text(
            self.price_scale_mode,
            name="price_scale_mode",
        )
        if price_scale_mode != _PRICE_SCALE_MODE:
            raise BlindingError(
                "non-identity price scaling is unsupported without complete "
                "instrument/execution invariant qualification"
            )
        object.__setattr__(self, "price_scale_mode", price_scale_mode)
        paths = (
            [item.path for item in identities]
            + [alias for item in identities for alias in item.alias_paths]
            + [item.path for item in calendars]
        )
        for index, left in enumerate(paths):
            for right in paths[index + 1 :]:
                if (
                    left == right
                    or left[: len(right)] == right
                    or right[: len(left)] == left
                ):
                    raise BlindingError(
                        "identity/calendar paths must be unique and non-overlapping"
                    )

    @property
    def digest(self) -> str:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "identity_fields": [
                {
                    "path": list(item.path),
                    "namespace": item.namespace,
                    "required": item.required,
                    "alias_paths": [list(path) for path in item.alias_paths],
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


def _snapshot_blinding_profile(profile: BlindingProfile) -> BlindingProfile:
    """Detach one exact profile cut without invoking mutated container callbacks."""

    if type(profile) is not BlindingProfile:
        raise TypeError("profile must be exact BlindingProfile")
    identity_fields = object.__getattribute__(profile, "identity_fields")
    calendar_fields = object.__getattribute__(profile, "calendar_fields")
    strict_text_scan = object.__getattribute__(profile, "strict_text_scan")
    price_scale_mode = object.__getattribute__(profile, "price_scale_mode")
    if type(identity_fields) is not tuple:
        raise TypeError("profile identity_fields must remain an exact tuple")
    if type(calendar_fields) is not tuple:
        raise TypeError("profile calendar_fields must remain an exact tuple")

    identities: list[IdentityField] = []
    for index, field in enumerate(identity_fields):
        if type(field) is not IdentityField:
            raise TypeError(
                f"profile identity_fields[{index}] must remain exact IdentityField"
            )
        identities.append(
            IdentityField(
                path=object.__getattribute__(field, "path"),
                namespace=object.__getattribute__(field, "namespace"),
                required=object.__getattribute__(field, "required"),
                alias_paths=object.__getattribute__(field, "alias_paths"),
            )
        )

    calendars: list[CalendarField] = []
    for index, field in enumerate(calendar_fields):
        if type(field) is not CalendarField:
            raise TypeError(
                f"profile calendar_fields[{index}] must remain exact CalendarField"
            )
        calendars.append(
            CalendarField(
                path=object.__getattribute__(field, "path"),
                required=object.__getattribute__(field, "required"),
            )
        )

    return BlindingProfile(
        identity_fields=tuple(identities),
        calendar_fields=tuple(calendars),
        strict_text_scan=strict_text_scan,
        price_scale_mode=price_scale_mode,
    )


def _snapshot_source_dataset(dataset: CausalDataset) -> CausalDataset:
    """Detach one committed source cut before any authority-bearing traversal."""

    if type(dataset) is not CausalDataset:
        raise TypeError("dataset must be exact CausalDataset")
    manifest = _digest(
        object.__getattribute__(dataset, "manifest_sha256"),
        name="manifest_sha256",
    )
    committed_digest = _digest(
        object.__getattribute__(dataset, "dataset_sha256"),
        name="dataset_sha256",
    )
    source_events = object.__getattribute__(dataset, "events")
    if type(source_events) is not tuple:
        raise TypeError("source dataset events must remain an exact tuple")

    detached_events: list[CausalEvent] = []
    for index, event in enumerate(source_events):
        if type(event) is not CausalEvent:
            raise TypeError(
                f"source dataset event[{index}] must remain exact CausalEvent"
            )
        payload = object.__getattribute__(event, "payload")
        if type(payload) is not _MAPPING_PROXY_TYPE:
            raise TypeError(
                f"source dataset event[{index}] payload must remain an exact frozen mapping"
            )
        detached_events.append(
            CausalEvent(
                event_id=object.__getattribute__(event, "event_id"),
                kind=object.__getattribute__(event, "kind"),
                event_time=object.__getattribute__(event, "event_time"),
                available_at=object.__getattribute__(event, "available_at"),
                ingested_at=object.__getattribute__(event, "ingested_at"),
                source_priority=object.__getattribute__(event, "source_priority"),
                source_sequence=object.__getattribute__(event, "source_sequence"),
                payload=_freeze(
                    payload,
                    path=f"source event[{index}] payload",
                ),
            )
        )

    snapshot = CausalDataset.create(
        manifest_sha256=manifest,
        events=tuple(detached_events),
    )
    if snapshot.dataset_sha256 != committed_digest:
        raise BlindingError(
            "source dataset content no longer matches its committed digest"
        )
    return snapshot


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
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _text(self.event_id, name="event_id"))
        object.__setattr__(self, "kind", _text(self.kind, name="kind").upper())
        if type(self.event_time_us) is not int:
            raise BlindingError("event_time_us must be an exact integer")
        for name in (
            "available_at_us",
            "ingested_at_us",
            "session_index",
            "moment_index",
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
    shuffle_key_commitment_sha256: str
    training_cutoff_uncertainty: str
    price_scale_mode: str
    events: tuple[BlindedEvent, ...]
    blinded_dataset_sha256: str

    def __post_init__(self) -> None:
        source = _digest(self.source_dataset_sha256, name="source_dataset_sha256")
        profile = _digest(self.profile_sha256, name="profile_sha256")
        mapping = _digest(self.mapping_sha256, name="mapping_sha256")
        shuffle_commitment = _digest(
            self.shuffle_key_commitment_sha256,
            name="shuffle_key_commitment_sha256",
        )
        experiment = _text(self.experiment_id, name="experiment_id")
        uncertainty = _text(
            self.training_cutoff_uncertainty, name="training_cutoff_uncertainty"
        )
        price_scale_mode = _text(
            self.price_scale_mode,
            name="price_scale_mode",
        )
        if price_scale_mode != _PRICE_SCALE_MODE:
            raise BlindingError("blinded artifact must preserve original economics")
        if type(self.events) is not tuple or any(type(item) is not BlindedEvent for item in self.events):
            raise TypeError("events must be an exact tuple of exact BlindedEvent")
        expected = _blinded_dataset_digest(
            source_dataset_sha256=source,
            experiment_id=experiment,
            profile_sha256=profile,
            mapping_sha256=mapping,
            shuffle_key_commitment_sha256=shuffle_commitment,
            training_cutoff_uncertainty=uncertainty,
            events=self.events,
        )
        supplied = _digest(self.blinded_dataset_sha256, name="blinded_dataset_sha256")
        if supplied != expected:
            raise BlindingError("blinded_dataset_sha256 does not match blinded content")
        object.__setattr__(self, "source_dataset_sha256", source)
        object.__setattr__(self, "profile_sha256", profile)
        object.__setattr__(self, "mapping_sha256", mapping)
        object.__setattr__(
            self, "shuffle_key_commitment_sha256", shuffle_commitment
        )
        object.__setattr__(self, "experiment_id", experiment)
        object.__setattr__(self, "training_cutoff_uncertainty", uncertainty)
        object.__setattr__(self, "price_scale_mode", price_scale_mode)
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
    shuffle_key_commitment_sha256: str,
    training_cutoff_uncertainty: str,
    events: tuple[BlindedEvent, ...],
) -> str:
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "source_dataset_sha256": source_dataset_sha256,
        "experiment_id": experiment_id,
        "profile_sha256": profile_sha256,
        "mapping_sha256": mapping_sha256,
        "shuffle_key_commitment_sha256": shuffle_key_commitment_sha256,
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
    profile = _snapshot_blinding_profile(profile)
    experiment = _text(experiment_id, name="experiment_id")
    shuffle_key = _digest(shuffle_key_sha256, name="shuffle_key_sha256")
    shuffle_key_commitment = "sha256:" + sha256(
        shuffle_key.encode("utf-8")
    ).hexdigest()
    uncertainty = _text(
        training_cutoff_uncertainty, name="training_cutoff_uncertainty"
    )

    # Snapshot once before inspecting identities, calendars or economics.
    snapshot = _snapshot_source_dataset(dataset)
    if not snapshot.events:
        raise BlindingError("blinded replay requires at least one causal event")

    identity_paths = frozenset(
        path
        for item in profile.identity_fields
        for path in (item.path, *item.alias_paths)
    )
    calendar_paths = frozenset(item.path for item in profile.calendar_fields)

    raw_by_namespace: dict[str, set[str]] = {}
    aliases_by_identity: dict[tuple[str, str], set[str]] = {}
    alias_owner: dict[tuple[str, str], str] = {}
    calendar_values: list[datetime] = []
    for event in snapshot.events:
        for field in profile.identity_fields:
            present, value = _lookup(event.payload, field.path)
            if not present:
                alias_present = any(
                    _lookup(event.payload, alias_path)[0]
                    for alias_path in field.alias_paths
                )
                if alias_present:
                    raise BlindingError(
                        f"identity aliases for {'.'.join(field.path)} exist without canonical identity"
                    )
                if field.required:
                    raise BlindingError(
                        f"required identity path {'.'.join(field.path)} is missing"
                    )
                continue
            if type(value) is not str or not value.strip():
                raise BlindingError(
                    f"identity path {'.'.join(field.path)} must contain non-empty text"
                )
            canonical_raw = value.strip()
            raw_by_namespace.setdefault(field.namespace, set()).add(canonical_raw)
            aliases = aliases_by_identity.setdefault(
                (field.namespace, canonical_raw), set()
            )
            for alias_path in field.alias_paths:
                alias_present, alias_value = _lookup(event.payload, alias_path)
                if not alias_present:
                    continue
                if type(alias_value) is not str or not alias_value.strip():
                    raise BlindingError(
                        f"identity alias path {'.'.join(alias_path)} must contain non-empty text"
                    )
                alias_raw = alias_value.strip()
                owner_key = (field.namespace, alias_raw)
                prior_owner = alias_owner.get(owner_key)
                if prior_owner is not None and prior_owner != canonical_raw:
                    raise BlindingError(
                        "one identity alias cannot refer to multiple canonical identities"
                    )
                alias_owner[owner_key] = canonical_raw
                aliases.add(alias_raw)
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

    for (namespace, alias_raw), canonical_raw in alias_owner.items():
        if (
            alias_raw in raw_by_namespace.get(namespace, set())
            and alias_raw != canonical_raw
        ):
            raise BlindingError(
                "identity alias collides with another canonical identity"
            )

    raw_identities = frozenset(
        [
            raw
            for values in raw_by_namespace.values()
            for raw in values
        ]
        + [
            alias
            for aliases in aliases_by_identity.values()
            for alias in aliases
        ]
    )
    for event in snapshot.events:
        for raw in raw_identities:
            if raw and _contains_identity(event.kind, raw):
                raise BlindingError("event kind exposes a declared raw identity")
        if _contains_absolute_date(event.kind):
            raise BlindingError("event kind exposes an absolute calendar date")
        _scan_payload(
            event.payload,
            raw_identities=raw_identities,
            identity_paths=identity_paths,
            calendar_paths=calendar_paths,
        )

    identity_map: dict[tuple[str, str], str] = {}
    for namespace in sorted(raw_by_namespace):
        prefix = _label_prefix(namespace)
        for raw in sorted(raw_by_namespace[namespace]):
            # The visible pseudonym is a keyed function of this identity alone.
            # Appending unseen future identities therefore cannot change an
            # already-visible strategy prefix or reveal future universe size.
            token = _sort_key(
                shuffle_key=shuffle_key,
                experiment_id=experiment,
                namespace=namespace,
                raw=raw,
            ).hex().upper()
            identity_map[(namespace, raw)] = f"{prefix} {token}"

    mapping_commitment = [
        {
            "namespace": namespace,
            "raw": raw,
            "aliases": sorted(aliases_by_identity.get((namespace, raw), set())),
            "blind": blind,
        }
        for (namespace, raw), blind in sorted(identity_map.items())
    ]
    mapping_sha256 = "sha256:" + hmac.new(
        bytes.fromhex(shuffle_key.removeprefix("sha256:")),
        _canonical_bytes(mapping_commitment),
        sha256,
    ).hexdigest()

    # The relative clock origin is derived only from the first causally
    # available event.  A later-published correction may refer to an older
    # event_time; it must not retroactively change an already-visible prefix.
    anchor = snapshot.events[0].event_time
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
            pseudonym = identity_map[(field.namespace, value.strip())]
            payload = _replace(payload, field.path, pseudonym)
            for alias_path in field.alias_paths:
                alias_present, _alias_value = _lookup(event.payload, alias_path)
                if alias_present:
                    payload = _replace(payload, alias_path, pseudonym)
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
                payload=payload,
            )
        )

    events_tuple = tuple(blinded_events)
    digest = _blinded_dataset_digest(
        source_dataset_sha256=snapshot.dataset_sha256,
        experiment_id=experiment,
        profile_sha256=profile.digest,
        mapping_sha256=mapping_sha256,
        shuffle_key_commitment_sha256=shuffle_key_commitment,
        training_cutoff_uncertainty=uncertainty,
        events=events_tuple,
    )
    return BlindedReplayDataset(
        source_dataset_sha256=snapshot.dataset_sha256,
        experiment_id=experiment,
        profile_sha256=profile.digest,
        mapping_sha256=mapping_sha256,
        shuffle_key_commitment_sha256=shuffle_key_commitment,
        training_cutoff_uncertainty=uncertainty,
        price_scale_mode=_PRICE_SCALE_MODE,
        events=events_tuple,
        blinded_dataset_sha256=digest,
    )



_VIEW_SCHEMA_VERSION = 1
_CHECKPOINT_SCHEMA_VERSION = 1


def _signed_int(value: int, *, name: str) -> int:
    if type(value) is not int:
        raise BlindingError(f"{name} must be an exact integer")
    return value


def _blinded_prefix_digest(events: tuple[BlindedEvent, ...], cursor: int) -> str:
    if type(cursor) is not int or cursor < 0 or cursor > len(events):
        raise BlindingError("cursor is outside blinded event range")
    return "sha256:" + sha256(
        _canonical_bytes([item.digest for item in events[:cursor]])
    ).hexdigest()


def _source_anchor(dataset: CausalDataset, profile: BlindingProfile) -> datetime:
    if type(dataset) is not CausalDataset:
        raise TypeError("dataset must be exact CausalDataset")
    if type(profile) is not BlindingProfile:
        raise TypeError("profile must be exact BlindingProfile")
    if not dataset.events:
        raise BlindingError("blinded replay requires at least one causal event")
    # Must match blind_dataset(): a later-available correction may carry an
    # older event_time but cannot retroactively move the clock origin visible
    # to an already-published strategy prefix.
    return dataset.events[0].event_time


@dataclass(frozen=True, slots=True)
class BlindedDataView:
    """Strategy-facing causal prefix using only relative time."""

    simulation_time_us: int
    events: tuple[BlindedEvent, ...]

    def __post_init__(self) -> None:
        simulation = _signed_int(self.simulation_time_us, name="simulation_time_us")
        if type(self.events) is not tuple or any(type(item) is not BlindedEvent for item in self.events):
            raise TypeError("events must be an exact tuple of exact BlindedEvent")
        seen: set[str] = set()
        for item in self.events:
            if item.available_at_us > simulation:
                raise BlindingError("blinded view contains a future event")
            if item.event_id in seen:
                raise BlindingError("blinded view contains duplicate event identity")
            seen.add(item.event_id)
        object.__setattr__(self, "simulation_time_us", simulation)

    def by_kind(self, kind: str) -> tuple[BlindedEvent, ...]:
        target = _text(kind, name="kind").upper()
        return tuple(item for item in self.events if item.kind == target)


@dataclass(frozen=True, slots=True)
class BlindedInputEvidence:
    """Content-addressed evidence for exactly the visible causal prefix.

    Full-dataset identity is intentionally excluded: strategy-visible evidence
    must not change merely because privileged storage contains additional
    unseen future events.
    """

    schema_version: int
    simulation_time_us: int
    published_prefix_sha256: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != _VIEW_SCHEMA_VERSION:
            raise BlindingError("unsupported blinded input evidence schema_version")
        object.__setattr__(
            self, "simulation_time_us",
            _signed_int(self.simulation_time_us, name="simulation_time_us"),
        )
        object.__setattr__(
            self, "published_prefix_sha256",
            _digest(self.published_prefix_sha256, name="published_prefix_sha256"),
        )

    @property
    def digest(self) -> str:
        return "sha256:" + sha256(
            _canonical_bytes(
                {
                    "schema_version": self.schema_version,
                    "simulation_time_us": self.simulation_time_us,
                    "published_prefix_sha256": self.published_prefix_sha256,
                }
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class BlindedFeederCheckpoint:
    """Restart cursor without absolute calendar or raw identities."""

    schema_version: int
    source_dataset_sha256: str
    blinded_dataset_sha256: str
    experiment_id: str
    profile_sha256: str
    simulation_time_us: int
    cursor: int
    published_prefix_sha256: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != _CHECKPOINT_SCHEMA_VERSION:
            raise BlindingError("unsupported blinded checkpoint schema_version")
        object.__setattr__(
            self, "source_dataset_sha256",
            _digest(self.source_dataset_sha256, name="source_dataset_sha256"),
        )
        object.__setattr__(
            self, "blinded_dataset_sha256",
            _digest(self.blinded_dataset_sha256, name="blinded_dataset_sha256"),
        )
        object.__setattr__(
            self, "profile_sha256",
            _digest(self.profile_sha256, name="profile_sha256"),
        )
        object.__setattr__(self, "experiment_id", _text(self.experiment_id, name="experiment_id"))
        object.__setattr__(
            self, "simulation_time_us",
            _signed_int(self.simulation_time_us, name="simulation_time_us"),
        )
        if type(self.cursor) is not int or self.cursor < 0:
            raise BlindingError("cursor must be a non-negative exact integer")
        object.__setattr__(
            self, "published_prefix_sha256",
            _digest(self.published_prefix_sha256, name="published_prefix_sha256"),
        )

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_dataset_sha256": self.source_dataset_sha256,
            "blinded_dataset_sha256": self.blinded_dataset_sha256,
            "experiment_id": self.experiment_id,
            "profile_sha256": self.profile_sha256,
            "simulation_time_us": self.simulation_time_us,
            "cursor": self.cursor,
            "published_prefix_sha256": self.published_prefix_sha256,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "BlindedFeederCheckpoint":
        if not isinstance(record, Mapping):
            raise TypeError("checkpoint record must be a mapping")
        required = {
            "schema_version",
            "source_dataset_sha256",
            "blinded_dataset_sha256",
            "experiment_id",
            "profile_sha256",
            "simulation_time_us",
            "cursor",
            "published_prefix_sha256",
        }
        if set(record) != required:
            raise BlindingError("blinded checkpoint record keys mismatch")
        return cls(
            schema_version=record["schema_version"],
            source_dataset_sha256=record["source_dataset_sha256"],
            blinded_dataset_sha256=record["blinded_dataset_sha256"],
            experiment_id=record["experiment_id"],
            profile_sha256=record["profile_sha256"],
            simulation_time_us=record["simulation_time_us"],
            cursor=record["cursor"],
            published_prefix_sha256=record["published_prefix_sha256"],
        )


class BlindedCausalFeeder:
    """Privileged feeder that never publishes the full historical future to strategy code."""

    def __init__(
        self,
        *,
        dataset: CausalDataset,
        start_time: datetime | str,
        experiment_id: str,
        shuffle_key_sha256: str,
        profile: BlindingProfile,
        training_cutoff_uncertainty: str,
    ) -> None:
        # Import locally to keep blinding data types independent from feeder internals.
        from .feeder import CausalFeeder

        dataset_snapshot = _snapshot_source_dataset(dataset)
        profile_snapshot = _snapshot_blinding_profile(profile)
        self._blinded = blind_dataset(
            dataset=dataset_snapshot,
            experiment_id=experiment_id,
            shuffle_key_sha256=shuffle_key_sha256,
            profile=profile_snapshot,
            training_cutoff_uncertainty=training_cutoff_uncertainty,
        )
        self._anchor = _source_anchor(dataset_snapshot, profile_snapshot)
        self._source = CausalFeeder(dataset_snapshot, start_time=start_time)
        if self._source.published_count > len(self._blinded.events):
            raise BlindingError("causal source cursor exceeds blinded event set")

    @property
    def published_count(self) -> int:
        return self._source.published_count

    @property
    def simulation_time_us(self) -> int:
        return _microseconds(self._source.simulation_time - self._anchor)

    @property
    def blinded_dataset_sha256(self) -> str:
        return self._blinded.blinded_dataset_sha256

    def advance_to(self, target_time: datetime | str) -> tuple[BlindedEvent, ...]:
        start = self._source.published_count
        self._source.advance_to(target_time)
        end = self._source.published_count
        return self._blinded.events[start:end]

    def advance_next_time(self) -> tuple[BlindedEvent, ...]:
        start = self._source.published_count
        self._source.advance_next_time()
        end = self._source.published_count
        return self._blinded.events[start:end]

    def view(self) -> BlindedDataView:
        cursor = self._source.published_count
        return BlindedDataView(
            simulation_time_us=self.simulation_time_us,
            events=self._blinded.events[:cursor],
        )

    def input_evidence(self) -> BlindedInputEvidence:
        cursor = self._source.published_count
        return BlindedInputEvidence(
            schema_version=_VIEW_SCHEMA_VERSION,
            simulation_time_us=self.simulation_time_us,
            published_prefix_sha256=_blinded_prefix_digest(
                self._blinded.events, cursor
            ),
        )

    def checkpoint(self) -> BlindedFeederCheckpoint:
        cursor = self._source.published_count
        return BlindedFeederCheckpoint(
            schema_version=_CHECKPOINT_SCHEMA_VERSION,
            source_dataset_sha256=self._blinded.source_dataset_sha256,
            blinded_dataset_sha256=self._blinded.blinded_dataset_sha256,
            experiment_id=self._blinded.experiment_id,
            profile_sha256=self._blinded.profile_sha256,
            simulation_time_us=self.simulation_time_us,
            cursor=cursor,
            published_prefix_sha256=_blinded_prefix_digest(
                self._blinded.events, cursor
            ),
        )

    @classmethod
    def restore(
        cls,
        *,
        dataset: CausalDataset,
        checkpoint: BlindedFeederCheckpoint,
        experiment_id: str,
        shuffle_key_sha256: str,
        profile: BlindingProfile,
        training_cutoff_uncertainty: str,
    ) -> "BlindedCausalFeeder":
        if type(checkpoint) is not BlindedFeederCheckpoint:
            raise TypeError("checkpoint must be exact BlindedFeederCheckpoint")
        checkpoint = BlindedFeederCheckpoint.from_record(checkpoint.to_record())
        anchor = _source_anchor(dataset, profile)
        start_time = anchor + timedelta(microseconds=checkpoint.simulation_time_us)
        feeder = cls(
            dataset=dataset,
            start_time=start_time,
            experiment_id=experiment_id,
            shuffle_key_sha256=shuffle_key_sha256,
            profile=profile,
            training_cutoff_uncertainty=training_cutoff_uncertainty,
        )
        if checkpoint.source_dataset_sha256 != feeder._blinded.source_dataset_sha256:
            raise BlindingError("checkpoint source dataset does not match")
        if checkpoint.blinded_dataset_sha256 != feeder._blinded.blinded_dataset_sha256:
            raise BlindingError("checkpoint blinded dataset does not match")
        if checkpoint.experiment_id != feeder._blinded.experiment_id:
            raise BlindingError("checkpoint experiment does not match")
        if checkpoint.profile_sha256 != feeder._blinded.profile_sha256:
            raise BlindingError("checkpoint profile does not match")
        if checkpoint.cursor != feeder.published_count:
            raise BlindingError("checkpoint cursor is not the complete causal prefix")
        expected_prefix = _blinded_prefix_digest(
            feeder._blinded.events, feeder.published_count
        )
        if checkpoint.published_prefix_sha256 != expected_prefix:
            raise BlindingError("checkpoint blinded prefix digest is invalid")
        return feeder
