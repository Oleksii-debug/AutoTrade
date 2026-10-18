"""Read-only benchmark math for the canonical historical ZERO journal result.

This is a comparison report, never an execution or accounting authority. The
agent's cash/position/equity are read from the reconciled canonical simulator;
cash and full-cash buy-and-hold are explicitly hypothetical scenarios.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from hashlib import sha256
from math import isqrt

from mvp.autotrade_mvp.exact_decimal import (
    as_fraction, parse_bounded_exact_decimal, round_fraction_to_quantum,
)
from mvp.autotrade_mvp.simulation_session import FEE_RATE


_TICK = Decimal("0.00000001")
_REGIME_LOOKBACK = 24
_REGIME_TREND_THRESHOLD = Decimal("0.001")
_REGIME_VOLATILITY_THRESHOLD = Decimal("0.0025")
_BOOTSTRAP_SAMPLES = 256
_MIN_BOOTSTRAP_EPISODES = 8
_WILSON_95_Z = Decimal("1.959963984540054")


def _text(value: Decimal) -> str:
    return format(value, "f")


def _fill(mark: Decimal, bps: Decimal, side: str) -> Decimal:
    value = as_fraction(mark) * (1 + as_fraction(bps) / 10000 if side == "BUY"
                                  else 1 - as_fraction(bps) / 10000)
    return round_fraction_to_quantum(value, _TICK,
                                     mode="CEILING" if side == "BUY" else "FLOOR")


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 42
        return numerator / denominator


def _regime_before(prices: list[Decimal], index: int) -> tuple[str, str | None]:
    """Classify an episode using execution opens strictly before that episode."""
    history = prices[max(0, index - _REGIME_LOOKBACK):index]
    if len(history) < 2:
        return "INSUFFICIENT_PRIOR_HISTORY", None
    changes = [_ratio(right - left, left) for left, right in zip(history, history[1:])]
    trailing = _ratio(history[-1] - history[0], history[0])
    mean_absolute_change = sum(abs(value) for value in changes) / len(changes)
    trend = ("UP" if trailing >= _REGIME_TREND_THRESHOLD else
             "DOWN" if trailing <= -_REGIME_TREND_THRESHOLD else "RANGE")
    volatility = ("ELEVATED" if mean_absolute_change >= _REGIME_VOLATILITY_THRESHOLD
                  else "QUIET")
    return f"{trend}_{volatility}", _text(trailing)


def _causal_regimes(prices: list[Decimal], dates: list[datetime],
                    equities: list[Decimal], starting: Decimal) -> dict:
    buckets: dict[str, dict] = {}
    prior_equity = starting
    assignments = []
    for index, equity in enumerate(equities):
        regime, trailing = _regime_before(prices, index)
        result = equity - prior_equity
        bucket = buckets.setdefault(regime, {
            "episodes": 0, "cost_adjusted_mark_pnl_usd": Decimal(0),
            "positive_episode_returns": 0, "nonpositive_episode_returns": 0,
        })
        bucket["episodes"] += 1
        bucket["cost_adjusted_mark_pnl_usd"] += result
        bucket["positive_episode_returns" if result > 0 else
               "nonpositive_episode_returns"] += 1
        assignments.append({
            "episode": index + 1,
            "regime": regime,
            "regime_information_cut_utc": (
                dates[index - 1].isoformat() if index else None),
            "trailing_price_return_fraction": trailing,
        })
        prior_equity = equity
    return {
        "status": "CAUSAL_TIME_SLICE_NOT_CAUSAL_EFFECT_OR_PROFIT_CLAIM",
        "classification": {
            "lookback_execution_opens": _REGIME_LOOKBACK,
            "trend_threshold_fraction": _text(_REGIME_TREND_THRESHOLD),
            "mean_absolute_change_threshold_fraction": _text(
                _REGIME_VOLATILITY_THRESHOLD),
            "information_rule": "ONLY_EXECUTION_OPENS_STRICTLY_BEFORE_EPISODE",
        },
        "assignments": assignments,
        "summary": {
            name: {**values,
                   "cost_adjusted_mark_pnl_usd": _text(
                       values["cost_adjusted_mark_pnl_usd"])}
            for name, values in sorted(buckets.items())
        },
    }


def _wilson_upper_95(missed: int, intents: int) -> str | None:
    if intents == 0:
        return None
    with localcontext() as context:
        context.prec = 42
        n = Decimal(intents)
        rate = Decimal(missed) / n
        z2 = _WILSON_95_Z * _WILSON_95_Z
        centre = rate + z2 / (2 * n)
        radius = _WILSON_95_Z * (
            (rate * (1 - rate) / n + z2 / (4 * n * n)).sqrt())
        return _text(min(Decimal(1), (centre + radius) / (1 + z2 / n)))


def _missed_fills(decisions: list[dict]) -> dict:
    intents = [row for row in decisions if row.get("decision") in {"BUY", "REDUCE"}]
    filled = [row for row in intents if row.get("status") == "FILLED"]
    missed = [row for row in intents if row.get("status") != "FILLED"]
    reasons: dict[str, int] = {}
    for row in missed:
        reason = row.get("status")
        if type(reason) is not str or not reason:
            raise ValueError("canonical agent execution status is missing")
        reasons[reason] = reasons.get(reason, 0) + 1
    if any(row.get("order_id") is None or row.get("fill_id") is None for row in filled):
        raise ValueError("FILLED decision lacks canonical order/fill evidence")
    if any(row.get("status") == "FILLED"
           and row.get("decision") not in {"BUY", "REDUCE"} for row in decisions):
        raise ValueError("FILLED status lacks a canonical order-intent")
    return {
        "definition": "BUY_OR_REDUCE_INTENT_WITHOUT_RECONCILED_FILLED_STATUS",
        "execution_intents": len(intents),
        "reconciled_fills": len(filled),
        "missed_fills": len(missed),
        "missed_fill_rate_fraction": (
            _text(_ratio(Decimal(len(missed)), Decimal(len(intents)))) if intents else None),
        "missed_fill_rate_wilson_95_upper_fraction": _wilson_upper_95(
            len(missed), len(intents)),
        "missed_fill_reasons": dict(sorted(reasons.items())),
        "non_order_decisions": len(decisions) - len(intents),
        "limitation": (
            "OHLCV cannot reveal venue fills that would have been missed; this counts "
            "only simulator order-intents lacking reconciled fill evidence."),
    }


def _percentile_interval(values: list[Decimal]) -> dict:
    ordered = sorted(values)
    lower = len(ordered) * 25 // 1000
    upper = (len(ordered) * 975 + 999) // 1000 - 1
    return {"lower": _text(ordered[lower]), "upper": _text(ordered[upper])}


def _bootstrap_uncertainty(returns: list[Decimal]) -> dict:
    if len(returns) < _MIN_BOOTSTRAP_EPISODES:
        return {
            "status": "INSUFFICIENT_COMPLETED_EPISODES",
            "minimum_completed_episodes": _MIN_BOOTSTRAP_EPISODES,
            "completed_episodes": len(returns),
            "limitations": [
                "No interval is emitted for a sample too small for bounded resampling.",
                "Historical simulation is not evidence of future or real-money profit.",
            ],
        }
    block = max(2, isqrt(len(returns)))
    seed = sha256("|".join(_text(value) for value in returns).encode("ascii")).hexdigest()
    net_results = []
    drawdown_results = []
    sharpe_results = []
    for sample in range(_BOOTSTRAP_SAMPLES):
        sampled = []
        block_number = 0
        while len(sampled) < len(returns):
            digest = sha256(f"{seed}:{sample}:{block_number}".encode("ascii")).digest()
            start = int.from_bytes(digest, "big") % len(returns)
            sampled.extend(returns[(start + offset) % len(returns)]
                           for offset in range(block))
            block_number += 1
        sampled = sampled[:len(returns)]
        with localcontext() as context:
            context.prec = 42
            equity = peak = Decimal(1)
            maximum_drawdown = Decimal(0)
            for value in sampled:
                equity *= 1 + value
                peak = max(peak, equity)
                maximum_drawdown = max(
                    maximum_drawdown, (peak - equity) / peak)
            mean = sum(sampled) / len(sampled)
            variance = sum((value - mean) ** 2 for value in sampled) / (len(sampled) - 1)
            volatility = variance.sqrt()
            if volatility > 0:
                sharpe_results.append(mean / volatility)
        net_results.append(equity - 1)
        drawdown_results.append(maximum_drawdown)
    result = {
        "status": "ESTIMATED_SAMPLING_UNCERTAINTY_ONLY",
        "method": "DETERMINISTIC_CIRCULAR_MOVING_BLOCK_BOOTSTRAP",
        "confidence_fraction": "0.95",
        "samples": _BOOTSTRAP_SAMPLES,
        "block_length_episodes": block,
        "seed_sha256": seed,
        "cost_adjusted_compounded_return_fraction_interval": _percentile_interval(
            net_results),
        "max_drawdown_fraction_interval": _percentile_interval(drawdown_results),
        "limitations": [
            "Intervals describe resampling uncertainty in the completed historical prefix only.",
            "They exclude archive publication delay, order-book liquidity, model selection, and regime-shift error.",
            "Historical simulation is not evidence of future or real-money profit.",
        ],
    }
    result["hourly_sharpe_zero_cash_rate_unannualized_interval"] = (
        _percentile_interval(sharpe_results) if len(sharpe_results) == _BOOTSTRAP_SAMPLES
        else None)
    return result


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
    completed_dates = execution_dates[:count]
    if (any(value.tzinfo is None or value.utcoffset() != timedelta(0)
            for value in completed_dates)
            or any(right <= left for left, right in zip(completed_dates, completed_dates[1:]))):
        raise ValueError("historical execution dates must be strictly ordered UTC values")
    marked_equity = cash + position * mark
    if parse_bounded_exact_decimal(decisions[-1]["equity"]) != marked_equity:
        raise ValueError("canonical agent mark conflicts with comparative report")
    peak = starting
    max_drawdown = Decimal(0)
    curve = []
    returns = []
    prior_equity = starting
    prior_position = Decimal(0)
    turnover_proxy = Decimal(0)
    equities = []
    completed_prices = []
    for index, decision in enumerate(decisions):
        if decision.get("episode") != index + 1 or decision.get("reconciled") is not True:
            raise ValueError("canonical agent decision history is incomplete")
        equity = parse_bounded_exact_decimal(decision["equity"])
        position_at_step = parse_bounded_exact_decimal(decision["position"])
        cash_at_step = parse_bounded_exact_decimal(decision["cash"])
        open_at_step = parse_bounded_exact_decimal(execution_prices[index])
        if (open_at_step <= 0 or position_at_step < 0 or cash_at_step < 0 or
            cash_at_step + position_at_step * open_at_step != equity):
            raise ValueError("canonical agent equity curve conflicts with account state")
        completed_prices.append(open_at_step)
        equities.append(equity)
        if prior_equity <= 0:
            raise ValueError("canonical agent equity must remain positive")
        returns.append((equity - prior_equity) / prior_equity)
        turnover_proxy += abs(position_at_step - prior_position) * open_at_step
        curve.append({"episode": index + 1,
                      "execution_open_utc": execution_dates[index].isoformat(),
                      "cash_usd": _text(cash_at_step),
                      "position": _text(position_at_step),
                      "mark_equity_usd": _text(equity)})
        prior_equity, prior_position = equity, position_at_step
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
    with localcontext() as context:
        context.prec = 42
        mean = sum(returns) / len(returns)
        variance = (sum((value - mean) ** 2 for value in returns) /
                    (len(returns) - 1) if len(returns) > 1 else None)
        volatility = variance.sqrt() if variance is not None else None
        sharpe = mean / volatility if volatility is not None and volatility > 0 else None
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
        "agent_equity_curve": curve,
        "agent_hourly_return_sample_volatility_fraction": (
            _text(volatility) if volatility is not None else None),
        "agent_hourly_sharpe_zero_cash_rate_unannualized": (
            _text(sharpe) if sharpe is not None else None),
        "agent_gross_mark_turnover_proxy_fraction": _text(turnover_proxy / starting),
        "cash_baseline_equity_usd": _text(starting),
        "buy_hold_liquidation_scenario_equity_usd": _text(hold_liquidation),
        "buy_hold_liquidation_scenario_pnl_usd": _text(hold_liquidation - starting),
        "scenario_fee_rate": _text(FEE_RATE),
        "adverse_slippage_bps": _text(bps),
        "missed_fill_metrics": _missed_fills(decisions),
        "causal_regime_analysis": _causal_regimes(
            completed_prices, completed_dates, equities, starting),
        "conservative_uncertainty": _bootstrap_uncertainty(returns),
    }
