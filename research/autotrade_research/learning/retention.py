"""Deterministic retention gate for continual AutoTrade candidates.

This module evaluates already-produced regime metrics. It neither trains models
nor changes live routing. Promotion is impossible unless every registered hard
gate is satisfied.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


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
        if not isinstance(regime, str) or not regime.strip():
            raise ValueError("regime is required")
        if not isinstance(observations, int) or isinstance(observations, bool) or observations < 0:
            raise ValueError("observations must be a non-negative integer")
        if not isinstance(label_complete, bool):
            raise TypeError("label_complete must be boolean")
        return cls(
            regime=regime.strip(),
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
        protected = tuple(str(x).strip() for x in protected_regimes if str(x).strip())
        recent = tuple(str(x).strip() for x in recent_regimes if str(x).strip())
        if len(protected) != len(set(protected)) or len(recent) != len(set(recent)):
            raise ValueError("regime lists must not contain duplicates")
        if not recent:
            raise ValueError("at least one recent regime is required")
        if not isinstance(min_observations_per_regime, int) or isinstance(min_observations_per_regime, bool) or min_observations_per_regime < 1:
            raise ValueError("min_observations_per_regime must be positive")
        for name, value in (
            ("require_complete_labels", require_complete_labels),
            ("independent_science_gate_passed", independent_science_gate_passed),
            ("risk_gate_passed", risk_gate_passed),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be boolean")
        return cls(
            protected_regimes=protected,
            recent_regimes=recent,
            max_protected_degradation=_non_negative(max_protected_degradation, name="max_protected_degradation"),
            max_recent_degradation=_non_negative(max_recent_degradation, name="max_recent_degradation"),
            min_recent_improvement=_decimal(min_recent_improvement, name="min_recent_improvement"),
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


def evaluate_retention(
    metrics: Mapping[str, RegimeMetric],
    policy: RetentionPolicy,
) -> RetentionDecision:
    required = set(policy.protected_regimes) | set(policy.recent_regimes)
    missing = sorted(required - set(metrics))
    reasons: list[str] = []
    if missing:
        reasons.append("missing registered regimes: " + ", ".join(missing))

    decisions: list[RegimeDecision] = []
    recent_deltas: list[Decimal] = []
    evidence_incomplete = bool(missing)

    for regime in sorted(required & set(metrics)):
        metric = metrics[regime]
        if metric.regime != regime:
            raise ValueError(f"metric key/regime mismatch for {regime}")
        protected = regime in policy.protected_regimes
        recent = regime in policy.recent_regimes
        delta = metric.candidate_net_score - metric.champion_net_score
        passed = True
        local_reasons: list[str] = []

        if metric.observations < policy.min_observations_per_regime:
            passed = False
            evidence_incomplete = True
            local_reasons.append("insufficient observations")
        if policy.require_complete_labels and not metric.label_complete:
            passed = False
            evidence_incomplete = True
            local_reasons.append("labels incomplete")
        if protected and delta < -policy.max_protected_degradation:
            passed = False
            local_reasons.append("protected-regime degradation exceeds tolerance")
        if recent:
            recent_deltas.append(delta)
            if delta < -policy.max_recent_degradation:
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
        if recent_improvement < policy.min_recent_improvement:
            reasons.append("registered recent-regime improvement threshold not met")

    if not policy.independent_science_gate_passed:
        reasons.append("independent scientific gate has not passed")
    if not policy.risk_gate_passed:
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
