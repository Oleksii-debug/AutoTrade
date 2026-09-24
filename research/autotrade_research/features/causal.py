"""Causality-preserving feature and label primitives for AutoTrade research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Iterable, Mapping, Sequence


def _time(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
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
        object.__setattr__(self, "symbol", _text(self.symbol, name="symbol"))
        object.__setattr__(
            self,
            "decision_time",
            _time(self.decision_time, name="decision_time"),
        )
        object.__setattr__(self, "value", _decimal(self.value, name="value"))
        input_ids = tuple(_text(value, name="input_id") for value in self.input_ids)
        revisions = tuple(
            _text(value, name="source_revision") for value in self.source_revisions
        )
        if not input_ids:
            raise ValueError("FeaturePoint requires at least one input_id")
        if len(input_ids) != len(revisions):
            raise ValueError("input_ids and source_revisions must have equal length")
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("FeaturePoint input_ids must be unique")
        object.__setattr__(self, "input_ids", input_ids)
        object.__setattr__(self, "source_revisions", revisions)
        object.__setattr__(
            self,
            "feature_name",
            _text(self.feature_name, name="feature_name"),
        )


@dataclass(frozen=True)
class LabelPoint:
    symbol: str
    anchor_time: datetime
    label_available_at: datetime
    value: Decimal
    source_revision: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _text(self.symbol, name="symbol"))
        anchor = _time(self.anchor_time, name="anchor_time")
        available = _time(self.label_available_at, name="label_available_at")
        if available <= anchor:
            raise ValueError("label_available_at must be after anchor_time")
        object.__setattr__(self, "anchor_time", anchor)
        object.__setattr__(self, "label_available_at", available)
        object.__setattr__(self, "value", _decimal(self.value, name="value"))
        object.__setattr__(
            self,
            "source_revision",
            _text(self.source_revision, name="source_revision"),
        )


@dataclass(frozen=True)
class Normalizer:
    mean: Decimal
    scale: Decimal
    fit_cutoff: datetime
    fit_input_ids: tuple[str, ...]
    provenance_hash: str

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
    if feature.decision_time != label.anchor_time:
        raise ValueError("feature decision_time must equal label anchor_time")
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



@dataclass(frozen=True)
class CausalFold:
    """Frozen walk-forward fold with an explicit purge before validation."""

    fold_id: str
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime
    purge_seconds: int

    @classmethod
    def create(
        cls,
        *,
        fold_id: str,
        train_start: datetime,
        train_end: datetime,
        validation_start: datetime,
        validation_end: datetime,
        purge_seconds: int,
    ) -> "CausalFold":
        start = _time(train_start, name="train_start")
        end = _time(train_end, name="train_end")
        validation_from = _time(validation_start, name="validation_start")
        validation_to = _time(validation_end, name="validation_end")
        if start > end:
            raise ValueError("train_start cannot follow train_end")
        if validation_from > validation_to:
            raise ValueError("validation_start cannot follow validation_end")
        if end >= validation_from:
            raise ValueError("training and validation windows must not overlap")
        if (
            isinstance(purge_seconds, bool)
            or not isinstance(purge_seconds, int)
            or purge_seconds < 0
        ):
            raise ValueError("purge_seconds must be a non-negative integer")
        purge_cutoff = validation_from - timedelta(seconds=purge_seconds)
        if purge_cutoff < start:
            raise ValueError("purge removes the entire training window")
        return cls(
            fold_id=_text(fold_id, name="fold_id"),
            train_start=start,
            train_end=end,
            validation_start=validation_from,
            validation_end=validation_to,
            purge_seconds=purge_seconds,
        )

    @property
    def training_information_cutoff(self) -> datetime:
        purge_cutoff = self.validation_start - timedelta(
            seconds=self.purge_seconds
        )
        return min(self.train_end, purge_cutoff)

    @property
    def fingerprint(self) -> str:
        payload = {
            "fold_id": self.fold_id,
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "validation_start": self.validation_start.isoformat(),
            "validation_end": self.validation_end.isoformat(),
            "purge_seconds": self.purge_seconds,
        }
        return "sha256:" + sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class FoldNormalizer:
    fold_id: str
    fold_fingerprint: str
    feature_name: str
    normalizer: Normalizer
    training_point_count: int

    def transform_validation(
        self,
        point: FeaturePoint,
        *,
        fold: CausalFold,
    ) -> Decimal:
        if fold.fold_id != self.fold_id or fold.fingerprint != self.fold_fingerprint:
            raise ValueError("normalizer was fitted for a different fold")
        if point.feature_name != self.feature_name:
            raise ValueError("feature_name differs from fitted normalizer")
        if not (
            fold.validation_start
            <= point.decision_time
            <= fold.validation_end
        ):
            raise ValueError("validation feature lies outside the frozen fold")
        return self.normalizer.transform(point.value)


def fit_fold_normalizer(
    feature_points: Sequence[FeaturePoint],
    *,
    fold: CausalFold,
    feature_name: str,
) -> FoldNormalizer:
    """Fit a normalizer from this fold's purged training window only.

    The caller may pass a complete dataset. This function performs the causal
    selection itself, so validation/test values cannot accidentally influence
    the fitted mean/scale.
    """

    if not isinstance(fold, CausalFold):
        raise TypeError("fold must be CausalFold")
    name = _text(feature_name, name="feature_name")
    selected = [
        point
        for point in feature_points
        if point.feature_name == name
        and fold.train_start
        <= point.decision_time
        <= fold.training_information_cutoff
    ]
    if len(selected) < 2:
        raise ValueError(
            "at least two purged training feature points are required"
        )
    normalizer = fit_normalizer(
        selected,
        fit_cutoff=fold.training_information_cutoff,
    )
    return FoldNormalizer(
        fold_id=fold.fold_id,
        fold_fingerprint=fold.fingerprint,
        feature_name=name,
        normalizer=normalizer,
        training_point_count=len(selected),
    )


def training_rows_for_fold(
    pairs: Sequence[tuple[FeaturePoint, LabelPoint]],
    *,
    fold: CausalFold,
) -> tuple[tuple[Decimal, Decimal], ...]:
    """Return only rows whose feature and label information is inside training.

    A feature anchor inside the purged tail, or a label that became available
    only after the fold's information cutoff, is excluded rather than leaked.
    """

    if not isinstance(fold, CausalFold):
        raise TypeError("fold must be CausalFold")
    cutoff = fold.training_information_cutoff
    rows: list[tuple[Decimal, Decimal]] = []
    for feature, label in pairs:
        if feature.symbol != label.symbol:
            raise ValueError("feature and label symbols differ")
        if label.anchor_time != feature.decision_time:
            raise ValueError("label anchor_time must equal feature decision_time")
        if not (fold.train_start <= feature.decision_time <= cutoff):
            continue
        if label.label_available_at > cutoff:
            continue
        rows.append(
            training_row(
                feature=feature,
                label=label,
                training_cutoff=cutoff,
            )
        )
    if not rows:
        raise ValueError("fold has no causally complete training rows")
    return tuple(rows)


@dataclass(frozen=True)
class CrossMarketPoint:
    decision_time: datetime
    values: Mapping[str, Decimal]
    input_ids: tuple[str, ...]
    source_revisions: tuple[str, ...]
    provenance_hash: str


def causal_cross_market_point(
    observations: Iterable[SourceValue],
    *,
    required_symbols: Sequence[str],
    decision_time: datetime,
    max_age_seconds: int,
) -> CrossMarketPoint:
    """Build an as-of cross-market input without future or stale components."""

    cutoff = _time(decision_time, name="decision_time")
    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, int)
        or max_age_seconds < 0
    ):
        raise ValueError("max_age_seconds must be a non-negative integer")
    symbols = tuple(_text(symbol, name="required_symbol") for symbol in required_symbols)
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("required_symbols must be non-empty and unique")
    selected = require_universe_members(
        symbols,
        observations,
        decision_time=cutoff,
    )
    stale = [
        symbol
        for symbol, item in selected.items()
        if cutoff - item.available_at > timedelta(seconds=max_age_seconds)
    ]
    if stale:
        raise ValueError(
            "stale cross-market inputs: " + ", ".join(sorted(stale))
        )
    ordered = [selected[symbol] for symbol in symbols]
    payload = {
        "decision_time": cutoff.isoformat(),
        "symbols": list(symbols),
        "observations": [
            {
                "observation_id": item.observation_id,
                "available_at": item.available_at.isoformat(),
                "source_revision": item.source_revision,
                "value": str(item.value),
            }
            for item in ordered
        ],
    }
    digest = "sha256:" + sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return CrossMarketPoint(
        decision_time=cutoff,
        values={
            item.symbol: item.value
            for item in ordered
        },
        input_ids=tuple(item.observation_id for item in ordered),
        source_revisions=tuple(item.source_revision for item in ordered),
        provenance_hash=digest,
    )
