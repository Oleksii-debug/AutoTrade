"""Read-only benchmark math for the canonical historical ZERO journal result.

This is a comparison report, never an execution or accounting authority. The
agent's cash/position/equity are read from the reconciled canonical simulator;
cash and full-cash buy-and-hold are explicitly hypothetical scenarios.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from mvp.autotrade_mvp.exact_decimal import (
    as_fraction, parse_bounded_exact_decimal, round_fraction_to_quantum,
)
from mvp.autotrade_mvp.simulation_session import FEE_RATE


_TICK = Decimal("0.00000001")


def _text(value: Decimal) -> str:
    return format(value, "f")


def _fill(mark: Decimal, bps: Decimal, side: str) -> Decimal:
    value = as_fraction(mark) * (1 + as_fraction(bps) / 10000 if side == "BUY"
                                  else 1 - as_fraction(bps) / 10000)
    return round_fraction_to_quantum(value, _TICK,
                                     mode="CEILING" if side == "BUY" else "FLOOR")


def holdout_report(simulation: dict, *, execution_prices: list[str],
                   execution_dates: list[datetime], virtual_cash: str,
                   slippage_bps: str) -> dict | None:
    """Compare only the completed prefix; never value at a future market step."""
    if simulation.get("status") == "UNKNOWN":
        return None
    count = simulation.get("completed_episodes")
    decisions = simulation.get("decisions")
    if (type(count) is not int or not 1 <= count <= len(execution_prices)
        or len(execution_prices) != len(execution_dates)
        or type(decisions) is not list or len(decisions) != count
        or "cash" not in simulation or "position" not in simulation):
        # A mocked or incomplete result cannot become performance evidence.
        return None
    starting = parse_bounded_exact_decimal(virtual_cash)
    bps = parse_bounded_exact_decimal(slippage_bps)
    first = parse_bounded_exact_decimal(execution_prices[0])
    mark = parse_bounded_exact_decimal(execution_prices[count - 1])
    cash = parse_bounded_exact_decimal(simulation["cash"])
    position = parse_bounded_exact_decimal(simulation["position"])
    if starting <= 0 or first <= 0 or mark <= 0 or position < 0 or not 0 <= bps <= 100:
        raise ValueError("historical benchmark inputs are invalid")
    marked_equity = cash + position * mark
    if parse_bounded_exact_decimal(decisions[-1]["equity"]) != marked_equity:
        raise ValueError("canonical agent mark conflicts with comparative report")
    peak = starting
    max_drawdown = Decimal(0)
    for index, decision in enumerate(decisions):
        if decision.get("episode") != index + 1 or decision.get("reconciled") is not True:
            raise ValueError("canonical agent decision history is incomplete")
        equity = parse_bounded_exact_decimal(decision["equity"])
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
    buy_price = _fill(first, bps, "BUY")
    sell_price = _fill(mark, bps, "SELL")
    hold_quantity = round_fraction_to_quantum(
        as_fraction(starting) / as_fraction(buy_price * (1 + FEE_RATE)),
        _TICK, mode="FLOOR",
    )
    spent = hold_quantity * buy_price * (1 + FEE_RATE)
    if spent > starting:
        raise ValueError("buy-and-hold exceeds its hypothetical cash budget")
    hold_liquidation = starting - spent + hold_quantity * sell_price * (1 - FEE_RATE)
    agent_liquidation = cash + position * sell_price * (1 - FEE_RATE)
    return {
        "status": "HYPOTHETICAL_COST_SCENARIO_NOT_EXCHANGE_FILL_EVIDENCE",
        "through_episode": count,
        "asof_execution_open_utc": execution_dates[count - 1].isoformat(),
        "starting_cash_usd": _text(starting),
        "agent_cash_usd": _text(cash),
        "agent_mark_equity_usd": _text(marked_equity),
        "agent_mark_pnl_usd": _text(marked_equity - starting),
        "agent_liquidation_scenario_equity_usd": _text(agent_liquidation),
        "agent_max_mark_drawdown_fraction": _text(max_drawdown),
        "cash_baseline_equity_usd": _text(starting),
        "buy_hold_liquidation_scenario_equity_usd": _text(hold_liquidation),
        "buy_hold_liquidation_scenario_pnl_usd": _text(hold_liquidation - starting),
        "scenario_fee_rate": _text(FEE_RATE),
        "adverse_slippage_bps": _text(bps),
    }
