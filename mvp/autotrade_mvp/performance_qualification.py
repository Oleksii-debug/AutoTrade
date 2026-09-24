"""Runtime resource-budget qualification primitives.

This module is evidence evaluation, not a scheduler, risk authority, throttler or
trading policy. It evaluates measured samples against an explicitly declared
workload budget and fails closed on event loss, stale state or insufficient
evidence. It makes no universal throughput or HFT claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence


class RuntimeBudgetError(ValueError):
    """Raised when performance evidence or its declared budget is malformed."""


def _positive_int(value: int, *, name: str, allow_zero: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeBudgetError(f"{name} must be an integer")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeBudgetError(f"{name} must be >= {minimum}")
    return value


def _series(values: Sequence[int], *, name: str) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise RuntimeBudgetError(f"{name} must be a sequence")
    normalized = tuple(_positive_int(v, name=name, allow_zero=True) for v in values)
    return normalized


def nearest_rank_percentile(values: Sequence[int], percentile: int) -> int:
    """Return an integer nearest-rank percentile without float arithmetic."""

    normalized = _series(values, name="values")
    if not normalized:
        raise RuntimeBudgetError("values cannot be empty")
    if not isinstance(percentile, int) or isinstance(percentile, bool) or not 1 <= percentile <= 100:
        raise RuntimeBudgetError("percentile must be an integer in [1, 100]")
    ordered = sorted(normalized)
    rank = (percentile * len(ordered) + 99) // 100
    return ordered[rank - 1]


@dataclass(frozen=True)
class RuntimeBudgetSpec:
    scenario_id: str
    strategy_horizon_us: int
    max_p95_financial_latency_us: int
    max_financial_staleness_us: int
    max_research_interference_us: int
    min_financial_samples: int = 20
    min_research_samples: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.scenario_id, str) or not self.scenario_id.strip():
            raise RuntimeBudgetError("scenario_id is required")
        object.__setattr__(self, "scenario_id", self.scenario_id.strip())
        for field in (
            "strategy_horizon_us",
            "max_p95_financial_latency_us",
            "max_financial_staleness_us",
            "max_research_interference_us",
            "min_financial_samples",
            "min_research_samples",
        ):
            object.__setattr__(
                self,
                field,
                _positive_int(getattr(self, field), name=field),
            )
        if self.max_p95_financial_latency_us > self.strategy_horizon_us:
            raise RuntimeBudgetError(
                "financial latency budget cannot exceed the declared strategy horizon"
            )
        if self.max_financial_staleness_us > self.strategy_horizon_us:
            raise RuntimeBudgetError(
                "staleness budget cannot exceed the declared strategy horizon"
            )


@dataclass(frozen=True)
class RuntimeLoadObservation:
    scenario_id: str
    expected_financial_events: int
    recovered_financial_events: int
    financial_latency_us: tuple[int, ...]
    financial_staleness_us: tuple[int, ...]
    research_interference_us: tuple[int, ...]
    reconnect_backlog_remaining: int

    @classmethod
    def create(
        cls,
        *,
        scenario_id: str,
        expected_financial_events: int,
        recovered_financial_events: int,
        financial_latency_us: Sequence[int],
        financial_staleness_us: Sequence[int],
        research_interference_us: Sequence[int],
        reconnect_backlog_remaining: int,
    ) -> "RuntimeLoadObservation":
        if not isinstance(scenario_id, str) or not scenario_id.strip():
            raise RuntimeBudgetError("scenario_id is required")
        expected = _positive_int(
            expected_financial_events, name="expected_financial_events", allow_zero=True
        )
        recovered = _positive_int(
            recovered_financial_events, name="recovered_financial_events", allow_zero=True
        )
        if recovered > expected:
            raise RuntimeBudgetError("recovered_financial_events cannot exceed expected")
        return cls(
            scenario_id=scenario_id.strip(),
            expected_financial_events=expected,
            recovered_financial_events=recovered,
            financial_latency_us=_series(financial_latency_us, name="financial_latency_us"),
            financial_staleness_us=_series(financial_staleness_us, name="financial_staleness_us"),
            research_interference_us=_series(
                research_interference_us, name="research_interference_us"
            ),
            reconnect_backlog_remaining=_positive_int(
                reconnect_backlog_remaining,
                name="reconnect_backlog_remaining",
                allow_zero=True,
            ),
        )


@dataclass(frozen=True)
class RuntimeBudgetDecision:
    status: str
    scenario_id: str
    reasons: tuple[str, ...]
    metrics: Mapping[str, int]

    def __post_init__(self) -> None:
        if self.status not in {"PASS", "FAIL", "INCONCLUSIVE"}:
            raise RuntimeBudgetError("unsupported decision status")
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))


def evaluate_runtime_budget(
    spec: RuntimeBudgetSpec,
    observation: RuntimeLoadObservation,
) -> RuntimeBudgetDecision:
    """Evaluate declared-load evidence without extrapolating beyond that scenario."""

    if not isinstance(spec, RuntimeBudgetSpec):
        raise TypeError("spec must be RuntimeBudgetSpec")
    if not isinstance(observation, RuntimeLoadObservation):
        raise TypeError("observation must be RuntimeLoadObservation")
    if spec.scenario_id != observation.scenario_id:
        raise RuntimeBudgetError("observation belongs to another declared scenario")

    reasons: list[str] = []
    metrics: dict[str, int] = {
        "expected_financial_events": observation.expected_financial_events,
        "recovered_financial_events": observation.recovered_financial_events,
        "reconnect_backlog_remaining": observation.reconnect_backlog_remaining,
        "financial_sample_count": len(observation.financial_latency_us),
        "research_sample_count": len(observation.research_interference_us),
    }

    if observation.recovered_financial_events != observation.expected_financial_events:
        reasons.append("financial_event_loss")
    if observation.reconnect_backlog_remaining != 0:
        reasons.append("reconnect_backlog_not_drained")

    insufficient: list[str] = []
    if len(observation.financial_latency_us) < spec.min_financial_samples:
        insufficient.append("insufficient_financial_latency_samples")
    if len(observation.financial_staleness_us) < spec.min_financial_samples:
        insufficient.append("insufficient_staleness_samples")
    if len(observation.research_interference_us) < spec.min_research_samples:
        insufficient.append("insufficient_research_interference_samples")

    if observation.financial_latency_us:
        p95 = nearest_rank_percentile(observation.financial_latency_us, 95)
        metrics["p95_financial_latency_us"] = p95
        if p95 > spec.max_p95_financial_latency_us:
            reasons.append("financial_latency_budget_exceeded")
    if observation.financial_staleness_us:
        max_staleness = max(observation.financial_staleness_us)
        metrics["max_financial_staleness_us"] = max_staleness
        if max_staleness > spec.max_financial_staleness_us:
            reasons.append("financial_staleness_budget_exceeded")
    if observation.research_interference_us:
        max_interference = max(observation.research_interference_us)
        metrics["max_research_interference_us"] = max_interference
        if max_interference > spec.max_research_interference_us:
            reasons.append("research_interference_budget_exceeded")

    if reasons:
        status = "FAIL"
        final_reasons = tuple(dict.fromkeys(reasons + insufficient))
    elif insufficient:
        status = "INCONCLUSIVE"
        final_reasons = tuple(insufficient)
    else:
        status = "PASS"
        final_reasons = ()

    return RuntimeBudgetDecision(
        status=status,
        scenario_id=spec.scenario_id,
        reasons=final_reasons,
        metrics=metrics,
    )
