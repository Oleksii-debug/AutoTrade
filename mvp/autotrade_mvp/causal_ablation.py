from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True, slots=True)
class ShadowPair:
    pair_id: str
    task_id: str
    input_cutoff_utc: datetime
    baseline_decision_utc: datetime
    candidate_decision_utc: datetime
    outcome_available_utc: datetime
    baseline_utility: Decimal
    candidate_utility: Decimal
    baseline_cost: Decimal
    candidate_cost: Decimal

    def __post_init__(self) -> None:
        if not self.pair_id.strip() or not self.task_id.strip():
            raise ValueError("pair_id and task_id are required")
        for name, value in (
            ("input_cutoff_utc", self.input_cutoff_utc),
            ("baseline_decision_utc", self.baseline_decision_utc),
            ("candidate_decision_utc", self.candidate_decision_utc),
            ("outcome_available_utc", self.outcome_available_utc),
        ):
            if value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        for value in (
            self.baseline_utility,
            self.candidate_utility,
            self.baseline_cost,
            self.candidate_cost,
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError("utility and cost values must be finite Decimal")
        if self.baseline_cost < 0 or self.candidate_cost < 0:
            raise ValueError("cost cannot be negative")
        if self.baseline_decision_utc > self.input_cutoff_utc:
            raise ValueError("baseline decision used information after the declared cutoff")
        if self.candidate_decision_utc > self.input_cutoff_utc:
            raise ValueError("candidate decision used information after the declared cutoff")
        if self.outcome_available_utc <= self.input_cutoff_utc:
            raise ValueError("outcome must become available strictly after the decision cutoff")

    @property
    def incremental_value(self) -> Decimal:
        baseline_net = self.baseline_utility - self.baseline_cost
        candidate_net = self.candidate_utility - self.candidate_cost
        return candidate_net - baseline_net


@dataclass(frozen=True, slots=True)
class AblationResult:
    status: str
    pair_count: int
    mean_incremental_value: Decimal | None
    sample_stddev: Decimal | None
    lower_bound: Decimal | None
    threshold: Decimal
    reason: str


def evaluate_incremental_value(
    pairs: Iterable[ShadowPair],
    *,
    minimum_pairs: int,
    required_lower_bound: Decimal,
    uncertainty_multiplier: Decimal = Decimal("2"),
) -> AblationResult:
    """Evaluate candidate value on matched causal shadow pairs only.

    The function does not route models and does not promote a candidate. It
    produces a frozen evidence result that another qualification gate may use.
    """
    if not isinstance(minimum_pairs, int) or isinstance(minimum_pairs, bool) or minimum_pairs < 2:
        raise ValueError("minimum_pairs must be an integer >= 2")
    if not isinstance(required_lower_bound, Decimal) or not required_lower_bound.is_finite():
        raise ValueError("required_lower_bound must be finite Decimal")
    if (
        not isinstance(uncertainty_multiplier, Decimal)
        or not uncertainty_multiplier.is_finite()
        or uncertainty_multiplier < 0
    ):
        raise ValueError("uncertainty_multiplier must be finite non-negative Decimal")

    seen: set[str] = set()
    values: list[Decimal] = []
    for pair in pairs:
        if pair.pair_id in seen:
            raise ValueError(f"duplicate shadow pair: {pair.pair_id}")
        seen.add(pair.pair_id)
        values.append(pair.incremental_value)

    if len(values) < minimum_pairs:
        return AblationResult(
            status="INCONCLUSIVE",
            pair_count=len(values),
            mean_incremental_value=None,
            sample_stddev=None,
            lower_bound=None,
            threshold=required_lower_bound,
            reason="insufficient_matched_pairs",
        )

    count = Decimal(len(values))
    mean = sum(values, Decimal("0")) / count
    squared = sum(((value - mean) * (value - mean) for value in values), Decimal("0"))
    variance = squared / Decimal(len(values) - 1)
    stddev = variance.sqrt()
    standard_error = stddev / count.sqrt()
    lower = mean - uncertainty_multiplier * standard_error
    status = "PASS" if lower >= required_lower_bound else "FAIL"
    return AblationResult(
        status=status,
        pair_count=len(values),
        mean_incremental_value=mean,
        sample_stddev=stddev,
        lower_bound=lower,
        threshold=required_lower_bound,
        reason="matched_shadow_ablation",
    )
