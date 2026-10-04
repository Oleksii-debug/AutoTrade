"""Evidence-only strategy-family weighting for product Section 21.

Strategies remain research tools.  This module never decides orders, promotes a
candidate, changes authority, or establishes economic edge.  It composes the
registered StrategyDescriptor, Section-20 cross-market/regime coverage and the
existing scientific GateDecision into one fail-closed scorecard.

A weight is a *research comparison weight* for one registered
asset-class/regime/horizon cell.  It is not a routing weight.  Any production
router that scopes a specialist to a regime still needs its own causally
qualified regime classifier and promotion authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Mapping, Sequence

from mvp.autotrade_mvp.exact_decimal import (
    ExactDecimalError,
    canonical_decimal_text,
)

from ..evaluation.gates import GateDecision
from ..learning.generalization import (
    CrossMarketGeneralizationAssessment,
    CrossMarketTrainingProtocol,
    MarketRegimeCell,
)
from .deterministic import StrategyDescriptor


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_STATUS = frozenset({"PASS", "FAIL", "INCONCLUSIVE"})
_DISPOSITIONS = frozenset(
    {
        "QUALIFIED_POSITIVE",
        "EVIDENCED_ZERO",
        "INCONCLUSIVE",
        "INVALID",
    }
)


class StrategyToolWeightingError(ValueError):
    """Raised for malformed or contradictory strategy-tool evidence."""


def _text(value: str, *, name: str) -> str:
    if type(value) is not str or value != value.strip() or not value:
        raise StrategyToolWeightingError(
            f"{name} must be non-empty exact text"
        )
    return value


def _sha(value: str, *, name: str) -> str:
    value = _text(value, name=name)
    if _SHA256.fullmatch(value) is None:
        raise StrategyToolWeightingError(
            f"{name} must be canonical sha256:<64 lowercase hex>"
        )
    return value


def _git_sha(value: str, *, name: str) -> str:
    value = _text(value, name=name)
    if _GIT_SHA.fullmatch(value) is None:
        raise StrategyToolWeightingError(
            f"{name} must be a 40-character lowercase git SHA"
        )
    return value


def _decimal(
    value: object,
    *,
    name: str,
    non_negative: bool = False,
) -> Decimal:
    if type(value) not in {Decimal, str, int}:
        raise TypeError(
            f"{name} must use exact Decimal, string or integer input"
        )
    try:
        result = value if type(value) is Decimal else Decimal(value)
        canonical_decimal_text(result)
    except (InvalidOperation, ValueError, TypeError, ExactDecimalError) as error:
        raise StrategyToolWeightingError(
            f"{name} must be a finite exact decimal"
        ) from error
    if non_negative and result < 0:
        raise StrategyToolWeightingError(f"{name} cannot be negative")
    return result


def _canonical(value: object, *, path: str = "value") -> object:
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is Decimal:
        return canonical_decimal_text(value)
    if type(value) is ExactWeight:
        return {
            "numerator": value.numerator,
            "denominator": value.denominator,
        }
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            key = _text(raw_key, name=f"{path} key")
            if key in normalized:
                raise StrategyToolWeightingError(
                    f"{path} contains duplicate keys"
                )
            normalized[key] = _canonical(
                raw_value,
                path=f"{path}.{key}",
            )
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, (tuple, list)):
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
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(payload).hexdigest()


def _detach_descriptor(value: StrategyDescriptor) -> StrategyDescriptor:
    if type(value) is not StrategyDescriptor:
        raise TypeError(
            "registered_strategies must contain exact StrategyDescriptor"
        )
    return StrategyDescriptor(
        strategy_id=value.strategy_id,
        version=value.version,
        family=value.family,
        feature_schema=value.feature_schema,
        market_requirements=value.market_requirements,
        minimum_history=value.minimum_history,
        horizon_seconds=value.horizon_seconds,
        decision_schedule=value.decision_schedule,
        proposal_semantics=value.proposal_semantics,
        parameter_bounds=value.parameter_bounds,
        resource_profile=value.resource_profile,
        supported_regimes=value.supported_regimes,
        source_license=value.source_license,
        evaluation_protocol_sha256=value.evaluation_protocol_sha256,
        artifact_sha256=value.artifact_sha256,
    )


def _detach_coverage_protocol(
    value: CrossMarketTrainingProtocol,
) -> CrossMarketTrainingProtocol:
    if type(value) is not CrossMarketTrainingProtocol:
        raise TypeError(
            "coverage_protocol must be exact CrossMarketTrainingProtocol"
        )
    return CrossMarketTrainingProtocol(
        protocol_id=value.protocol_id,
        candidate_hash=value.candidate_hash,
        population_protocol_hash=value.population_protocol_hash,
        exact_build_sha=value.exact_build_sha,
        asset_profiles=value.asset_profiles,
        required_cells=value.required_cells,
        min_observations_per_cell=value.min_observations_per_cell,
    )


def _detach_coverage_assessment(
    value: CrossMarketGeneralizationAssessment,
) -> CrossMarketGeneralizationAssessment:
    if type(value) is not CrossMarketGeneralizationAssessment:
        raise TypeError(
            "coverage_assessment must be exact "
            "CrossMarketGeneralizationAssessment"
        )
    return CrossMarketGeneralizationAssessment(
        status=value.status,
        reasons=value.reasons,
        cell_statuses=dict(value.cell_statuses),
        protocol_sha256=value.protocol_sha256,
        evidence_sha256=value.evidence_sha256,
        economic_edge_status=value.economic_edge_status,
    )


def _detach_gate(value: GateDecision) -> GateDecision:
    if type(value) is not GateDecision:
        raise TypeError("scientific_gate must be exact GateDecision")
    return GateDecision(
        status=value.status,
        reasons=value.reasons,
        checks=dict(value.checks),
        provenance=dict(value.provenance),
    )


@dataclass(frozen=True, slots=True, order=True)
class StrategyToolCell:
    """One registered comparison context."""

    asset_class: str
    regime: str
    horizon_seconds: int

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
        if (
            type(self.horizon_seconds) is not int
            or self.horizon_seconds <= 0
        ):
            raise StrategyToolWeightingError(
                "horizon_seconds must be a positive exact integer"
            )

    @property
    def key(self) -> str:
        return (
            f"{self.asset_class}::{self.regime}::"
            f"{self.horizon_seconds}"
        )


@dataclass(frozen=True, slots=True)
class ExactWeight:
    """Canonical non-negative rational weight with no rounding."""

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if (
            type(self.numerator) is not int
            or type(self.denominator) is not int
        ):
            raise TypeError("weight numerator/denominator must be exact integers")
        if self.numerator < 0 or self.denominator <= 0:
            raise StrategyToolWeightingError(
                "weight must be a non-negative rational"
            )
        reduced = Fraction(self.numerator, self.denominator)
        if (
            reduced.numerator != self.numerator
            or reduced.denominator != self.denominator
        ):
            raise StrategyToolWeightingError(
                "weight must be stored in reduced canonical form"
            )

    @classmethod
    def from_fraction(cls, value: Fraction) -> "ExactWeight":
        if type(value) is not Fraction:
            raise TypeError("weight source must be exact Fraction")
        if value < 0:
            raise StrategyToolWeightingError("weight cannot be negative")
        return cls(value.numerator, value.denominator)

    @classmethod
    def zero(cls) -> "ExactWeight":
        return cls(0, 1)

    @property
    def text(self) -> str:
        return f"{self.numerator}/{self.denominator}"


@dataclass(frozen=True, slots=True)
class StrategyToolPolicy:
    """Frozen comparison protocol; popularity is intentionally absent."""

    policy_id: str
    exact_build_sha: str
    minimum_practical_advantage: Decimal
    registered_strategies: tuple[StrategyDescriptor, ...]
    required_cells: tuple[StrategyToolCell, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _text(self.policy_id, name="policy_id"),
        )
        object.__setattr__(
            self,
            "exact_build_sha",
            _git_sha(self.exact_build_sha, name="exact_build_sha"),
        )
        object.__setattr__(
            self,
            "minimum_practical_advantage",
            _decimal(
                self.minimum_practical_advantage,
                name="minimum_practical_advantage",
                non_negative=True,
            ),
        )
        if type(self.registered_strategies) is not tuple:
            raise TypeError("registered_strategies must be an exact tuple")
        if len(self.registered_strategies) < 2:
            raise StrategyToolWeightingError(
                "strategy-tool policy requires at least two strategies"
            )

        strategies = tuple(
            _detach_descriptor(item)
            for item in self.registered_strategies
        )
        fingerprints = tuple(item.fingerprint for item in strategies)
        if len(set(fingerprints)) != len(fingerprints):
            raise StrategyToolWeightingError(
                "registered strategy fingerprints must be unique"
            )
        if fingerprints != tuple(sorted(fingerprints)):
            raise StrategyToolWeightingError(
                "registered_strategies must use canonical fingerprint ordering"
            )

        if type(self.required_cells) is not tuple or not self.required_cells:
            raise StrategyToolWeightingError(
                "required_cells must be a non-empty exact tuple"
            )
        cells: list[StrategyToolCell] = []
        seen: set[str] = set()
        for item in self.required_cells:
            if type(item) is not StrategyToolCell:
                raise TypeError(
                    "required_cells must contain exact StrategyToolCell"
                )
            clean = StrategyToolCell(
                item.asset_class,
                item.regime,
                item.horizon_seconds,
            )
            if clean.key in seen:
                raise StrategyToolWeightingError(
                    f"duplicate required cell: {clean.key}"
                )
            seen.add(clean.key)
            cells.append(clean)
        if tuple(cells) != tuple(sorted(cells)):
            raise StrategyToolWeightingError(
                "required_cells must use canonical ordering"
            )

        for cell in cells:
            eligible = [
                item
                for item in strategies
                if (
                    item.horizon_seconds == cell.horizon_seconds
                    and cell.regime in item.supported_regimes
                )
            ]
            families = {item.family for item in eligible}
            if len(families) < 2:
                raise StrategyToolWeightingError(
                    f"{cell.key} requires at least two distinct "
                    "registered strategy families"
                )

        object.__setattr__(self, "registered_strategies", strategies)
        object.__setattr__(self, "required_cells", tuple(cells))

    @classmethod
    def create(
        cls,
        *,
        policy_id: str,
        exact_build_sha: str,
        minimum_practical_advantage: object,
        registered_strategies: Sequence[StrategyDescriptor],
        required_cells: Sequence[StrategyToolCell],
    ) -> "StrategyToolPolicy":
        if isinstance(registered_strategies, (str, bytes)):
            raise TypeError("registered_strategies must be a sequence")
        if isinstance(required_cells, (str, bytes)):
            raise TypeError("required_cells must be a sequence")
        strategies = tuple(_detach_descriptor(item) for item in registered_strategies)
        strategies = tuple(sorted(strategies, key=lambda item: item.fingerprint))
        cells_raw = tuple(required_cells)
        if any(type(item) is not StrategyToolCell for item in cells_raw):
            raise TypeError(
                "required_cells must contain exact StrategyToolCell"
            )
        return cls(
            policy_id=_text(policy_id, name="policy_id"),
            exact_build_sha=_git_sha(
                exact_build_sha,
                name="exact_build_sha",
            ),
            minimum_practical_advantage=_decimal(
                minimum_practical_advantage,
                name="minimum_practical_advantage",
                non_negative=True,
            ),
            registered_strategies=strategies,
            required_cells=tuple(sorted(cells_raw)),
        )

    @property
    def digest(self) -> str:
        return _digest(
            {
                "schema_version": 1,
                "policy_id": self.policy_id,
                "exact_build_sha": self.exact_build_sha,
                "minimum_practical_advantage":
                    self.minimum_practical_advantage,
                "registered_strategies": tuple(
                    {
                        "strategy_fingerprint": item.fingerprint,
                        "strategy_id": item.strategy_id,
                        "version": item.version,
                        "family": item.family,
                        "artifact_sha256": item.artifact_sha256,
                        "evaluation_protocol_sha256":
                            item.evaluation_protocol_sha256,
                        "horizon_seconds": item.horizon_seconds,
                        "supported_regimes": item.supported_regimes,
                    }
                    for item in self.registered_strategies
                ),
                "required_cells": tuple(
                    {
                        "asset_class": item.asset_class,
                        "regime": item.regime,
                        "horizon_seconds": item.horizon_seconds,
                    }
                    for item in self.required_cells
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class StrategyCellEvidence:
    """Evidence for one registered strategy in one comparison cell."""

    cell: StrategyToolCell
    strategy_fingerprint: str
    coverage_protocol: CrossMarketTrainingProtocol
    coverage_assessment: CrossMarketGeneralizationAssessment
    scientific_gate: GateDecision
    after_cost_net_advantage: Decimal | None
    dependence_aware_lower_bound: Decimal | None
    costs_complete: bool
    metrics_sha256: str

    def __post_init__(self) -> None:
        if type(self.cell) is not StrategyToolCell:
            raise TypeError("cell must be exact StrategyToolCell")
        object.__setattr__(
            self,
            "cell",
            StrategyToolCell(
                self.cell.asset_class,
                self.cell.regime,
                self.cell.horizon_seconds,
            ),
        )
        object.__setattr__(
            self,
            "strategy_fingerprint",
            _sha(
                self.strategy_fingerprint,
                name="strategy_fingerprint",
            ),
        )
        object.__setattr__(
            self,
            "coverage_protocol",
            _detach_coverage_protocol(self.coverage_protocol),
        )
        object.__setattr__(
            self,
            "coverage_assessment",
            _detach_coverage_assessment(self.coverage_assessment),
        )
        object.__setattr__(
            self,
            "scientific_gate",
            _detach_gate(self.scientific_gate),
        )
        if self.after_cost_net_advantage is not None:
            object.__setattr__(
                self,
                "after_cost_net_advantage",
                _decimal(
                    self.after_cost_net_advantage,
                    name="after_cost_net_advantage",
                ),
            )
        if self.dependence_aware_lower_bound is not None:
            object.__setattr__(
                self,
                "dependence_aware_lower_bound",
                _decimal(
                    self.dependence_aware_lower_bound,
                    name="dependence_aware_lower_bound",
                ),
            )
        if type(self.costs_complete) is not bool:
            raise TypeError("costs_complete must be boolean")
        object.__setattr__(
            self,
            "metrics_sha256",
            _sha(self.metrics_sha256, name="metrics_sha256"),
        )

    @property
    def key(self) -> tuple[str, str]:
        return self.cell.key, self.strategy_fingerprint

    @property
    def digest(self) -> str:
        return _digest(
            {
                "schema_version": 1,
                "cell": self.cell.key,
                "strategy_fingerprint": self.strategy_fingerprint,
                "coverage_protocol_sha256": self.coverage_protocol.digest,
                "coverage_assessment_protocol_sha256":
                    self.coverage_assessment.protocol_sha256,
                "coverage_assessment_evidence_sha256":
                    self.coverage_assessment.evidence_sha256,
                "coverage_status": self.coverage_assessment.status,
                "scientific_gate_status": self.scientific_gate.status,
                "scientific_gate_checks":
                    dict(self.scientific_gate.checks),
                "scientific_gate_provenance":
                    dict(self.scientific_gate.provenance),
                "after_cost_net_advantage":
                    self.after_cost_net_advantage,
                "dependence_aware_lower_bound":
                    self.dependence_aware_lower_bound,
                "costs_complete": self.costs_complete,
                "metrics_sha256": self.metrics_sha256,
            }
        )


def _detach_evidence(value: StrategyCellEvidence) -> StrategyCellEvidence:
    if type(value) is not StrategyCellEvidence:
        raise TypeError(
            "evidence must contain exact StrategyCellEvidence"
        )
    return StrategyCellEvidence(
        cell=value.cell,
        strategy_fingerprint=value.strategy_fingerprint,
        coverage_protocol=value.coverage_protocol,
        coverage_assessment=value.coverage_assessment,
        scientific_gate=value.scientific_gate,
        after_cost_net_advantage=value.after_cost_net_advantage,
        dependence_aware_lower_bound=value.dependence_aware_lower_bound,
        costs_complete=value.costs_complete,
        metrics_sha256=value.metrics_sha256,
    )


@dataclass(frozen=True, slots=True)
class StrategyToolAssessment:
    """Comparison result; never routing, promotion or trading authority."""

    status: str
    reasons: tuple[str, ...]
    cell_statuses: Mapping[str, str]
    dispositions: Mapping[str, Mapping[str, str]]
    weights: Mapping[str, Mapping[str, ExactWeight]]
    policy_sha256: str
    evidence_sha256: str
    economic_edge_status: str = "NOT_ESTABLISHED"
    routing_authority_granted: bool = False
    promotion_authority_granted: bool = False
    trading_authority_granted: bool = False

    def __post_init__(self) -> None:
        status = _text(self.status, name="status")
        if status not in _STATUS:
            raise StrategyToolWeightingError(
                "status must be PASS, FAIL or INCONCLUSIVE"
            )
        if type(self.reasons) is not tuple or not self.reasons:
            raise StrategyToolWeightingError(
                "assessment requires non-empty reasons"
            )
        reasons = tuple(
            _text(item, name="reason")
            for item in self.reasons
        )
        if not isinstance(self.cell_statuses, Mapping):
            raise TypeError("cell_statuses must be a mapping")
        statuses: dict[str, str] = {}
        for raw_key, raw_value in self.cell_statuses.items():
            key = _text(raw_key, name="cell status key")
            value = _text(raw_value, name=f"cell status {key}")
            if value not in _STATUS:
                raise StrategyToolWeightingError(
                    f"unsupported cell status: {value}"
                )
            statuses[key] = value
        if not statuses:
            raise StrategyToolWeightingError(
                "cell_statuses must not be empty"
            )

        if not isinstance(self.dispositions, Mapping):
            raise TypeError("dispositions must be a mapping")
        frozen_dispositions: dict[str, Mapping[str, str]] = {}
        for raw_cell, raw_rows in self.dispositions.items():
            cell = _text(raw_cell, name="disposition cell")
            if not isinstance(raw_rows, Mapping):
                raise TypeError("cell dispositions must be mappings")
            rows: dict[str, str] = {}
            for raw_strategy, raw_disposition in raw_rows.items():
                strategy = _sha(
                    raw_strategy,
                    name="disposition strategy fingerprint",
                )
                disposition = _text(
                    raw_disposition,
                    name="strategy disposition",
                )
                if disposition not in _DISPOSITIONS:
                    raise StrategyToolWeightingError(
                        f"unsupported strategy disposition: {disposition}"
                    )
                rows[strategy] = disposition
            frozen_dispositions[cell] = MappingProxyType(
                {key: rows[key] for key in sorted(rows)}
            )

        if not isinstance(self.weights, Mapping):
            raise TypeError("weights must be a mapping")
        frozen_weights: dict[str, Mapping[str, ExactWeight]] = {}
        for raw_cell, raw_rows in self.weights.items():
            cell = _text(raw_cell, name="weight cell")
            if not isinstance(raw_rows, Mapping):
                raise TypeError("cell weights must be mappings")
            rows: dict[str, ExactWeight] = {}
            for raw_strategy, raw_weight in raw_rows.items():
                strategy = _sha(
                    raw_strategy,
                    name="weight strategy fingerprint",
                )
                if type(raw_weight) is not ExactWeight:
                    raise TypeError(
                        "weights must contain exact ExactWeight values"
                    )
                rows[strategy] = ExactWeight(
                    raw_weight.numerator,
                    raw_weight.denominator,
                )
            if statuses.get(cell) != "PASS":
                if any(weight.numerator != 0 for weight in rows.values()):
                    raise StrategyToolWeightingError(
                        "non-PASS cells must fail closed to zero weights"
                    )
            elif any(weight.numerator != 0 for weight in rows.values()):
                total = sum(
                    (
                        Fraction(weight.numerator, weight.denominator)
                        for weight in rows.values()
                    ),
                    Fraction(0, 1),
                )
                if total != 1:
                    raise StrategyToolWeightingError(
                        "positive PASS-cell weights must sum exactly to one"
                    )
            frozen_weights[cell] = MappingProxyType(
                {key: rows[key] for key in sorted(rows)}
            )

        if set(statuses) != set(frozen_dispositions):
            raise StrategyToolWeightingError(
                "dispositions must cover every assessment cell"
            )
        if set(statuses) != set(frozen_weights):
            raise StrategyToolWeightingError(
                "weights must cover every assessment cell"
            )
        if self.economic_edge_status != "NOT_ESTABLISHED":
            raise StrategyToolWeightingError(
                "strategy-tool weighting cannot establish economic edge"
            )
        for name in (
            "routing_authority_granted",
            "promotion_authority_granted",
            "trading_authority_granted",
        ):
            if getattr(self, name) is not False:
                raise StrategyToolWeightingError(
                    f"{name} must remain false"
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
            "dispositions",
            MappingProxyType(
                {
                    key: frozen_dispositions[key]
                    for key in sorted(frozen_dispositions)
                }
            ),
        )
        object.__setattr__(
            self,
            "weights",
            MappingProxyType(
                {
                    key: frozen_weights[key]
                    for key in sorted(frozen_weights)
                }
            ),
        )
        object.__setattr__(
            self,
            "policy_sha256",
            _sha(self.policy_sha256, name="policy_sha256"),
        )
        object.__setattr__(
            self,
            "evidence_sha256",
            _sha(self.evidence_sha256, name="evidence_sha256"),
        )


def _eligible(
    descriptor: StrategyDescriptor,
    cell: StrategyToolCell,
) -> bool:
    return (
        descriptor.horizon_seconds == cell.horizon_seconds
        and cell.regime in descriptor.supported_regimes
    )


def _zero_map(
    fingerprints: Sequence[str],
) -> dict[str, ExactWeight]:
    return {
        fingerprint: ExactWeight.zero()
        for fingerprint in fingerprints
    }


def assess_strategy_tools(
    policy: StrategyToolPolicy,
    evidence: Sequence[StrategyCellEvidence],
) -> StrategyToolAssessment:
    """Build a fail-closed regime/horizon strategy research scorecard."""

    if type(policy) is not StrategyToolPolicy:
        raise TypeError("policy must be exact StrategyToolPolicy")
    policy = StrategyToolPolicy(
        policy_id=policy.policy_id,
        exact_build_sha=policy.exact_build_sha,
        minimum_practical_advantage=policy.minimum_practical_advantage,
        registered_strategies=policy.registered_strategies,
        required_cells=policy.required_cells,
    )
    if isinstance(evidence, (str, bytes)):
        raise TypeError("evidence must be a sequence")
    records = tuple(_detach_evidence(item) for item in evidence)

    strategies = {
        item.fingerprint: item
        for item in policy.registered_strategies
    }
    expected: dict[
        tuple[str, str],
        tuple[StrategyToolCell, StrategyDescriptor],
    ] = {}
    eligible_by_cell: dict[str, tuple[str, ...]] = {}
    for cell in policy.required_cells:
        fingerprints = tuple(
            sorted(
                descriptor.fingerprint
                for descriptor in policy.registered_strategies
                if _eligible(descriptor, cell)
            )
        )
        eligible_by_cell[cell.key] = fingerprints
        for fingerprint in fingerprints:
            expected[(cell.key, fingerprint)] = (
                cell,
                strategies[fingerprint],
            )

    supplied: dict[tuple[str, str], StrategyCellEvidence] = {}
    global_failures: list[str] = []
    for item in records:
        if item.key in supplied:
            raise StrategyToolWeightingError(
                "duplicate strategy/cell evidence: "
                f"{item.cell.key} / {item.strategy_fingerprint}"
            )
        supplied[item.key] = item
        if item.key not in expected:
            global_failures.append(
                "unregistered strategy/cell evidence: "
                f"{item.cell.key} / {item.strategy_fingerprint}"
            )

    evidence_sha256 = _digest(
        tuple(
            item.digest
            for item in sorted(
                records,
                key=lambda row: row.key,
            )
        )
    )

    cell_statuses: dict[str, str] = {}
    dispositions: dict[str, dict[str, str]] = {}
    weights: dict[str, dict[str, ExactWeight]] = {}
    all_failures: list[str] = list(global_failures)
    all_incomplete: list[str] = []
    no_positive_cells: list[str] = []

    for cell in policy.required_cells:
        fingerprints = eligible_by_cell[cell.key]
        cell_failures: list[str] = []
        cell_incomplete: list[str] = []
        cell_dispositions: dict[str, str] = {}
        excess: dict[str, Fraction] = {}

        for fingerprint in fingerprints:
            descriptor = strategies[fingerprint]
            item = supplied.get((cell.key, fingerprint))
            if item is None:
                cell_incomplete.append(
                    f"missing evidence for {descriptor.family} "
                    f"({fingerprint})"
                )
                cell_dispositions[fingerprint] = "INCONCLUSIVE"
                continue

            coverage_protocol = item.coverage_protocol
            coverage = item.coverage_assessment
            gate = item.scientific_gate
            candidate_failures: list[str] = []
            candidate_incomplete: list[str] = []

            if coverage_protocol.exact_build_sha != policy.exact_build_sha:
                candidate_failures.append(
                    "coverage exact build differs from scorecard build"
                )
            if coverage_protocol.candidate_hash != descriptor.artifact_sha256:
                candidate_failures.append(
                    "coverage candidate is not the registered strategy artifact"
                )
            required_market_cell = MarketRegimeCell(
                cell.asset_class,
                cell.regime,
            )
            if required_market_cell not in coverage_protocol.required_cells:
                candidate_failures.append(
                    "coverage protocol does not register this market/regime cell"
                )
            if coverage.protocol_sha256 != coverage_protocol.digest:
                candidate_failures.append(
                    "coverage assessment does not bind the supplied protocol"
                )
            if coverage.status == "FAIL":
                candidate_failures.append(
                    "cross-market/regime coverage failed"
                )
            elif coverage.status != "PASS":
                candidate_incomplete.append(
                    "cross-market/regime coverage is inconclusive"
                )

            if gate.status == "PASS" and any(
                value != "PASS"
                for value in gate.checks.values()
            ):
                candidate_failures.append(
                    "scientific gate PASS contradicts non-PASS checks"
                )
            if gate.status == "PASS":
                graph_digest = gate.provenance.get("evidence_graph_digest")
                review_source_sha = gate.provenance.get("review_source_sha")
                evidence_bundle_id = gate.provenance.get("evidence_bundle_id")
                if (
                    graph_digest is None
                    or evidence_bundle_id is None
                    or review_source_sha is None
                ):
                    candidate_incomplete.append(
                        "scientific PASS lacks immutable reviewed provenance"
                    )
                else:
                    try:
                        _sha(
                            graph_digest,
                            name="scientific evidence_graph_digest",
                        )
                    except (TypeError, StrategyToolWeightingError):
                        candidate_failures.append(
                            "scientific evidence graph digest is malformed"
                        )
                    if review_source_sha != policy.exact_build_sha:
                        candidate_failures.append(
                            "scientific review source differs from scorecard build"
                        )
                    try:
                        _text(
                            evidence_bundle_id,
                            name="scientific evidence_bundle_id",
                        )
                    except (TypeError, StrategyToolWeightingError):
                        candidate_failures.append(
                            "scientific evidence bundle identity is malformed"
                        )
            elif gate.status == "INCONCLUSIVE":
                candidate_incomplete.append(
                    "scientific evaluation is inconclusive"
                )

            if not item.costs_complete:
                candidate_incomplete.append(
                    "after-cost evidence is incomplete"
                )
            if (
                item.after_cost_net_advantage is None
                or item.dependence_aware_lower_bound is None
            ):
                candidate_incomplete.append(
                    "after-cost point estimate/lower bound is missing"
                )
            elif (
                item.dependence_aware_lower_bound
                > item.after_cost_net_advantage
            ):
                candidate_failures.append(
                    "dependence-aware lower bound exceeds point estimate"
                )

            if candidate_failures:
                cell_failures.extend(
                    f"{descriptor.family} ({fingerprint}): {reason}"
                    for reason in candidate_failures
                )
                cell_dispositions[fingerprint] = "INVALID"
                continue
            if candidate_incomplete:
                cell_incomplete.extend(
                    f"{descriptor.family} ({fingerprint}): {reason}"
                    for reason in candidate_incomplete
                )
                cell_dispositions[fingerprint] = "INCONCLUSIVE"
                continue

            if gate.status == "FAIL":
                cell_dispositions[fingerprint] = "EVIDENCED_ZERO"
                excess[fingerprint] = Fraction(0, 1)
                continue

            lower_bound = item.dependence_aware_lower_bound
            if lower_bound <= policy.minimum_practical_advantage:
                cell_dispositions[fingerprint] = "EVIDENCED_ZERO"
                excess[fingerprint] = Fraction(0, 1)
                continue

            margin = lower_bound - policy.minimum_practical_advantage
            cell_dispositions[fingerprint] = "QUALIFIED_POSITIVE"
            excess[fingerprint] = Fraction(margin)

        if cell_failures:
            cell_statuses[cell.key] = "FAIL"
            all_failures.extend(
                f"{cell.key}: {reason}"
                for reason in cell_failures
            )
            weights[cell.key] = _zero_map(fingerprints)
        elif cell_incomplete:
            cell_statuses[cell.key] = "INCONCLUSIVE"
            all_incomplete.extend(
                f"{cell.key}: {reason}"
                for reason in cell_incomplete
            )
            weights[cell.key] = _zero_map(fingerprints)
        else:
            cell_statuses[cell.key] = "PASS"
            positive_total = sum(
                excess.values(),
                Fraction(0, 1),
            )
            if positive_total > 0:
                weights[cell.key] = {
                    fingerprint: ExactWeight.from_fraction(
                        excess.get(fingerprint, Fraction(0, 1))
                        / positive_total
                    )
                    for fingerprint in fingerprints
                }
            else:
                weights[cell.key] = _zero_map(fingerprints)
                no_positive_cells.append(cell.key)

        dispositions[cell.key] = {
            fingerprint: cell_dispositions.get(
                fingerprint,
                "INCONCLUSIVE",
            )
            for fingerprint in fingerprints
        }

    if global_failures:
        for cell in policy.required_cells:
            cell_statuses[cell.key] = "FAIL"
            weights[cell.key] = _zero_map(eligible_by_cell[cell.key])

    if all_failures:
        status = "FAIL"
        reasons = tuple(dict.fromkeys(all_failures + all_incomplete))
    elif all_incomplete:
        status = "INCONCLUSIVE"
        reasons = tuple(dict.fromkeys(all_incomplete))
    elif no_positive_cells:
        status = "PASS"
        reasons = tuple(
            f"{cell}: no registered strategy cleared the after-cost "
            "dependence-aware practical-effect threshold"
            for cell in no_positive_cells
        )
    else:
        status = "PASS"
        reasons = (
            "all registered strategy-tool comparison cells are complete",
        )

    return StrategyToolAssessment(
        status=status,
        reasons=reasons,
        cell_statuses=cell_statuses,
        dispositions=dispositions,
        weights=weights,
        policy_sha256=policy.digest,
        evidence_sha256=evidence_sha256,
    )
