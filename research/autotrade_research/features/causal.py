"""Causality-preserving feature and label primitives for AutoTrade research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from typing import Iterable, Mapping, Sequence


def _time(value: datetime, *, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


@dataclass(frozen=True)
class SourceValue:
    observation_id: str
    symbol: str
    event_time: datetime
    available_at: datetime
    value: Decimal
    source_revision: str

    def __post_init__(self) -> None:
        event = _time(self.event_time, name="event_time")
        available = _time(self.available_at, name="available_at")
        if available < event:
            raise ValueError("available_at cannot precede event_time")
        object.__setattr__(self, "observation_id", _text(self.observation_id, name="observation_id"))
        object.__setattr__(self, "symbol", _text(self.symbol, name="symbol"))
        object.__setattr__(self, "event_time", event)
        object.__setattr__(self, "available_at", available)
        object.__setattr__(self, "value", _decimal(self.value, name="value"))
        object.__setattr__(self, "source_revision", _text(self.source_revision, name="source_revision"))

    @classmethod
    def create(
        cls,
        *,
        observation_id: str,
        symbol: str,
        event_time: datetime,
        available_at: datetime,
        value,
        source_revision: str,
    ) -> "SourceValue":
        event = _time(event_time, name="event_time")
        available = _time(available_at, name="available_at")
        if available < event:
            raise ValueError("available_at cannot precede event_time")
        return cls(
            observation_id=_text(observation_id, name="observation_id"),
            symbol=_text(symbol, name="symbol"),
            event_time=event,
            available_at=available,
            value=_decimal(value, name="value"),
            source_revision=_text(source_revision, name="source_revision"),
        )


@dataclass(frozen=True)
class FeaturePoint:
    symbol: str
    decision_time: datetime
    value: Decimal
    input_ids: tuple[str, ...]
    source_revisions: tuple[str, ...]
    feature_name: str

    def __post_init__(self) -> None:
        if isinstance(self.input_ids, (str, bytes)) or not self.input_ids:
            raise ValueError("input_ids must be a non-empty sequence")
        if isinstance(self.source_revisions, (str, bytes)) or not self.source_revisions:
            raise ValueError("source_revisions must be a non-empty sequence")
        input_ids = tuple(_text(value, name="input_id") for value in self.input_ids)
        revisions = tuple(
            _text(value, name="source_revision") for value in self.source_revisions
        )
        if len(input_ids) != len(revisions):
            raise ValueError("input_ids and source_revisions must have equal length")
        object.__setattr__(self, "symbol", _text(self.symbol, name="symbol"))
        object.__setattr__(self, "decision_time", _time(self.decision_time, name="decision_time"))
        object.__setattr__(self, "value", _decimal(self.value, name="value"))
        object.__setattr__(self, "input_ids", input_ids)
        object.__setattr__(self, "source_revisions", revisions)
        object.__setattr__(self, "feature_name", _text(self.feature_name, name="feature_name"))


@dataclass(frozen=True)
class LabelPoint:
    symbol: str
    anchor_time: datetime
    label_available_at: datetime
    value: Decimal
    source_revision: str

    def __post_init__(self) -> None:
        anchor = _time(self.anchor_time, name="anchor_time")
        available = _time(self.label_available_at, name="label_available_at")
        if available <= anchor:
            raise ValueError("label_available_at must be after anchor_time")
        object.__setattr__(self, "symbol", _text(self.symbol, name="symbol"))
        object.__setattr__(self, "anchor_time", anchor)
        object.__setattr__(self, "label_available_at", available)
        object.__setattr__(self, "value", _decimal(self.value, name="value"))
        object.__setattr__(self, "source_revision", _text(self.source_revision, name="source_revision"))


@dataclass(frozen=True)
class Normalizer:
    mean: Decimal
    scale: Decimal
    fit_cutoff: datetime
    fit_input_ids: tuple[str, ...]
    provenance_hash: str

    def __post_init__(self) -> None:
        mean = _decimal(self.mean, name="mean")
        scale = _decimal(self.scale, name="scale")
        if scale <= 0:
            raise ValueError("scale must be positive")
        if isinstance(self.fit_input_ids, (str, bytes)) or not self.fit_input_ids:
            raise ValueError("fit_input_ids must be a non-empty sequence")
        ids = tuple(_text(value, name="fit_input_id") for value in self.fit_input_ids)
        digest = _text(self.provenance_hash, name="provenance_hash")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise ValueError("provenance_hash must be a canonical SHA-256 digest")
        object.__setattr__(self, "mean", mean)
        object.__setattr__(self, "scale", scale)
        object.__setattr__(self, "fit_cutoff", _time(self.fit_cutoff, name="fit_cutoff"))
        object.__setattr__(self, "fit_input_ids", ids)
        object.__setattr__(self, "provenance_hash", digest)

    def transform(self, value) -> Decimal:
        item = _decimal(value, name="value")
        return (item - self.mean) / self.scale


def causal_window(
    observations: Iterable[SourceValue],
    *,
    symbol: str,
    decision_time: datetime,
    count: int,
) -> tuple[SourceValue, ...]:
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ValueError("count must be a positive integer")
    name = _text(symbol, name="symbol")
    cutoff = _time(decision_time, name="decision_time")
    eligible = [
        item
        for item in observations
        if item.symbol == name and item.available_at <= cutoff
    ]
    eligible.sort(key=lambda item: (item.available_at, item.event_time, item.observation_id))
    if len(eligible) < count:
        raise ValueError("insufficient causally available observations")
    return tuple(eligible[-count:])


def rolling_return(
    observations: Iterable[SourceValue],
    *,
    symbol: str,
    decision_time: datetime,
    count: int,
) -> FeaturePoint:
    window = causal_window(
        observations,
        symbol=symbol,
        decision_time=decision_time,
        count=count,
    )
    first = window[0].value
    last = window[-1].value
    if first == 0:
        raise ValueError("rolling return cannot use zero starting value")
    return FeaturePoint(
        symbol=window[-1].symbol,
        decision_time=_time(decision_time, name="decision_time"),
        value=(last / first) - Decimal("1"),
        input_ids=tuple(item.observation_id for item in window),
        source_revisions=tuple(item.source_revision for item in window),
        feature_name=f"rolling_return_{count}",
    )


def fit_normalizer(
    feature_points: Sequence[FeaturePoint],
    *,
    fit_cutoff: datetime,
) -> Normalizer:
    cutoff = _time(fit_cutoff, name="fit_cutoff")
    eligible = [point for point in feature_points if point.decision_time <= cutoff]
    if not eligible:
        raise ValueError("no feature points are available at fit_cutoff")
    if len(eligible) != len(feature_points):
        raise ValueError("normalizer fit input contains observations after fit_cutoff")
    values = [point.value for point in eligible]
    mean = sum(values, Decimal("0")) / Decimal(len(values))
    variance = sum(((value - mean) ** 2 for value in values), Decimal("0")) / Decimal(len(values))
    if variance == 0:
        raise ValueError("normalizer scale would be zero")
    scale = variance.sqrt()
    ids = tuple(item for point in eligible for item in point.input_ids)
    payload = {
        "fit_cutoff": cutoff.isoformat(),
        "feature_names": [point.feature_name for point in eligible],
        "input_ids": ids,
        "source_revisions": [
            revision
            for point in eligible
            for revision in point.source_revisions
        ],
        "mean": str(mean),
        "scale": str(scale),
    }
    digest = "sha256:" + sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return Normalizer(
        mean=mean,
        scale=scale,
        fit_cutoff=cutoff,
        fit_input_ids=ids,
        provenance_hash=digest,
    )


def make_forward_label(
    *,
    symbol: str,
    anchor_time: datetime,
    anchor_value,
    future: SourceValue,
) -> LabelPoint:
    name = _text(symbol, name="symbol")
    anchor = _time(anchor_time, name="anchor_time")
    if future.symbol != name:
        raise ValueError("label source symbol does not match anchor symbol")
    if future.event_time <= anchor:
        raise ValueError("label source must occur after the anchor")
    base = _decimal(anchor_value, name="anchor_value")
    if base == 0:
        raise ValueError("anchor_value cannot be zero")
    return LabelPoint(
        symbol=name,
        anchor_time=anchor,
        label_available_at=future.available_at,
        value=(future.value / base) - Decimal("1"),
        source_revision=future.source_revision,
    )


def training_row(
    *,
    feature: FeaturePoint,
    label: LabelPoint,
    training_cutoff: datetime,
) -> tuple[Decimal, Decimal]:
    cutoff = _time(training_cutoff, name="training_cutoff")
    if feature.symbol != label.symbol:
        raise ValueError("feature and label symbols differ")
    if feature.decision_time > cutoff:
        raise ValueError("feature is not available by training cutoff")
    if label.label_available_at > cutoff:
        raise ValueError("label is delayed beyond training cutoff")
    return feature.value, label.value


def require_universe_members(
    required_symbols: Sequence[str],
    observations: Iterable[SourceValue],
    *,
    decision_time: datetime,
) -> Mapping[str, SourceValue]:
    cutoff = _time(decision_time, name="decision_time")
    latest: dict[str, SourceValue] = {}
    for item in observations:
        if item.available_at > cutoff:
            continue
        current = latest.get(item.symbol)
        if current is None or (item.available_at, item.event_time, item.observation_id) > (
            current.available_at,
            current.event_time,
            current.observation_id,
        ):
            latest[item.symbol] = item
    missing = [symbol for symbol in required_symbols if symbol not in latest]
    if missing:
        raise ValueError("missing required universe members: " + ", ".join(sorted(missing)))
    return {symbol: latest[symbol] for symbol in required_symbols}
