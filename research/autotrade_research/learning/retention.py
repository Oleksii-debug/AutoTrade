"""Deterministic retention gate for continual AutoTrade candidates.

This module evaluates already-produced regime metrics. It neither trains models
nor changes live routing. Promotion is impossible unless every registered hard
gate is satisfied.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping

from .population_coverage import PopulationCoverageManifest


def _decimal(value, *, name: str) -> Decimal:
    if type(value) not in (Decimal, str, int):
        raise TypeError(f"{name} must use exact Decimal, string or integer input")
    try:
        result = value if type(value) is Decimal else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _regime_text(value: object, *, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be exact text")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized


def _regime_tuple(value: object, *, name: str) -> tuple[str, ...]:
    if type(value) not in (list, tuple):
        raise TypeError("regime lists must be exact list or tuple values")
    items = tuple(value)
    if any(type(item) is not str for item in items):
        raise TypeError("regime lists must contain exact text values")
    normalized = tuple(_regime_text(item, name=name) for item in items)
    if len(normalized) != len(set(normalized)):
        raise ValueError("regime lists must not contain duplicates")
    return normalized


def _metrics_snapshot(metrics: object) -> dict[str, "RegimeMetric"]:
    if type(metrics) is not dict:
        raise TypeError("metrics must be an exact dict")
    snapshot: dict[str, RegimeMetric] = {}
    for key, metric in metrics.items():
        if type(key) is not str:
            raise TypeError("metric keys must be exact text")
        if type(metric) is not RegimeMetric:
            raise TypeError("metric values must be exact RegimeMetric")
        canonical_key = _regime_text(key, name="metric key")
        canonical_metric = RegimeMetric(
            regime=metric.regime,
            champion_net_score=metric.champion_net_score,
            candidate_net_score=metric.candidate_net_score,
            observations=metric.observations,
            label_complete=metric.label_complete,
        )
        if canonical_metric.regime != canonical_key:
            raise ValueError(f"metric key/regime mismatch for {canonical_key}")
        snapshot[canonical_key] = canonical_metric
    return snapshot


def _non_negative(value, *, name: str) -> Decimal:
    result = _decimal(value, name=name)
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


@dataclass(frozen=True)
class RegimeMetric:
    regime: str
    champion_net_score: Decimal
    candidate_net_score: Decimal
    observations: int
    label_complete: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "regime",
            _regime_text(self.regime, name="regime"),
        )
        if type(self.observations) is not int or self.observations < 0:
            raise ValueError("observations must be a non-negative exact integer")
        if type(self.label_complete) is not bool:
            raise TypeError("label_complete must be exact boolean")
        object.__setattr__(
            self,
            "champion_net_score",
            _decimal(self.champion_net_score, name="champion_net_score"),
        )
        object.__setattr__(
            self,
            "candidate_net_score",
            _decimal(self.candidate_net_score, name="candidate_net_score"),
        )

    @classmethod
    def create(
        cls,
        *,
        regime: str,
        champion_net_score,
        candidate_net_score,
        observations: int,
        label_complete: bool,
    ) -> "RegimeMetric":
        regime_value = _regime_text(regime, name="regime")
        if type(observations) is not int or observations < 0:
            raise ValueError("observations must be a non-negative exact integer")
        if type(label_complete) is not bool:
            raise TypeError("label_complete must be exact boolean")
        return cls(
            regime=regime_value,
            champion_net_score=_decimal(champion_net_score, name="champion_net_score"),
            candidate_net_score=_decimal(candidate_net_score, name="candidate_net_score"),
            observations=observations,
            label_complete=label_complete,
        )


@dataclass(frozen=True)
class RetentionPolicy:
    protected_regimes: tuple[str, ...]
    recent_regimes: tuple[str, ...]
    max_protected_degradation: Decimal
    max_recent_degradation: Decimal
    min_recent_improvement: Decimal
    min_observations_per_regime: int
    require_complete_labels: bool
    independent_science_gate_passed: bool
    risk_gate_passed: bool

    def __post_init__(self) -> None:
        protected = _regime_tuple(
            self.protected_regimes,
            name="protected regime",
        )
        recent = _regime_tuple(
            self.recent_regimes,
            name="recent regime",
        )
        if not recent:
            raise ValueError("at least one recent regime is required")
        if (
            type(self.min_observations_per_regime) is not int
            or self.min_observations_per_regime < 1
        ):
            raise ValueError("min_observations_per_regime must be a positive exact integer")
        for name in (
            "require_complete_labels",
            "independent_science_gate_passed",
            "risk_gate_passed",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be exact boolean")
        object.__setattr__(self, "protected_regimes", protected)
        object.__setattr__(self, "recent_regimes", recent)
        object.__setattr__(
            self,
            "max_protected_degradation",
            _non_negative(
                self.max_protected_degradation,
                name="max_protected_degradation",
            ),
        )
        object.__setattr__(
            self,
            "max_recent_degradation",
            _non_negative(
                self.max_recent_degradation,
                name="max_recent_degradation",
            ),
        )
        object.__setattr__(
            self,
            "min_recent_improvement",
            _non_negative(
                self.min_recent_improvement,
                name="min_recent_improvement",
            ),
        )

    @classmethod
    def create(
        cls,
        *,
        protected_regimes,
        recent_regimes,
        max_protected_degradation,
        max_recent_degradation="0",
        min_recent_improvement,
        min_observations_per_regime: int,
        require_complete_labels: bool = True,
        independent_science_gate_passed: bool,
        risk_gate_passed: bool,
    ) -> "RetentionPolicy":
        protected = _regime_tuple(
            protected_regimes,
            name="protected regime",
        )
        recent = _regime_tuple(
            recent_regimes,
            name="recent regime",
        )
        if not recent:
            raise ValueError("at least one recent regime is required")
        if type(min_observations_per_regime) is not int or min_observations_per_regime < 1:
            raise ValueError("min_observations_per_regime must be a positive exact integer")
        for name, value in (
            ("require_complete_labels", require_complete_labels),
            ("independent_science_gate_passed", independent_science_gate_passed),
            ("risk_gate_passed", risk_gate_passed),
        ):
            if type(value) is not bool:
                raise TypeError(f"{name} must be exact boolean")
        return cls(
            protected_regimes=protected,
            recent_regimes=recent,
            max_protected_degradation=_non_negative(max_protected_degradation, name="max_protected_degradation"),
            max_recent_degradation=_non_negative(max_recent_degradation, name="max_recent_degradation"),
            min_recent_improvement=_non_negative(min_recent_improvement, name="min_recent_improvement"),
            min_observations_per_regime=min_observations_per_regime,
            require_complete_labels=require_complete_labels,
            independent_science_gate_passed=independent_science_gate_passed,
            risk_gate_passed=risk_gate_passed,
        )


@dataclass(frozen=True)
class RegimeDecision:
    regime: str
    delta: Decimal
    protected: bool
    recent: bool
    passed: bool
    reason: str


@dataclass(frozen=True)
class RetentionDecision:
    promotable: bool
    status: str
    recent_improvement: Decimal | None
    regimes: tuple[RegimeDecision, ...]
    reasons: tuple[str, ...]
    population_coverage_digest: str | None = None


def evaluate_retention(
    metrics: Mapping[str, RegimeMetric],
    policy: RetentionPolicy,
) -> RetentionDecision:
    if type(policy) is not RetentionPolicy:
        raise TypeError("policy must be exact RetentionPolicy")
    canonical_policy = RetentionPolicy(
        protected_regimes=policy.protected_regimes,
        recent_regimes=policy.recent_regimes,
        max_protected_degradation=policy.max_protected_degradation,
        max_recent_degradation=policy.max_recent_degradation,
        min_recent_improvement=policy.min_recent_improvement,
        min_observations_per_regime=policy.min_observations_per_regime,
        require_complete_labels=policy.require_complete_labels,
        independent_science_gate_passed=policy.independent_science_gate_passed,
        risk_gate_passed=policy.risk_gate_passed,
    )
    metric_snapshot = _metrics_snapshot(metrics)
    required = set(canonical_policy.protected_regimes) | set(canonical_policy.recent_regimes)
    missing = sorted(required - set(metric_snapshot))
    reasons: list[str] = []
    if missing:
        reasons.append("missing registered regimes: " + ", ".join(missing))

    decisions: list[RegimeDecision] = []
    recent_deltas: list[Decimal] = []
    evidence_incomplete = bool(missing)

    for regime in sorted(required & set(metric_snapshot)):
        metric = metric_snapshot[regime]
        if metric.regime != regime:
            raise ValueError(f"metric key/regime mismatch for {regime}")
        protected = regime in canonical_policy.protected_regimes
        recent = regime in canonical_policy.recent_regimes
        delta = metric.candidate_net_score - metric.champion_net_score
        passed = True
        local_reasons: list[str] = []

        if metric.observations < canonical_policy.min_observations_per_regime:
            passed = False
            evidence_incomplete = True
            local_reasons.append("insufficient observations")
        if canonical_policy.require_complete_labels and not metric.label_complete:
            passed = False
            evidence_incomplete = True
            local_reasons.append("labels incomplete")
        if protected and delta < -canonical_policy.max_protected_degradation:
            passed = False
            local_reasons.append("protected-regime degradation exceeds tolerance")
        if recent:
            recent_deltas.append(delta)
            if delta < -canonical_policy.max_recent_degradation:
                passed = False
                local_reasons.append("recent-regime degradation exceeds tolerance")

        decisions.append(
            RegimeDecision(
                regime=regime,
                delta=delta,
                protected=protected,
                recent=recent,
                passed=passed,
                reason="; ".join(local_reasons) if local_reasons else "registered regime constraints satisfied",
            )
        )

    recent_improvement = None
    if recent_deltas:
        recent_improvement = sum(recent_deltas, Decimal("0")) / Decimal(len(recent_deltas))
        if recent_improvement < canonical_policy.min_recent_improvement:
            reasons.append("registered recent-regime improvement threshold not met")

    if not canonical_policy.independent_science_gate_passed:
        reasons.append("independent scientific gate has not passed")
    if not canonical_policy.risk_gate_passed:
        reasons.append("independent risk gate has not passed")
    if any(not row.passed for row in decisions):
        reasons.append("one or more registered regime constraints failed")

    promotable = (
        not evidence_incomplete
        and not reasons
        and recent_improvement is not None
        and all(row.passed for row in decisions)
    )
    if promotable:
        status = "PASS"
    elif evidence_incomplete:
        status = "INCONCLUSIVE"
    else:
        status = "FAIL"
    return RetentionDecision(
        promotable=promotable,
        status=status,
        recent_improvement=recent_improvement,
        regimes=tuple(decisions),
        reasons=tuple(reasons),
    )


def evaluate_population_bound_retention(
    metrics: Mapping[str, RegimeMetric],
    policy: RetentionPolicy,
    population: PopulationCoverageManifest,
) -> RetentionDecision:
    """Require aggregate metrics to reconcile to one complete population manifest.

    The existing retention math remains the only scoring authority.  This bridge
    only proves that its observation counts and label-completeness flags describe
    the same causal population that scientific qualification will attest.
    """

    if type(population) is not PopulationCoverageManifest:
        raise TypeError("population must be exact PopulationCoverageManifest")
    if type(policy) is not RetentionPolicy:
        raise TypeError("policy must be exact RetentionPolicy")
    canonical_policy = RetentionPolicy(
        protected_regimes=policy.protected_regimes,
        recent_regimes=policy.recent_regimes,
        max_protected_degradation=policy.max_protected_degradation,
        max_recent_degradation=policy.max_recent_degradation,
        min_recent_improvement=policy.min_recent_improvement,
        min_observations_per_regime=policy.min_observations_per_regime,
        require_complete_labels=policy.require_complete_labels,
        independent_science_gate_passed=policy.independent_science_gate_passed,
        risk_gate_passed=policy.risk_gate_passed,
    )
    metric_snapshot = _metrics_snapshot(metrics)
    base = evaluate_retention(metric_snapshot, canonical_policy)
    reasons = list(base.reasons)
    evidence_incomplete = not population.complete
    if evidence_incomplete:
        reasons.append("population coverage manifest is incomplete")

    counts = dict(population.included_regime_counts)
    labels = dict(population.included_labels_complete_by_regime)
    required = set(canonical_policy.protected_regimes) | set(canonical_policy.recent_regimes)

    for regime in sorted(required):
        metric = metric_snapshot.get(regime)
        if metric is None:
            continue
        expected_observations = counts.get(regime, 0)
        if metric.observations != expected_observations:
            evidence_incomplete = True
            reasons.append(
                f"population observation count mismatch for regime {regime}"
            )
        expected_label_complete = labels.get(regime, False)
        if metric.label_complete != expected_label_complete:
            evidence_incomplete = True
            reasons.append(
                f"population label-completeness mismatch for regime {regime}"
            )

    unregistered_population = sorted(set(counts) - required)
    if unregistered_population:
        evidence_incomplete = True
        reasons.append(
            "population contains unregistered scored regimes: "
            + ", ".join(unregistered_population)
        )

    if base.status == "FAIL":
        status = "FAIL"
    elif evidence_incomplete:
        status = "INCONCLUSIVE"
    else:
        status = base.status
    promotable = base.promotable and not evidence_incomplete and status == "PASS"
    return RetentionDecision(
        promotable=promotable,
        status=status,
        recent_improvement=base.recent_improvement,
        regimes=base.regimes,
        reasons=tuple(dict.fromkeys(reasons)),
        population_coverage_digest=population.digest,
    )
