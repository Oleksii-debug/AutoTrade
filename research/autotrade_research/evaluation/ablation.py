"""Evidence-bound matched-input ablation primitives.

This module measures descriptive marginal contribution only.  It does not claim
causality, profitability or statistical significance by itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable


def _decimal(value: Decimal | int | str | float, field: str) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError(f"{field} must be finite")
    return number


@dataclass(frozen=True)
class AblationOutcome:
    case_id: str
    input_fingerprint: str
    variant: str
    utility: Decimal
    cost: Decimal
    elapsed_ms: int
    deadline_ms: int
    components: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.case_id or not self.input_fingerprint:
            raise ValueError("case_id and input_fingerprint are required")
        if self.variant not in {"FULL", "ABLATED"}:
            raise ValueError("variant must be FULL or ABLATED")
        if self.elapsed_ms < 0 or self.deadline_ms <= 0:
            raise ValueError("elapsed_ms must be non-negative and deadline_ms positive")
        if len(self.components) != len(set(self.components)):
            raise ValueError("components must use deduplicated canonical identities")
        if any(not item for item in self.components):
            raise ValueError("component identities must be non-empty")
        object.__setattr__(self, "utility", _decimal(self.utility, "utility"))
        cost = _decimal(self.cost, "cost")
        if cost < 0:
            raise ValueError("cost must be non-negative")
        object.__setattr__(self, "cost", cost)

    @property
    def met_deadline(self) -> bool:
        return self.elapsed_ms <= self.deadline_ms


@dataclass(frozen=True)
class AblationPair:
    target_component: str
    full: AblationOutcome
    ablated: AblationOutcome

    def __post_init__(self) -> None:
        if not self.target_component:
            raise ValueError("target_component is required")
        if self.full.variant != "FULL" or self.ablated.variant != "ABLATED":
            raise ValueError("pair must contain FULL and ABLATED outcomes")
        if self.full.case_id != self.ablated.case_id:
            raise ValueError("matched outcomes must share case_id")
        if self.full.input_fingerprint != self.ablated.input_fingerprint:
            raise ValueError("matched outcomes must share exact input_fingerprint")
        if self.full.deadline_ms != self.ablated.deadline_ms:
            raise ValueError("matched outcomes must use the same deadline budget")
        full_components = set(self.full.components)
        ablated_components = set(self.ablated.components)
        if self.target_component not in full_components:
            raise ValueError("target component must be present in FULL outcome")
        if self.target_component in ablated_components:
            raise ValueError("target component must be absent from ABLATED outcome")
        if full_components - {self.target_component} != ablated_components:
            raise ValueError("matched pair may differ only by the target component")

    @property
    def deadline_comparable(self) -> bool:
        return self.full.met_deadline == self.ablated.met_deadline

    @property
    def utility_delta(self) -> Decimal | None:
        if not self.deadline_comparable:
            return None
        return self.full.utility - self.ablated.utility

    @property
    def cost_delta(self) -> Decimal:
        return self.full.cost - self.ablated.cost

    @property
    def latency_delta_ms(self) -> int:
        return self.full.elapsed_ms - self.ablated.elapsed_ms


@dataclass(frozen=True)
class AblationSummary:
    target_component: str
    total_pairs: int
    comparable_pairs: int
    deadline_mismatch_pairs: int
    full_deadline_misses: int
    ablated_deadline_misses: int
    mean_utility_delta: Decimal | None
    mean_cost_delta: Decimal
    mean_latency_delta_ms: Decimal
    status: str


def summarize_ablation(target_component: str, pairs: Iterable[AblationPair]) -> AblationSummary:
    selected = list(pairs)
    if any(pair.target_component != target_component for pair in selected):
        raise ValueError("all pairs must target the requested component")

    comparable = [pair for pair in selected if pair.deadline_comparable]
    utility_deltas = [pair.utility_delta for pair in comparable]
    concrete_utility = [value for value in utility_deltas if value is not None]

    def mean(values: list[Decimal]) -> Decimal | None:
        if not values:
            return None
        return sum(values, Decimal("0")) / Decimal(len(values))

    cost_values = [pair.cost_delta for pair in selected]
    latency_values = [Decimal(pair.latency_delta_ms) for pair in selected]
    mean_cost = mean(cost_values) or Decimal("0")
    mean_latency = mean(latency_values) or Decimal("0")
    mean_utility = mean(concrete_utility)

    return AblationSummary(
        target_component=target_component,
        total_pairs=len(selected),
        comparable_pairs=len(comparable),
        deadline_mismatch_pairs=sum(not pair.deadline_comparable for pair in selected),
        full_deadline_misses=sum(not pair.full.met_deadline for pair in selected),
        ablated_deadline_misses=sum(not pair.ablated.met_deadline for pair in selected),
        mean_utility_delta=mean_utility,
        mean_cost_delta=mean_cost,
        mean_latency_delta_ms=mean_latency,
        status="DESCRIPTIVE_ONLY" if concrete_utility else "INCONCLUSIVE",
    )
