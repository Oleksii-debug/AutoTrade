from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


ZERO = Decimal("0")


def _decimal(name: str, value) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be finite decimal")
    return result


def _non_negative(name: str, value) -> Decimal:
    result = _decimal(name, value)
    if result < ZERO:
        raise ValueError(f"{name} cannot be negative")
    return result


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    commission: Decimal = ZERO
    spread: Decimal = ZERO
    slippage: Decimal = ZERO
    financing: Decimal = ZERO
    funding: Decimal = ZERO
    borrow: Decimal = ZERO
    market_data: Decimal = ZERO
    model_compute: Decimal = ZERO
    infrastructure: Decimal = ZERO
    tax_estimate: Decimal = ZERO

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, _non_negative(name, getattr(self, name)))

    @property
    def total(self) -> Decimal:
        return sum((getattr(self, name) for name in self.__dataclass_fields__), ZERO)


@dataclass(frozen=True, slots=True)
class TradeEconomics:
    capital_at_risk: Decimal
    expected_gross_pnl: Decimal
    worst_case_loss: Decimal
    costs: CostBreakdown
    minimum_practical_advantage: Decimal = ZERO

    def __post_init__(self) -> None:
        capital = _decimal("capital_at_risk", self.capital_at_risk)
        if capital <= ZERO:
            raise ValueError("capital_at_risk must be positive and finite")
        object.__setattr__(self, "capital_at_risk", capital)
        object.__setattr__(
            self, "expected_gross_pnl", _decimal("expected_gross_pnl", self.expected_gross_pnl)
        )
        object.__setattr__(
            self, "worst_case_loss", _non_negative("worst_case_loss", self.worst_case_loss)
        )
        object.__setattr__(
            self,
            "minimum_practical_advantage",
            _non_negative("minimum_practical_advantage", self.minimum_practical_advantage),
        )

    @property
    def expected_net_pnl(self) -> Decimal:
        return self.expected_gross_pnl - self.costs.total

    @property
    def expected_net_return(self) -> Decimal:
        return self.expected_net_pnl / self.capital_at_risk


@dataclass(frozen=True, slots=True)
class EconomicQualification:
    disposition: str
    expected_net_pnl: Decimal
    expected_net_return: Decimal
    total_cost: Decimal
    required_net_pnl: Decimal
    reason_codes: tuple[str, ...]
    live_authority_granted: bool = False


def qualify_after_cost_edge(
    trade: TradeEconomics,
    *,
    max_loss_budget,
    minimum_order_notional=ZERO,
    proposed_notional=None,
) -> EconomicQualification:
    """Research/paper economics gate. It is not a profitability or live-trade claim."""

    loss_budget = _non_negative("max_loss_budget", max_loss_budget)
    minimum_notional = _non_negative("minimum_order_notional", minimum_order_notional)
    notional = (
        trade.capital_at_risk
        if proposed_notional is None
        else _non_negative("proposed_notional", proposed_notional)
    )

    reasons: list[str] = []
    if notional < minimum_notional:
        reasons.append("BELOW_MINIMUM_NOTIONAL")
    if trade.worst_case_loss > loss_budget:
        reasons.append("LOSS_BUDGET_EXCEEDED")
    required = trade.minimum_practical_advantage
    if trade.expected_net_pnl <= required:
        reasons.append("NO_AFTER_COST_PRACTICAL_EDGE")
    return EconomicQualification(
        disposition="ADMISSIBLE_FOR_RESEARCH" if not reasons else "REJECT",
        expected_net_pnl=trade.expected_net_pnl,
        expected_net_return=trade.expected_net_return,
        total_cost=trade.costs.total,
        required_net_pnl=required,
        reason_codes=tuple(reasons),
        live_authority_granted=False,
    )
