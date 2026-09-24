"""Deterministic cost/risk-aware portfolio allocation foundation.

This module produces proposed targets only. It never sends orders and never
treats simulated or expected returns as evidence of profitability.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Mapping, Sequence


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


def _positive(value, *, name: str, allow_zero: bool = False) -> Decimal:
    result = _decimal(value, name=name)
    if result < 0 or (result == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'}")
    return result


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


@dataclass(frozen=True)
class AllocationCandidate:
    symbol: str
    desired_notional: Decimal
    price: Decimal
    lot_size: Decimal
    cost_rate: Decimal = Decimal("0")
    capital_requirement_rate: Decimal = Decimal("1")

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        desired_notional,
        price,
        lot_size,
        cost_rate=0,
        capital_requirement_rate=1,
    ) -> "AllocationCandidate":
        return cls(
            symbol=_text(symbol, name="symbol"),
            desired_notional=_decimal(desired_notional, name="desired_notional"),
            price=_positive(price, name="price"),
            lot_size=_positive(lot_size, name="lot_size"),
            cost_rate=_positive(cost_rate, name="cost_rate", allow_zero=True),
            capital_requirement_rate=_positive(
                capital_requirement_rate,
                name="capital_requirement_rate",
            ),
        )


@dataclass(frozen=True)
class AllocationPolicy:
    cash_available: Decimal
    max_gross_notional: Decimal
    max_net_notional: Decimal
    max_symbol_notional: Decimal
    max_total_cost: Decimal
    max_stress_loss: Decimal
    max_iterations: int = 64
    min_scale_tolerance: Decimal = Decimal("0.000001")

    @classmethod
    def create(
        cls,
        *,
        cash_available,
        max_gross_notional,
        max_net_notional,
        max_symbol_notional,
        max_total_cost,
        max_stress_loss,
        max_iterations: int = 64,
        min_scale_tolerance="0.000001",
    ) -> "AllocationPolicy":
        if not isinstance(max_iterations, int) or isinstance(max_iterations, bool) or max_iterations < 1:
            raise ValueError("max_iterations must be a positive integer")
        return cls(
            cash_available=_positive(cash_available, name="cash_available", allow_zero=True),
            max_gross_notional=_positive(max_gross_notional, name="max_gross_notional", allow_zero=True),
            max_net_notional=_positive(max_net_notional, name="max_net_notional", allow_zero=True),
            max_symbol_notional=_positive(max_symbol_notional, name="max_symbol_notional", allow_zero=True),
            max_total_cost=_positive(max_total_cost, name="max_total_cost", allow_zero=True),
            max_stress_loss=_positive(max_stress_loss, name="max_stress_loss", allow_zero=True),
            max_iterations=max_iterations,
            min_scale_tolerance=_positive(min_scale_tolerance, name="min_scale_tolerance"),
        )


@dataclass(frozen=True)
class AllocationTarget:
    symbol: str
    quantity: Decimal
    notional: Decimal
    estimated_cost: Decimal


@dataclass(frozen=True)
class AllocationResult:
    status: str
    scale: Decimal
    targets: tuple[AllocationTarget, ...]
    gross_notional: Decimal
    net_notional: Decimal
    estimated_cost: Decimal
    worst_stress_loss: Decimal
    cash_required: Decimal
    reason: str


def _round_quantity(notional: Decimal, price: Decimal, lot_size: Decimal) -> Decimal:
    if notional == 0:
        return Decimal("0")
    absolute_quantity = abs(notional) / price
    lots = (absolute_quantity / lot_size).to_integral_value(rounding=ROUND_DOWN)
    quantity = lots * lot_size
    return quantity if notional > 0 else -quantity


def _evaluate(
    candidates: Sequence[AllocationCandidate],
    policy: AllocationPolicy,
    stress_scenarios: Mapping[str, Mapping[str, Decimal]],
    scale: Decimal,
) -> AllocationResult:
    targets: list[AllocationTarget] = []
    notionals: dict[str, Decimal] = {}
    total_cost = Decimal("0")
    capital_required = Decimal("0")

    for candidate in candidates:
        scaled = candidate.desired_notional * scale
        quantity = _round_quantity(scaled, candidate.price, candidate.lot_size)
        notional = quantity * candidate.price
        cost = abs(notional) * candidate.cost_rate
        notionals[candidate.symbol] = notional
        total_cost += cost
        capital_required += abs(notional) * candidate.capital_requirement_rate
        targets.append(
            AllocationTarget(
                symbol=candidate.symbol,
                quantity=quantity,
                notional=notional,
                estimated_cost=cost,
            )
        )

    gross = sum((abs(value) for value in notionals.values()), Decimal("0"))
    net = abs(sum(notionals.values(), Decimal("0")))
    # Short-sale proceeds are never treated as spendable capital. Each
    # candidate must declare an explicit capital/margin requirement; the
    # conservative default is 100% of absolute notional.
    cash_required = capital_required + total_cost

    worst_stress_loss = Decimal("0")
    for scenario in stress_scenarios.values():
        pnl = sum(
            (
                notional * scenario.get(symbol, Decimal("0"))
                for symbol, notional in notionals.items()
            ),
            Decimal("0"),
        )
        worst_stress_loss = max(worst_stress_loss, -pnl)

    symbol_ok = all(abs(value) <= policy.max_symbol_notional for value in notionals.values())
    feasible = (
        symbol_ok
        and gross <= policy.max_gross_notional
        and net <= policy.max_net_notional
        and total_cost <= policy.max_total_cost
        and worst_stress_loss <= policy.max_stress_loss
        and cash_required <= policy.cash_available
    )

    return AllocationResult(
        status="ALLOCATED" if feasible else "INFEASIBLE",
        scale=scale,
        targets=tuple(targets),
        gross_notional=gross,
        net_notional=net,
        estimated_cost=total_cost,
        worst_stress_loss=worst_stress_loss,
        cash_required=cash_required,
        reason="all hard constraints satisfied" if feasible else "one or more hard constraints failed",
    )


def allocate_targets(
    candidates: Sequence[AllocationCandidate],
    policy: AllocationPolicy,
    *,
    stress_scenarios: Mapping[str, Mapping[str, object]] | None = None,
) -> AllocationResult:
    """Return the largest uniformly scaled feasible target set.

    When no positive lot can be proven feasible inside the bounded search,
    returns an explicit all-cash no-increase fallback.
    """

    if not candidates:
        return AllocationResult(
            status="NO_INCREASE_FALLBACK",
            scale=Decimal("0"),
            targets=(),
            gross_notional=Decimal("0"),
            net_notional=Decimal("0"),
            estimated_cost=Decimal("0"),
            worst_stress_loss=Decimal("0"),
            cash_required=Decimal("0"),
            reason="no allocation candidates",
        )

    symbols = [candidate.symbol for candidate in candidates]
    if len(symbols) != len(set(symbols)):
        raise ValueError("candidate symbols must be unique")

    normalized_stress = {
        _text(name, name="scenario name"): {
            _text(symbol, name="stress symbol"): _decimal(shock, name=f"stress shock {symbol}")
            for symbol, shock in scenario.items()
        }
        for name, scenario in (stress_scenarios or {}).items()
    }
    unknown = sorted(
        {
            symbol
            for scenario in normalized_stress.values()
            for symbol in scenario
            if symbol not in set(symbols)
        }
    )
    if unknown:
        raise ValueError(f"stress scenarios reference unknown symbols: {', '.join(unknown)}")

    required_symbols = set(symbols)
    for scenario_name, scenario in normalized_stress.items():
        missing = sorted(required_symbols - set(scenario))
        if missing:
            raise ValueError(
                f"stress scenario {scenario_name} is missing explicit shocks for: {', '.join(missing)}"
            )

    requested = _evaluate(candidates, policy, normalized_stress, Decimal("1"))
    if requested.status == "ALLOCATED":
        return requested

    low = Decimal("0")
    high = Decimal("1")
    best = _evaluate(candidates, policy, normalized_stress, low)

    for _ in range(policy.max_iterations):
        if high - low <= policy.min_scale_tolerance:
            break
        mid = (low + high) / Decimal("2")
        result = _evaluate(candidates, policy, normalized_stress, mid)
        if result.status == "ALLOCATED":
            best = result
            low = mid
        else:
            high = mid

    if best.gross_notional == 0:
        return AllocationResult(
            status="NO_INCREASE_FALLBACK",
            scale=Decimal("0"),
            targets=tuple(
                AllocationTarget(
                    symbol=candidate.symbol,
                    quantity=Decimal("0"),
                    notional=Decimal("0"),
                    estimated_cost=Decimal("0"),
                )
                for candidate in candidates
            ),
            gross_notional=Decimal("0"),
            net_notional=Decimal("0"),
            estimated_cost=Decimal("0"),
            worst_stress_loss=Decimal("0"),
            cash_required=Decimal("0"),
            reason="bounded search found no positive-lot feasible allocation; remain in cash",
        )

    return AllocationResult(
        status="ALLOCATED",
        scale=best.scale,
        targets=best.targets,
        gross_notional=best.gross_notional,
        net_notional=best.net_notional,
        estimated_cost=best.estimated_cost,
        worst_stress_loss=best.worst_stress_loss,
        cash_required=best.cash_required,
        reason="requested allocation was infeasible; uniformly reduced to the largest verified feasible target found",
    )
