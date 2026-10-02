"""Authoritative WP-10 -> WP-34 bridge for causal fold normalization.

The historical vintage registry remains the source-data authority. This module
only composes its authenticated point-in-time population with the existing
causal feature and fold-normalizer primitives; it does not create a second
dataset registry, replay engine, science registry, or financial authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Context, ROUND_HALF_EVEN, localcontext
from hashlib import sha256
import json
from typing import Iterable, Mapping

from autotrade_research.data.vintages import (
    FrozenMarketPopulation,
    HistoricalVintageRegistry,
)
from autotrade_research.features.causal import (
    CausalFold,
    FeaturePoint,
    FoldNormalizer,
    SourceValue,
    fit_fold_normalizer,
    rolling_return,
)


_SHA256_HEX = frozenset("0123456789abcdef")
_SCIENTIFIC_DECIMAL_POLICY_ID = "WP34_DECIMAL_V1_PREC50_HALF_EVEN"
_DECISION_SCHEDULE_ID = "EACH_ELIGIBLE_SOURCE_KNOWLEDGE_TIME_V1"
_SYMBOL_BASIS_ID = "INSTRUMENT_VERSION_V1"
_SCIENTIFIC_DECIMAL_CONTEXT = Context(
    prec=50,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    capitals=1,
    clamp=0,
)



def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    result = value.strip()
    if result != value:
        raise ValueError(f"{name} must use canonical text")
    return result


def _instant(value: object, *, name: str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"{name} must be timezone-aware")
        return value.astimezone(timezone.utc)
    text = _text(value, name=name)
    if not text.endswith("Z"):
        raise ValueError(f"{name} must be UTC and end in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO date-time") from error
    return parsed.astimezone(timezone.utc)


def _sha256_digest(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if (
        not text.startswith("sha256:")
        or len(text) != 71
        or any(character not in _SHA256_HEX for character in text[7:])
    ):
        raise ValueError(f"{name} must be sha256:<64 lowercase hex>")
    return text


def _replay_common_cut(value: object | None) -> str | None:
    if value is None:
        return None
    text = _text(value, name="replay_common_cut_fingerprint")
    if (
        len(text) != 64
        or any(character not in _SHA256_HEX for character in text)
    ):
        raise ValueError(
            "replay_common_cut_fingerprint must be 64 lowercase SHA-256 hex"
        )
    return text


def _canonical_hash(material: Mapping[str, object]) -> str:
    return "sha256:" + sha256(
        json.dumps(
            dict(material),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class HistoricalFeatureInputSpec:
    """Versioned deterministic mapping from historical market rows to a feature."""

    spec_id: str
    payload_value_field: str
    window_count: int
    event_kinds: tuple[str, ...] = ("BAR", "TRADE")
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        object.__setattr__(self, "spec_id", _text(self.spec_id, name="spec_id"))
        object.__setattr__(
            self,
            "payload_value_field",
            _text(self.payload_value_field, name="payload_value_field"),
        )
        if (
            isinstance(self.window_count, bool)
            or not isinstance(self.window_count, int)
            or self.window_count < 2
        ):
            raise ValueError("window_count must be an integer >= 2")
        kinds = tuple(_text(kind, name="event_kind") for kind in self.event_kinds)
        if not kinds or len(set(kinds)) != len(kinds):
            raise ValueError("event_kinds must be non-empty and unique")
        object.__setattr__(self, "event_kinds", kinds)
        if self.schema_version != "1.0.0":
            raise ValueError("unsupported historical feature input spec schema")

    @property
    def feature_name(self) -> str:
        return f"rolling_return_{self.window_count}"

    @property
    def fingerprint(self) -> str:
        return _canonical_hash(
            {
                "schema_version": self.schema_version,
                "spec_id": self.spec_id,
                "payload_value_field": self.payload_value_field,
                "window_count": self.window_count,
                "event_kinds": list(self.event_kinds),
                "feature_name": self.feature_name,
                "decimal_policy_id": _SCIENTIFIC_DECIMAL_POLICY_ID,
                "decision_schedule_id": _DECISION_SCHEDULE_ID,
                "symbol_basis_id": _SYMBOL_BASIS_ID,
            }
        )


def _knowledge_time(row: Mapping[str, object]) -> datetime:
    evidence = row.get("raw_evidence_ref")
    if not isinstance(evidence, Mapping):
        raise ValueError("historical row lacks raw_evidence_ref")
    return max(
        _instant(row.get("available_at"), name="available_at"),
        _instant(row.get("ingested_at"), name="ingested_at"),
        _instant(evidence.get("observed_at"), name="raw evidence observed_at"),
    )


def authoritative_source_values(
    population: FrozenMarketPopulation,
    *,
    spec: HistoricalFeatureInputSpec,
) -> tuple[SourceValue, ...]:
    """Derive immutable SourceValue inputs from a registry-issued population."""

    if type(population) is not FrozenMarketPopulation:
        raise TypeError("population must be FrozenMarketPopulation")
    if type(spec) is not HistoricalFeatureInputSpec:
        raise TypeError("spec must be HistoricalFeatureInputSpec")

    result: list[SourceValue] = []
    for row in population.events():
        kind = _text(row.get("kind"), name="market event kind")
        if kind not in spec.event_kinds:
            continue
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("historical market event payload must be an object")
        if spec.payload_value_field not in payload:
            raise ValueError(
                "historical market event payload lacks configured feature value"
            )
        revision = row.get("revision")
        if isinstance(revision, bool):
            raise ValueError("historical event revision must be a positive integer")
        try:
            revision_int = int(revision)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "historical event revision must be a positive integer"
            ) from error
        if revision_int < 1 or str(revision_int) != str(revision):
            raise ValueError("historical event revision must be canonical")
        event_id = _text(row.get("event_id"), name="event_id")
        result.append(
            SourceValue.create(
                observation_id=f"{event_id}@r{revision_int}",
                symbol=_text(
                    row.get("instrument_version"),
                    name="instrument_version",
                ),
                event_time=_instant(
                    row.get("source_event_at"),
                    name="source_event_at",
                ),
                available_at=_knowledge_time(row),
                value=payload[spec.payload_value_field],
                source_revision=str(revision_int),
            )
        )
    if not result:
        raise ValueError("authoritative population has no events for feature spec")
    result.sort(
        key=lambda item: (
            item.available_at,
            item.event_time,
            item.symbol,
            item.observation_id,
        )
    )
    return tuple(result)


def authoritative_feature_points(
    population: FrozenMarketPopulation,
    *,
    spec: HistoricalFeatureInputSpec,
) -> tuple[FeaturePoint, ...]:
    """Derive deterministic rolling-return points from authenticated source rows."""

    sources = authoritative_source_values(population, spec=spec)
    points: list[FeaturePoint] = []
    seen: set[tuple[object, ...]] = set()
    for source in sources:
        try:
            # Rolling division is statistical arithmetic, not financial-money
            # authority, but it still must be reproducible. Use one explicit
            # policy instead of whichever Decimal context a caller left active.
            with localcontext(_SCIENTIFIC_DECIMAL_CONTEXT):
                point = rolling_return(
                    sources,
                    symbol=source.symbol,
                    decision_time=source.available_at,
                    count=spec.window_count,
                )
        except ValueError as error:
            if str(error) == "insufficient causally available observations":
                continue
            raise
        identity = (
            point.symbol,
            point.decision_time,
            point.feature_name,
            point.input_ids,
            point.source_revisions,
            point.value,
        )
        if identity in seen:
            continue
        seen.add(identity)
        points.append(point)
    if not points:
        raise ValueError("authoritative population cannot produce feature points")
    points.sort(
        key=lambda point: (
            point.decision_time,
            point.symbol,
            point.input_ids,
        )
    )
    return tuple(points)


def resolve_authoritative_feature_points(
    *,
    registry: HistoricalVintageRegistry,
    dataset_id: str,
    dataset_version: int,
    manifest_digest: str,
    events: Iterable[Mapping[str, object]],
    cutoff: datetime,
    spec: HistoricalFeatureInputSpec,
) -> tuple[FeaturePoint, ...]:
    """Resolve a causal dataset cut then derive feature points from its exact bytes."""

    if type(registry) is not HistoricalVintageRegistry:
        raise TypeError("registry must be HistoricalVintageRegistry")
    population = registry.resolve_market_population(
        dataset_id,
        dataset_version,
        manifest_digest=manifest_digest,
        events=tuple(events),
        cutoff=cutoff,
    )
    return authoritative_feature_points(population, spec=spec)


@dataclass(frozen=True)
class AuthoritativeFoldNormalizer:
    """Fold normalizer bound to a registry-authenticated training population."""

    dataset_id: str
    dataset_version: int
    manifest_digest: str
    training_population_fingerprint: str
    feature_spec_fingerprint: str
    replay_common_cut_fingerprint: str | None
    fold_normalizer: FoldNormalizer
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_id", _text(self.dataset_id, name="dataset_id"))
        if (
            isinstance(self.dataset_version, bool)
            or not isinstance(self.dataset_version, int)
            or self.dataset_version < 1
        ):
            raise ValueError("dataset_version must be a positive integer")
        object.__setattr__(
            self,
            "manifest_digest",
            _sha256_digest(self.manifest_digest, name="manifest_digest"),
        )
        object.__setattr__(
            self,
            "training_population_fingerprint",
            _sha256_digest(
                self.training_population_fingerprint,
                name="training_population_fingerprint",
            ),
        )
        object.__setattr__(
            self,
            "feature_spec_fingerprint",
            _sha256_digest(
                self.feature_spec_fingerprint,
                name="feature_spec_fingerprint",
            ),
        )
        object.__setattr__(
            self,
            "replay_common_cut_fingerprint",
            _replay_common_cut(self.replay_common_cut_fingerprint),
        )
        if type(self.fold_normalizer) is not FoldNormalizer:
            raise TypeError("fold_normalizer must be FoldNormalizer")
        if self.schema_version != "1.0.0":
            raise ValueError("unsupported authoritative fold-normalizer schema")

    @property
    def fingerprint(self) -> str:
        normalizer = self.fold_normalizer.normalizer
        return _canonical_hash(
            {
                "schema_version": self.schema_version,
                "dataset_id": self.dataset_id,
                "dataset_version": self.dataset_version,
                "manifest_digest": self.manifest_digest,
                "training_population_fingerprint": self.training_population_fingerprint,
                "feature_spec_fingerprint": self.feature_spec_fingerprint,
                "replay_common_cut_fingerprint": self.replay_common_cut_fingerprint,
                "fold_id": self.fold_normalizer.fold_id,
                "fold_fingerprint": self.fold_normalizer.fold_fingerprint,
                "feature_name": self.fold_normalizer.feature_name,
                "training_point_count": self.fold_normalizer.training_point_count,
                "normalizer": {
                    "mean": str(normalizer.mean),
                    "scale": str(normalizer.scale),
                    "fit_cutoff": normalizer.fit_cutoff.isoformat(),
                    "fit_input_ids": list(normalizer.fit_input_ids),
                    "provenance_hash": normalizer.provenance_hash,
                },
            }
        )

    def transform_validation(
        self,
        point: FeaturePoint,
        *,
        fold: CausalFold,
        registry: HistoricalVintageRegistry,
        events: Iterable[Mapping[str, object]],
        spec: HistoricalFeatureInputSpec,
        replay_common_cut_fingerprint: str | None = None,
    ):
        """Re-resolve training + validation source truth before transforming.

        The supplied FeaturePoint is an assertion/cache only. The exact
        validation point is independently re-derived from the authenticated
        dataset bytes and must match before its value is transformed.
        """

        if type(point) is not FeaturePoint:
            raise TypeError("point must be FeaturePoint")
        if type(fold) is not CausalFold:
            raise TypeError("fold must be CausalFold")
        if type(registry) is not HistoricalVintageRegistry:
            raise TypeError("registry must be HistoricalVintageRegistry")
        if type(spec) is not HistoricalFeatureInputSpec:
            raise TypeError("spec must be HistoricalFeatureInputSpec")
        if fold.fingerprint != self.fold_normalizer.fold_fingerprint:
            raise ValueError("normalizer was fitted for a different fold")
        if spec.fingerprint != self.feature_spec_fingerprint:
            raise ValueError("feature input specification differs from fitted authority")
        if _replay_common_cut(replay_common_cut_fingerprint) != self.replay_common_cut_fingerprint:
            raise ValueError("replay common-cut identity differs from fitted authority")

        raw_events = tuple(events)
        training_population = registry.resolve_market_population(
            self.dataset_id,
            self.dataset_version,
            manifest_digest=self.manifest_digest,
            events=raw_events,
            cutoff=fold.training_information_cutoff,
        )
        if training_population.fingerprint != self.training_population_fingerprint:
            raise ValueError("authoritative training population differs from fitted authority")

        recomputed = _fit_from_population(
            training_population,
            fold=fold,
            spec=spec,
        )
        if recomputed != self.fold_normalizer:
            raise ValueError("stored fold normalizer differs from authoritative recomputation")

        validation_population = registry.resolve_market_population(
            self.dataset_id,
            self.dataset_version,
            manifest_digest=self.manifest_digest,
            events=raw_events,
            cutoff=point.decision_time,
        )
        candidates = [
            candidate
            for candidate in authoritative_feature_points(
                validation_population,
                spec=spec,
            )
            if (
                candidate.symbol == point.symbol
                and candidate.decision_time == point.decision_time
                and candidate.feature_name == point.feature_name
            )
        ]
        if len(candidates) != 1:
            raise ValueError(
                "validation point is not uniquely derivable from authoritative population"
            )
        if candidates[0] != point:
            raise ValueError("caller validation point differs from authoritative population")
        with localcontext(_SCIENTIFIC_DECIMAL_CONTEXT):
            return self.fold_normalizer.transform_validation(point, fold=fold)


def _fit_from_population(
    population: FrozenMarketPopulation,
    *,
    fold: CausalFold,
    spec: HistoricalFeatureInputSpec,
) -> FoldNormalizer:
    points = authoritative_feature_points(population, spec=spec)
    with localcontext(_SCIENTIFIC_DECIMAL_CONTEXT):
        return fit_fold_normalizer(
            points,
            fold=fold,
            feature_name=spec.feature_name,
        )


def fit_authoritative_fold_normalizer(
    *,
    registry: HistoricalVintageRegistry,
    dataset_id: str,
    dataset_version: int,
    manifest_digest: str,
    events: Iterable[Mapping[str, object]],
    fold: CausalFold,
    spec: HistoricalFeatureInputSpec,
    replay_common_cut_fingerprint: str | None = None,
) -> AuthoritativeFoldNormalizer:
    """Fit WP-34 normalization only from WP-10 authenticated source truth."""

    if type(registry) is not HistoricalVintageRegistry:
        raise TypeError("registry must be HistoricalVintageRegistry")
    if type(fold) is not CausalFold:
        raise TypeError("fold must be CausalFold")
    if type(spec) is not HistoricalFeatureInputSpec:
        raise TypeError("spec must be HistoricalFeatureInputSpec")
    population = registry.resolve_market_population(
        dataset_id,
        dataset_version,
        manifest_digest=manifest_digest,
        events=tuple(events),
        cutoff=fold.training_information_cutoff,
    )
    fitted = _fit_from_population(population, fold=fold, spec=spec)
    return AuthoritativeFoldNormalizer(
        dataset_id=population.dataset_id,
        dataset_version=population.version,
        manifest_digest=population.manifest_digest,
        training_population_fingerprint=population.fingerprint,
        feature_spec_fingerprint=spec.fingerprint,
        replay_common_cut_fingerprint=_replay_common_cut(
            replay_common_cut_fingerprint
        ),
        fold_normalizer=fitted,
    )
