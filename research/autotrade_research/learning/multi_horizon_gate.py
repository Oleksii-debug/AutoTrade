"""Leakage-safe after-cost screen for longer-horizon BBO hypotheses."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any


SCHEMA = "autotrade-multi-horizon-after-cost-gate-v1"
_FAMILIES = ("MOMENTUM", "CONTRARIAN")
_LOOKBACKS = (1, 3, 5)
_HORIZONS = (3, 5, 10)
_COST_MULTIPLIERS = (Decimal("0"), Decimal("0.5"), Decimal("1"))


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValueError(f"{name} must be an exact decimal")
    try:
        result = Decimal(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an exact decimal") from None
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f"{name} is outside the supported range")
    return result


def _net(entry_ask: Decimal, exit_bid: Decimal, *, notional: Decimal,
         fee_bps: Decimal, slippage_bps: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 42
        slip = slippage_bps / Decimal(10000)
        fee = fee_bps / Decimal(10000)
        entry = entry_ask * (1 + slip)
        exit_price = exit_bid * (1 - slip)
        quantity = notional / entry
        gross = quantity * (exit_price - entry)
        fees = (notional + quantity * exit_price) * fee
        return gross - fees


def _candidate_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    return (candidate["family"], candidate["lookback_observations"],
            candidate["horizon_observations"],
            Decimal(candidate["signal_cost_multiplier"]))


def _score(rows, candidate, indices, *, notional, fee_bps, slippage_bps,
           maximum_gap_seconds):
    family = candidate["family"]
    lookback = candidate["lookback_observations"]
    horizon = candidate["horizon_observations"]
    multiplier = Decimal(candidate["signal_cost_multiplier"])
    outcomes = []
    for index in indices:
        if any(not 0 < (rows[position][4] - rows[position - 1][4]).total_seconds()
               <= maximum_gap_seconds
               for position in range(index - lookback + 1, index + horizon + 1)):
            continue
        feature = rows[index][3] - rows[index - lookback][3]
        signed = -feature if family == "CONTRARIAN" else feature
        ask, bid = rows[index][2], rows[index][1]
        with localcontext() as context:
            context.prec = 42
            fee = fee_bps / Decimal(10000)
            slip = slippage_bps / Decimal(10000)
            required_bid = ask * (1 + slip) * (1 + fee) / ((1 - slip) * (1 - fee))
            signal_floor = (required_bid - bid) * multiplier
        if signed > signal_floor:
            outcomes.append(_net(ask, rows[index + horizon][1], notional=notional,
                                 fee_bps=fee_bps, slippage_bps=slippage_bps))
    total = peak = drawdown = Decimal(0)
    wins = 0
    for outcome in outcomes:
        total += outcome
        peak = max(peak, total)
        drawdown = max(drawdown, peak - total)
        wins += outcome > 0
    return {"net_after_costs_usdt": format(total, "f"),
            "trade_count": len(outcomes), "win_count": wins,
            "max_drawdown_usdt": format(drawdown, "f")}


def evaluate_multi_horizon_gate(
    observations: Sequence[Mapping[str, object]], *,
    virtual_notional: object = "100", fee_bps_per_leg: object = "10",
    slippage_bps_per_leg: object = "5", train_fraction: object = "0.60",
    minimum_train_trades: int = 8, minimum_evaluation_trades: int = 5,
    maximum_evaluation_drawdown_usdt: object = "2",
    maximum_observation_gap_seconds: int = 180,
) -> dict[str, Any]:
    """Select on prior data, then gate once on the later untouched cut."""
    if type(observations) not in (list, tuple):
        raise TypeError("observations must be a concrete chronological sequence")
    notional = _decimal(virtual_notional, "virtual_notional", positive=True)
    fee = _decimal(fee_bps_per_leg, "fee_bps_per_leg")
    slippage = _decimal(slippage_bps_per_leg, "slippage_bps_per_leg")
    fraction = _decimal(train_fraction, "train_fraction", positive=True)
    drawdown_limit = _decimal(maximum_evaluation_drawdown_usdt,
                              "maximum_evaluation_drawdown_usdt", positive=True)
    if fraction >= 1 or not (0 <= fee <= 100 and 0 <= slippage <= 100):
        raise ValueError("cost or split configuration is outside the supported range")
    if type(minimum_train_trades) is not int or minimum_train_trades < 1:
        raise ValueError("minimum_train_trades must be positive")
    if type(minimum_evaluation_trades) is not int or minimum_evaluation_trades < 1:
        raise ValueError("minimum_evaluation_trades must be positive")
    if (type(maximum_observation_gap_seconds) is not int
            or maximum_observation_gap_seconds < 1):
        raise ValueError("maximum_observation_gap_seconds must be positive")

    rows = []
    prior_instant = None
    for item in observations:
        if type(item) is not dict:
            raise TypeError("each observation must be an exact mapping")
        available = item.get("available_at")
        try:
            instant = datetime.fromisoformat(available) if type(available) is str else None
        except ValueError:
            instant = None
        if (instant is None or instant.tzinfo is None
                or (prior_instant is not None and instant <= prior_instant)):
            raise ValueError("observations must have strictly increasing canonical timestamps")
        bid = _decimal(item.get("bid"), "bid", positive=True)
        ask = _decimal(item.get("ask"), "ask", positive=True)
        if bid > ask:
            raise ValueError("observation book is crossed")
        rows.append((available, bid, ask, (bid + ask) / Decimal(2), instant))
        prior_instant = instant

    split = int(Decimal(len(rows)) * fraction)
    candidates = [{"family": family, "lookback_observations": lookback,
                   "horizon_observations": horizon,
                   "signal_cost_multiplier": format(multiplier, "f")}
                  for family in _FAMILIES for lookback in _LOOKBACKS
                  for horizon in _HORIZONS for multiplier in _COST_MULTIPLIERS]
    scored = []
    for candidate in candidates:
        lookback = candidate["lookback_observations"]
        horizon = candidate["horizon_observations"]
        stop = split - horizon
        if stop <= lookback:
            continue
        score = _score(rows, candidate, range(lookback, stop), notional=notional,
                       fee_bps=fee, slippage_bps=slippage,
                       maximum_gap_seconds=maximum_observation_gap_seconds)
        scored.append((Decimal(score["net_after_costs_usdt"]),
                       score["trade_count"], _candidate_key(candidate),
                       candidate, score))
    sampled = [item for item in scored if item[1] >= minimum_train_trades]
    eligible = [item for item in sampled if item[0] > 0]
    selected = max(eligible, key=lambda item: (item[0], item[1], item[2])) if eligible else None
    base = {"schema": SCHEMA, "observation_count": len(rows),
            "train_end_index_exclusive": split, "candidate_count": len(scored),
            "cost_model": {"virtual_notional_usdt": format(notional, "f"),
                           "fee_bps_per_leg": format(fee, "f"),
                           "slippage_bps_per_leg": format(slippage, "f"),
                           "maximum_observation_gap_seconds": maximum_observation_gap_seconds,
                           "entry": "CONTEMPORANEOUS_ASK", "exit": "FUTURE_BID"},
            "selection_scope": "CHRONOLOGICAL_TRAIN_ONLY",
            "evaluation_scope": "LATER_UNTOUCHED_CUT",
            "automatic_trading_promotion": False, "network_orders": False,
            "trading_authority": False, "profitability_proven": False}
    if selected is None:
        rejected = max(sampled, key=lambda item: (item[0], item[1], item[2])) if sampled else None
        return {**base,
            "status": "NO_POSITIVE_TRAIN_CANDIDATE" if rejected
                      else "INSUFFICIENT_TRAIN_SAMPLE",
            "selected_candidate": None,
            "best_rejected_candidate": None if rejected is None else rejected[3],
            "best_rejected_train": None if rejected is None else rejected[4]}
    _train_net, _train_trades, _key, candidate, train_score = selected
    horizon = candidate["horizon_observations"]
    evaluation = _score(rows, candidate,
        range(max(split, candidate["lookback_observations"]), len(rows) - horizon),
        notional=notional, fee_bps=fee, slippage_bps=slippage,
        maximum_gap_seconds=maximum_observation_gap_seconds)
    qualified = (Decimal(train_score["net_after_costs_usdt"]) > 0
        and evaluation["trade_count"] >= minimum_evaluation_trades
        and Decimal(evaluation["net_after_costs_usdt"]) > 0
        and Decimal(evaluation["max_drawdown_usdt"]) < drawdown_limit)
    return {**base,
        "status": "COMPLETE" if evaluation["trade_count"] >= minimum_evaluation_trades
                  else "INSUFFICIENT_EVALUATION_SAMPLE",
        "selected_candidate": candidate, "train": train_score,
        "evaluation": evaluation, "qualifies_for_research_simulation": qualified}


__all__ = ["SCHEMA", "evaluate_multi_horizon_gate"]
