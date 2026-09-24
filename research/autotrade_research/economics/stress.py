from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Iterable

from .after_cost import CostBreakdown, EconomicQualification, TradeEconomics, qualify_after_cost_edge


@dataclass(frozen=True, slots=True)
class CostScenario:
    scenario_id: str
    costs: CostBreakdown

    def __post_init__(self) -> None:
        if not isinstance(self.scenario_id, str) or not self.scenario_id.strip():
            raise ValueError("scenario_id is required")


@dataclass(frozen=True, slots=True)
class StressQualification:
    disposition: str
    scenarios: tuple[tuple[str, EconomicQualification], ...]
    best_expected_net_pnl: Decimal
    worst_expected_net_pnl: Decimal
    reason_codes: tuple[str, ...]
    live_authority_granted: bool = False


def qualify_cost_stress(
    trade: TradeEconomics,
    scenarios: Iterable[CostScenario],
    *,
    max_loss_budget,
    minimum_order_notional=Decimal("0"),
    proposed_notional=None,
) -> StressQualification:
    materialized = tuple(scenarios)
    if not materialized:
        raise ValueError("at least one cost scenario is required")
    ids = [scenario.scenario_id for scenario in materialized]
    if len(ids) != len(set(ids)):
        raise ValueError("scenario_id must be unique")

    qualified: list[tuple[str, EconomicQualification]] = []
    for scenario in materialized:
        result = qualify_after_cost_edge(
            replace(trade, costs=scenario.costs),
            max_loss_budget=max_loss_budget,
            minimum_order_notional=minimum_order_notional,
            proposed_notional=proposed_notional,
        )
        qualified.append((scenario.scenario_id, result))

    net_values = tuple(result.expected_net_pnl for _, result in qualified)
    reasons = tuple(
        f"SCENARIO_REJECTED:{scenario_id}"
        for scenario_id, result in qualified
        if result.disposition != "ADMISSIBLE_FOR_RESEARCH"
    )
    return StressQualification(
        disposition="ROBUST_FOR_RESEARCH" if not reasons else "REJECT",
        scenarios=tuple(qualified),
        best_expected_net_pnl=max(net_values),
        worst_expected_net_pnl=min(net_values),
        reason_codes=reasons,
        live_authority_granted=False,
    )
