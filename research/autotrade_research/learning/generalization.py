"""Cross-market/regime training-school evidence for product Section 20.

This module composes existing causal population evidence.  It does not train a
model, choose a strategy, promote a candidate, or claim economic edge.

The purpose is narrower: prove that a candidate's learning/evaluation school
covers explicitly registered asset-class/regime cells, that each cell is backed
by a complete canonical population, and that asset-specific feature namespaces
remain present instead of being erased by a pooled "universal" representation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Mapping, Sequence

from mvp.autotrade_mvp.exact_decimal import (
    ExactDecimalError,
    canonical_decimal_text,
)

from .population_coverage import PopulationCoverageManifest


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_CELL_STATUS = frozenset({"PASS", "FAIL", "INCONCLUSIVE"})


class CrossMarketGeneralizationError(ValueError):
    """Raised when cross-market/regime evidence is malformed or contradictory."""


def _text(value: str, *, name: str) -> str:
    if type(value) is not str or value != value.strip() or not value:
        raise CrossMarketGeneralizationError(
            f"{name} must be non-empty exact text"
        )
    return value


def _sha(value: str, *, name: str) -> str:
    value = _text(value, name=name)
    if _SHA256.fullmatch(value) is None:
        raise CrossMarketGeneralizationError(
            f"{name} must be canonical sha256:<64 lowercase hex>"
        )
    return value


def _git_sha(value: str, *, name: str) -> str:
    value = _text(value, name=name)
    if _GIT_SHA.fullmatch(value) is None:
        raise CrossMarketGeneralizationError(
            f"{name} must be a 40-character lowercase git SHA"
        )
    return value


def _decimal(value: object, *, name: str) -> Decimal:
    if type(value) not in {Decimal, str, int}:
        raise TypeError(
            f"{name} must use exact Decimal, string or integer input"
        )
    try:
        result = value if type(value) is Decimal else Decimal(value)
        canonical_decimal_text(result)
    except (InvalidOperation, ValueError, TypeError, ExactDecimalError) as error:
        raise CrossMarketGeneralizationError(
            f"{name} must be a finite exact decimal"
        ) from error
    return result


def _canonical(value: object, *, path: str = "value") -> object:
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is Decimal:
        return canonical_decimal_text(value)
    if type(value) is dict:
        normalized: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            key = _text(raw_key, name=f"{path} key")
            if key in normalized:
                raise CrossMarketGeneralizationError(
                    f"{path} contains duplicate keys"
                )
            normalized[key] = _canonical(
                raw_value,
                path=f"{path}.{key}",
            )
        return {key: normalized[key] for key in sorted(normalized)}
    if type(value) in {tuple, list}:
        return [
            _canonical(item, path=f"{path}[]")
            for item in value
        ]
    raise TypeError(
        f"{path} contains unsupported type {type(value).__name__}"
    )


def _digest(value: object) -> str:
    payload = json.dumps(
        _canonical(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + sha256(payload).hexdigest()


def _canonical_text_tuple(
    values: tuple[str, ...],
    *,
    name: str,
    require_nonempty: bool = True,
) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise TypeError(f"{name} must be an exact tuple")
    normalized = tuple(_text(value, name=name) for value in values)
    if require_nonempty and not normalized:
        raise CrossMarketGeneralizationError(f"{name} must not be empty")
    if tuple(sorted(set(normalized))) != normalized:
        raise CrossMarketGeneralizationError(
            f"{name} must be sorted and unique"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class AssetClassProfile:
    """Registered asset-specific semantics that pooled learning may not erase."""

    asset_class: str
    instrument_families: tuple[str, ...]
    specialized_feature_namespaces: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "asset_class",
            _text(self.asset_class, name="asset_class"),
        )
        object.__setattr__(
            self,
            "instrument_families",
            _canonical_text_tuple(
                self.instrument_families,
                name="instrument_families",
            ),
        )
        object.__setattr__(
            self,
            "specialized_feature_namespaces",
            _canonical_text_tuple(
                self.specialized_feature_namespaces,
                name="specialized_feature_namespaces",
            ),
        )

    @classmethod
    def create(
        cls,
        *,
        asset_class: str,
        instrument_families: Sequence[str],
        specialized_feature_namespaces: Sequence[str],
    ) -> "AssetClassProfile":
        if type(instrument_families) not in {tuple, list}:
            raise TypeError("instrument_families must be an exact tuple or list")
        if type(specialized_feature_namespaces) not in {tuple, list}:
            raise TypeError(
                "specialized_feature_namespaces must be an exact tuple or list"
            )
        return cls(
            asset_class=_text(asset_class, name="asset_class"),
            instrument_families=tuple(
                sorted(
                    _text(item, name="instrument_family")
                    for item in instrument_families
                )
            ),
            specialized_feature_namespaces=tuple(
                sorted(
                    _text(item, name="specialized_feature_namespace")
                    for item in specialized_feature_namespaces
                )
            ),
        )


@dataclass(frozen=True, slots=True, order=True)
class MarketRegimeCell:
    """One explicitly registered asset-class/regime evaluation cell."""

    asset_class: str
    regime: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "asset_class",
            _text(self.asset_class, name="asset_class"),
        )
        object.__setattr__(
            self,
            "regime",
            _text(self.regime, name="regime"),
        )

    @property
    def key(self) -> str:
        return f"{self.asset_class}::{self.regime}"


@dataclass(frozen=True, slots=True)
class CrossMarketTrainingProtocol:
    """Frozen coverage contract for multi-asset, multi-regime learning evidence."""

    protocol_id: str
    candidate_hash: str
    population_protocol_hash: str
    exact_build_sha: str
    asset_profiles: tuple[AssetClassProfile, ...]
    required_cells: tuple[MarketRegimeCell, ...]
    min_observations_per_cell: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "protocol_id",
            _text(self.protocol_id, name="protocol_id"),
        )
        object.__setattr__(
            self,
            "candidate_hash",
            _sha(self.candidate_hash, name="candidate_hash"),
        )
        object.__setattr__(
            self,
            "population_protocol_hash",
            _sha(
                self.population_protocol_hash,
                name="population_protocol_hash",
            ),
        )
        object.__setattr__(
            self,
            "exact_build_sha",
            _git_sha(self.exact_build_sha, name="exact_build_sha"),
        )
        if type(self.asset_profiles) is not tuple:
            raise TypeError("asset_profiles must be an exact tuple")
        if len(self.asset_profiles) < 2:
            raise CrossMarketGeneralizationError(
                "cross-market protocol requires at least two asset classes"
            )
        profiles = []
        classes: set[str] = set()
        family_owner: dict[str, str] = {}
        for item in self.asset_profiles:
            if type(item) is not AssetClassProfile:
                raise TypeError(
                    "asset_profiles must contain exact AssetClassProfile"
                )
            clean = AssetClassProfile(
                asset_class=item.asset_class,
                instrument_families=item.instrument_families,
                specialized_feature_namespaces=
                    item.specialized_feature_namespaces,
            )
            if clean.asset_class in classes:
                raise CrossMarketGeneralizationError(
                    f"duplicate asset class: {clean.asset_class}"
                )
            classes.add(clean.asset_class)
            for family in clean.instrument_families:
                prior = family_owner.setdefault(family, clean.asset_class)
                if prior != clean.asset_class:
                    raise CrossMarketGeneralizationError(
                        "instrument family cannot belong to multiple "
                        "asset-class profiles"
                    )
            profiles.append(clean)
        if tuple(sorted(profiles, key=lambda x: x.asset_class)) != tuple(profiles):
            raise CrossMarketGeneralizationError(
                "asset_profiles must use canonical asset-class ordering"
            )

        if type(self.required_cells) is not tuple:
            raise TypeError("required_cells must be an exact tuple")
        if not self.required_cells:
            raise CrossMarketGeneralizationError(
                "required_cells must not be empty"
            )
        cells = []
        seen: set[tuple[str, str]] = set()
        for item in self.required_cells:
            if type(item) is not MarketRegimeCell:
                raise TypeError(
                    "required_cells must contain exact MarketRegimeCell"
                )
            clean = MarketRegimeCell(item.asset_class, item.regime)
            identity = (clean.asset_class, clean.regime)
            if identity in seen:
                raise CrossMarketGeneralizationError(
                    f"duplicate required cell: {clean.key}"
                )
            if clean.asset_class not in classes:
                raise CrossMarketGeneralizationError(
                    f"required cell references unknown asset class: "
                    f"{clean.asset_class}"
                )
            seen.add(identity)
            cells.append(clean)
        if tuple(sorted(cells)) != tuple(cells):
            raise CrossMarketGeneralizationError(
                "required_cells must use canonical ordering"
            )

        covered_classes = {item.asset_class for item in cells}
        if covered_classes != classes:
            raise CrossMarketGeneralizationError(
                "every asset class requires at least one registered regime cell"
            )
        if len({item.regime for item in cells}) < 2:
            raise CrossMarketGeneralizationError(
                "training school requires at least two distinct market regimes"
            )
        if (
            type(self.min_observations_per_cell) is not int
            or self.min_observations_per_cell <= 0
        ):
            raise CrossMarketGeneralizationError(
                "min_observations_per_cell must be a positive exact integer"
            )

        object.__setattr__(self, "asset_profiles", tuple(profiles))
        object.__setattr__(self, "required_cells", tuple(cells))

    @classmethod
    def create(
        cls,
        *,
        protocol_id: str,
        candidate_hash: str,
        population_protocol_hash: str,
        exact_build_sha: str,
        asset_profiles: Sequence[AssetClassProfile],
        required_cells: Sequence[MarketRegimeCell],
        min_observations_per_cell: int,
    ) -> "CrossMarketTrainingProtocol":
        if type(asset_profiles) not in {tuple, list}:
            raise TypeError("asset_profiles must be an exact tuple or list")
        if type(required_cells) not in {tuple, list}:
            raise TypeError("required_cells must be an exact tuple or list")
        profiles_raw = tuple(asset_profiles)
        if any(type(item) is not AssetClassProfile for item in profiles_raw):
            raise TypeError(
                "asset_profiles must contain exact AssetClassProfile"
            )
        cells_raw = tuple(required_cells)
        if any(type(item) is not MarketRegimeCell for item in cells_raw):
            raise TypeError(
                "required_cells must contain exact MarketRegimeCell"
            )
        profiles = tuple(
            sorted(profiles_raw, key=lambda item: item.asset_class)
        )
        cells = tuple(sorted(cells_raw))
        return cls(
            protocol_id=protocol_id,
            candidate_hash=candidate_hash,
            population_protocol_hash=population_protocol_hash,
            exact_build_sha=exact_build_sha,
            asset_profiles=profiles,
            required_cells=cells,
            min_observations_per_cell=min_observations_per_cell,
        )

    @property
    def digest(self) -> str:
        return _digest(
            {
                "schema_version": 1,
                "protocol_id": self.protocol_id,
                "candidate_hash": self.candidate_hash,
                "population_protocol_hash":
                    self.population_protocol_hash,
                "exact_build_sha": self.exact_build_sha,
                "asset_profiles": tuple(
                    {
                        "asset_class": item.asset_class,
                        "instrument_families": item.instrument_families,
                        "specialized_feature_namespaces":
                            item.specialized_feature_namespaces,
                    }
                    for item in self.asset_profiles
                ),
                "required_cells": tuple(
                    {
                        "asset_class": item.asset_class,
                        "regime": item.regime,
                    }
                    for item in self.required_cells
                ),
                "min_observations_per_cell":
                    self.min_observations_per_cell,
            }
        )


@dataclass(frozen=True, slots=True)
class MarketRegimeEvidence:
    """Execution-adjusted evidence for one registered market/regime cell."""

    asset_class: str
    regime: str
    exact_build_sha: str
    population: PopulationCoverageManifest
    execution_adjusted_net_score: Decimal
    observations: int
    costs_complete: bool
    observed_feature_namespaces: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "asset_class",
            _text(self.asset_class, name="asset_class"),
        )
        object.__setattr__(
            self,
            "regime",
            _text(self.regime, name="regime"),
        )
        object.__setattr__(
            self,
            "exact_build_sha",
            _git_sha(self.exact_build_sha, name="exact_build_sha"),
        )
        if type(self.population) is not PopulationCoverageManifest:
            raise TypeError(
                "population must be exact PopulationCoverageManifest"
            )
        if (
            type(self.observations) is not int
            or self.observations <= 0
        ):
            raise CrossMarketGeneralizationError(
                "observations must be a positive exact integer"
            )
        if type(self.costs_complete) is not bool:
            raise TypeError("costs_complete must be boolean")
        object.__setattr__(
            self,
            "execution_adjusted_net_score",
            _decimal(
                self.execution_adjusted_net_score,
                name="execution_adjusted_net_score",
            ),
        )
        object.__setattr__(
            self,
            "observed_feature_namespaces",
            _canonical_text_tuple(
                self.observed_feature_namespaces,
                name="observed_feature_namespaces",
            ),
        )

    @classmethod
    def create(
        cls,
        *,
        asset_class: str,
        regime: str,
        exact_build_sha: str,
        population: PopulationCoverageManifest,
        execution_adjusted_net_score: object,
        observations: int,
        costs_complete: bool,
        observed_feature_namespaces: Sequence[str],
    ) -> "MarketRegimeEvidence":
        if type(observed_feature_namespaces) not in {tuple, list}:
            raise TypeError(
                "observed_feature_namespaces must be an exact tuple or list"
            )
        return cls(
            asset_class=asset_class,
            regime=regime,
            exact_build_sha=exact_build_sha,
            population=population,
            execution_adjusted_net_score=_decimal(
                execution_adjusted_net_score,
                name="execution_adjusted_net_score",
            ),
            observations=observations,
            costs_complete=costs_complete,
            observed_feature_namespaces=tuple(
                sorted(
                    _text(item, name="observed_feature_namespace")
                    for item in observed_feature_namespaces
                )
            ),
        )

    @property
    def cell(self) -> MarketRegimeCell:
        return MarketRegimeCell(self.asset_class, self.regime)

    @property
    def digest(self) -> str:
        return _digest(
            {
                "schema_version": 1,
                "asset_class": self.asset_class,
                "regime": self.regime,
                "exact_build_sha": self.exact_build_sha,
                "population_digest": self.population.digest,
                "execution_adjusted_net_score":
                    self.execution_adjusted_net_score,
                "observations": self.observations,
                "costs_complete": self.costs_complete,
                "observed_feature_namespaces":
                    self.observed_feature_namespaces,
            }
        )


def _detach_population(
    value: PopulationCoverageManifest,
) -> PopulationCoverageManifest:
    if type(value) is not PopulationCoverageManifest:
        raise TypeError(
            "population must be exact PopulationCoverageManifest"
        )
    return PopulationCoverageManifest(
        candidate_hash=value.candidate_hash,
        frozen_protocol_hash=value.frozen_protocol_hash,
        input_snapshot_hash=value.input_snapshot_hash,
        causal_cutoff=value.causal_cutoff,
        permission_classes=value.permission_classes,
        task=value.task,
        instrument_family=value.instrument_family,
        eligible_episode_ids=value.eligible_episode_ids,
        included_episode_ids=value.included_episode_ids,
        exclusions=value.exclusions,
        episode_digests=value.episode_digests,
        eligible_outcomes=value.eligible_outcomes,
        included_outcomes=value.included_outcomes,
        eligible_no_trade_count=value.eligible_no_trade_count,
        included_no_trade_count=value.included_no_trade_count,
        included_regime_counts=value.included_regime_counts,
        included_labels_complete_by_regime=
            value.included_labels_complete_by_regime,
        digest=value.digest,
    )


def _detach_evidence(value: MarketRegimeEvidence) -> MarketRegimeEvidence:
    if type(value) is not MarketRegimeEvidence:
        raise TypeError(
            "evidence must contain exact MarketRegimeEvidence"
        )
    return MarketRegimeEvidence(
        asset_class=value.asset_class,
        regime=value.regime,
        exact_build_sha=value.exact_build_sha,
        population=_detach_population(value.population),
        execution_adjusted_net_score=value.execution_adjusted_net_score,
        observations=value.observations,
        costs_complete=value.costs_complete,
        observed_feature_namespaces=value.observed_feature_namespaces,
    )


@dataclass(frozen=True, slots=True)
class CrossMarketGeneralizationAssessment:
    """Coverage/comparability result; never an economic-edge verdict."""

    status: str
    reasons: tuple[str, ...]
    cell_statuses: Mapping[str, str]
    protocol_sha256: str
    evidence_sha256: str
    economic_edge_status: str = "NOT_ESTABLISHED"
    regime_routing_status: str = "NOT_ESTABLISHED"
    strategy_comparison_status: str = "NOT_ESTABLISHED"
    grants_trading_authority: bool = False

    def __post_init__(self) -> None:
        status = _text(self.status, name="status")
        if status not in _CELL_STATUS:
            raise CrossMarketGeneralizationError(
                "status must be PASS, FAIL or INCONCLUSIVE"
            )
        if type(self.reasons) is not tuple:
            raise TypeError("reasons must be an exact tuple")
        reasons = tuple(
            _text(item, name="reason")
            for item in self.reasons
        )
        if not reasons:
            raise CrossMarketGeneralizationError(
                "assessment requires at least one reason"
            )
        if type(self.cell_statuses) not in {dict, MappingProxyType}:
            raise TypeError("cell_statuses must be an exact dict or mapping proxy")
        statuses: dict[str, str] = {}
        for raw_key, raw_status in self.cell_statuses.items():
            key = _text(raw_key, name="cell_status key")
            value = _text(raw_status, name=f"cell status {key}")
            if value not in _CELL_STATUS:
                raise CrossMarketGeneralizationError(
                    f"unsupported cell status: {value}"
                )
            if key in statuses:
                raise CrossMarketGeneralizationError(
                    f"duplicate cell status: {key}"
                )
            statuses[key] = value
        if not statuses:
            raise CrossMarketGeneralizationError(
                "cell_statuses must not be empty"
            )
        for name, value in (
            ("economic_edge_status", self.economic_edge_status),
            ("regime_routing_status", self.regime_routing_status),
            ("strategy_comparison_status", self.strategy_comparison_status),
        ):
            if type(value) is not str or value != "NOT_ESTABLISHED":
                raise CrossMarketGeneralizationError(
                    f"{name} must remain NOT_ESTABLISHED for coverage evidence"
                )
        if type(self.grants_trading_authority) is not bool:
            raise TypeError("grants_trading_authority must be boolean")
        if self.grants_trading_authority:
            raise CrossMarketGeneralizationError(
                "cross-market coverage cannot grant trading authority"
            )
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(
            self,
            "cell_statuses",
            MappingProxyType(
                {key: statuses[key] for key in sorted(statuses)}
            ),
        )
        object.__setattr__(
            self,
            "protocol_sha256",
            _sha(self.protocol_sha256, name="protocol_sha256"),
        )
        object.__setattr__(
            self,
            "evidence_sha256",
            _sha(self.evidence_sha256, name="evidence_sha256"),
        )

    @property
    def coverage_ready(self) -> bool:
        """Coverage/comparability only; never routing, edge, or trading authority."""
        return self.status == "PASS"


def assess_cross_market_generalization(
    protocol: CrossMarketTrainingProtocol,
    evidence: Sequence[MarketRegimeEvidence],
) -> CrossMarketGeneralizationAssessment:
    """Assess registered cross-market/regime coverage without scoring edge."""

    if type(protocol) is not CrossMarketTrainingProtocol:
        raise TypeError(
            "protocol must be exact CrossMarketTrainingProtocol"
        )
    protocol = CrossMarketTrainingProtocol(
        protocol_id=protocol.protocol_id,
        candidate_hash=protocol.candidate_hash,
        population_protocol_hash=protocol.population_protocol_hash,
        exact_build_sha=protocol.exact_build_sha,
        asset_profiles=protocol.asset_profiles,
        required_cells=protocol.required_cells,
        min_observations_per_cell=protocol.min_observations_per_cell,
    )
    if type(evidence) not in {tuple, list}:
        raise TypeError("evidence must be an exact tuple or list")
    records = tuple(_detach_evidence(item) for item in evidence)

    profiles = {
        item.asset_class: item
        for item in protocol.asset_profiles
    }
    required = {
        item.key: item
        for item in protocol.required_cells
    }
    supplied: dict[str, MarketRegimeEvidence] = {}
    failures: list[str] = []
    incomplete: list[str] = []
    statuses: dict[str, str] = {}

    for item in records:
        key = item.cell.key
        if key in supplied:
            raise CrossMarketGeneralizationError(
                f"duplicate evidence cell: {key}"
            )
        supplied[key] = item
        if key not in required:
            failures.append(f"unregistered evidence cell: {key}")
            statuses[key] = "FAIL"

    population_digests = [
        item.population.digest for item in records
    ]
    if len(population_digests) != len(set(population_digests)):
        failures.append(
            "one population manifest cannot substitute for multiple cells"
        )

    for key, cell in required.items():
        item = supplied.get(key)
        if item is None:
            incomplete.append(f"missing required cell: {key}")
            statuses[key] = "INCONCLUSIVE"
            continue

        profile = profiles[cell.asset_class]
        cell_failures: list[str] = []
        cell_incomplete: list[str] = []
        population = item.population

        if item.exact_build_sha != protocol.exact_build_sha:
            cell_failures.append("evidence exact build mismatch")
        if population.candidate_hash != protocol.candidate_hash:
            cell_failures.append("population candidate hash mismatch")
        if (
            population.frozen_protocol_hash
            != protocol.population_protocol_hash
        ):
            cell_failures.append("population frozen protocol mismatch")
        if population.instrument_family not in profile.instrument_families:
            cell_failures.append(
                "population instrument family is not registered for asset class"
            )

        expected_regime_counts = ((cell.regime, item.observations),)
        if population.included_regime_counts != expected_regime_counts:
            cell_failures.append(
                "population regime/count does not match evidence cell"
            )
        label_rows = population.included_labels_complete_by_regime
        if len(label_rows) != 1 or label_rows[0][0] != cell.regime:
            cell_failures.append(
                "population label regime does not match evidence cell"
            )
        elif label_rows[0][1] is not True:
            cell_incomplete.append("cell labels are incomplete")

        if not population.complete:
            cell_incomplete.append(
                "canonical population coverage is incomplete"
            )
        if item.observations < protocol.min_observations_per_cell:
            cell_incomplete.append(
                "minimum observations per cell not reached"
            )
        if not item.costs_complete:
            cell_incomplete.append(
                "execution-adjusted cost evidence is incomplete"
            )

        observed_features = set(item.observed_feature_namespaces)
        missing_features = sorted(
            set(profile.specialized_feature_namespaces)
            - observed_features
        )
        if missing_features:
            cell_incomplete.append(
                "missing asset-specific feature namespaces: "
                + ", ".join(missing_features)
            )

        if cell_failures:
            statuses[key] = "FAIL"
            failures.extend(
                f"{key}: {reason}" for reason in cell_failures
            )
        elif cell_incomplete:
            statuses[key] = "INCONCLUSIVE"
            incomplete.extend(
                f"{key}: {reason}" for reason in cell_incomplete
            )
        else:
            statuses[key] = "PASS"

    evidence_sha = _digest(
        tuple(
            item.digest
            for item in sorted(
                records,
                key=lambda row: (row.asset_class, row.regime),
            )
        )
    )
    if failures:
        status = "FAIL"
        reasons = tuple(dict.fromkeys(failures + incomplete))
    elif incomplete:
        status = "INCONCLUSIVE"
        reasons = tuple(dict.fromkeys(incomplete))
    else:
        status = "PASS"
        reasons = (
            "all registered cross-market/regime coverage constraints passed",
        )

    return CrossMarketGeneralizationAssessment(
        status=status,
        reasons=reasons,
        cell_statuses=statuses,
        protocol_sha256=protocol.digest,
        evidence_sha256=evidence_sha,
    )
