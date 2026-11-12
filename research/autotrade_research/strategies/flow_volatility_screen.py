"""Locked-final screen for causal WhiteBIT flow/volatility interactions."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path

from .trade_flow_screen import _hold, _read_rows
from ..integrations.whitebit_public import _symbol


SCHEMA = "autotrade-whitebit-flow-volatility-screen-v1"
CANDIDATE_REGISTRY_VERSION = "FLOW_VOLATILITY_PREREGISTERED_V1"
REGIMES = ("EXPANSION", "CONTRACTION")
SIDES = ("MOMENTUM", "CONTRARIAN")


@dataclass(frozen=True)
class FlowVolatilityCandidate:
    regime: str
    side: str
    lookback: int
    imbalance_threshold: str
    volume_multiplier: str
    range_multiplier: str
    horizon: int


def candidates() -> tuple[FlowVolatilityCandidate, ...]:
    return tuple(
        FlowVolatilityCandidate(regime, side, lookback, imbalance, volume, span, horizon)
        for regime in REGIMES
        for side in SIDES
        for lookback in (6, 12)
        for imbalance in ("0.2", "0.5")
        for volume in ("1.5", "2.5")
        for span in ("1.25", "2")
        for horizon in (3, 12)
    )


def _features(rows: list[dict], index: int, candidate: FlowVolatilityCandidate):
    prior = rows[index - candidate.lookback:index]
    current = rows[index]
    buy = Decimal(current["aggressive_buy_quote_quantity"])
    sell = Decimal(current["aggressive_sell_quote_quantity"])
    total = buy + sell
    if total <= 0:
        return None
    imbalance = (buy - sell) / total
    prior_volumes = [
        Decimal(row["aggressive_buy_quote_quantity"])
        + Decimal(row["aggressive_sell_quote_quantity"])
        for row in prior
    ]
    average_volume = sum(prior_volumes, Decimal(0)) / len(prior_volumes)
    if average_volume <= 0:
        return None
    volume_ratio = total / average_volume
    prior_ranges = []
    for row in prior:
        first = Decimal(row["first_price"])
        if first <= 0:
            return None
        prior_ranges.append(
            (Decimal(row["high_price"]) - Decimal(row["low_price"])) / first
        )
    average_range = sum(prior_ranges, Decimal(0)) / len(prior_ranges)
    current_first = Decimal(current["first_price"])
    if current_first <= 0 or average_range <= 0:
        return None
    current_range = (
        Decimal(current["high_price"]) - Decimal(current["low_price"])
    ) / current_first
    return imbalance, volume_ratio, current_range / average_range


def _simulate(
    rows: list[dict], candidate: FlowVolatilityCandidate, cost: Decimal,
) -> dict:
    if candidate.regime not in REGIMES or candidate.side not in SIDES:
        raise ValueError("unknown flow-volatility candidate")
    equity = Decimal(1)
    peak = equity
    maximum_drawdown = Decimal(0)
    trades = wins = 0
    index = candidate.lookback
    imbalance_threshold = Decimal(candidate.imbalance_threshold)
    volume_multiplier = Decimal(candidate.volume_multiplier)
    range_multiplier = Decimal(candidate.range_multiplier)
    while index + candidate.horizon < len(rows):
        values = _features(rows, index, candidate)
        direction = 0
        if values is not None:
            imbalance, volume_ratio, range_ratio = values
            flow_direction = (
                1 if imbalance >= imbalance_threshold
                else -1 if imbalance <= -imbalance_threshold else 0
            )
            regime_passed = (
                range_ratio >= range_multiplier
                if candidate.regime == "EXPANSION"
                else range_ratio <= Decimal(1) / range_multiplier
            )
            if flow_direction and volume_ratio >= volume_multiplier and regime_passed:
                direction = flow_direction
                if candidate.side == "CONTRARIAN":
                    direction = -direction
        if not direction:
            index += 1
            continue
        # The signal bucket is complete before the next bucket opens.
        entry = Decimal(rows[index + 1]["first_price"])
        exit_price = Decimal(rows[index + candidate.horizon]["last_price"])
        gross = Decimal(direction) * (exit_price / entry - Decimal(1))
        net = gross - cost
        equity *= Decimal(1) + net
        trades += 1
        wins += int(net > 0)
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, (peak - equity) / peak)
        index += candidate.horizon
    return {
        "candidate": asdict(candidate),
        "net_return_fraction": format(equity - Decimal(1), "f"),
        "trade_count": trades,
        "win_count": wins,
        "maximum_drawdown_fraction": format(maximum_drawdown, "f"),
    }


def screen_flow_volatility(
    path: Path, market: str, *, minimum_buckets: int = 300,
    minimum_trades: int = 3, round_trip_cost_bps: str = "26",
    maximum_drawdown_fraction: str = "0.20",
) -> dict:
    if type(minimum_buckets) is not int or minimum_buckets < 300:
        raise ValueError("minimum_buckets must be at least 300")
    if type(minimum_trades) is not int or not 1 <= minimum_trades <= 10000:
        raise ValueError("minimum_trades is invalid")
    try:
        cost = Decimal(round_trip_cost_bps) / Decimal(10000)
        drawdown_limit = Decimal(maximum_drawdown_fraction)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid screen economics") from None
    if (not cost.is_finite() or not drawdown_limit.is_finite()
            or not 0 <= cost < 1 or not 0 <= drawdown_limit <= 1):
        raise ValueError("invalid screen economics")
    rows = _read_rows(path, market)
    base = {
        "schema": SCHEMA,
        "market": _symbol(market),
        "candidate_registry_version": CANDIDATE_REGISTRY_VERSION,
        "available_causal_buckets": len(rows),
        "minimum_buckets": minimum_buckets,
        "round_trip_cost_bps": format(Decimal(round_trip_cost_bps), "f"),
        "network_orders": False,
        "trading_authority": False,
        "automatic_promotion": False,
        "paid_model_calls": 0,
    }
    if len(rows) < minimum_buckets:
        result = {
            **base,
            "status": "WAITING_FOR_CAUSAL_BUCKETS",
            "prospective_qualification": False,
            "final_consumed": False,
        }
        result["result_sha256"] = sha256(json.dumps(
            result, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        return result
    rows = rows[-minimum_buckets:]
    train_end = int(len(rows) * 0.6)
    development_end = int(len(rows) * 0.8)
    partitions = {
        "TRAIN": rows[:train_end],
        "DEVELOPMENT": rows[train_end:development_end],
        "FINAL": rows[development_end:],
    }
    training = [_simulate(partitions["TRAIN"], item, cost) for item in candidates()]
    eligible = [
        row for row in training
        if row["trade_count"] >= minimum_trades
        and Decimal(row["maximum_drawdown_fraction"]) <= drawdown_limit
        and Decimal(row["net_return_fraction"]) > max(
            Decimal(0), _hold(partitions["TRAIN"], cost),
        )
    ]
    selected = None if not eligible else max(
        eligible, key=lambda row: Decimal(row["net_return_fraction"]),
    )
    development = final = None
    development_passed = False
    if selected is not None:
        candidate = FlowVolatilityCandidate(**selected["candidate"])
        development = _simulate(partitions["DEVELOPMENT"], candidate, cost)
        development_passed = (
            development["trade_count"] >= minimum_trades
            and Decimal(development["maximum_drawdown_fraction"]) <= drawdown_limit
            and Decimal(development["net_return_fraction"]) > max(
                Decimal(0), _hold(partitions["DEVELOPMENT"], cost),
            )
        )
        if development_passed:
            final = _simulate(partitions["FINAL"], candidate, cost)
    qualified = bool(
        final is not None
        and final["trade_count"] >= minimum_trades
        and Decimal(final["maximum_drawdown_fraction"]) <= drawdown_limit
        and Decimal(final["net_return_fraction"]) > max(
            Decimal(0), _hold(partitions["FINAL"], cost),
        )
    )
    result = {
        **base,
        "status": "QUALIFIED" if qualified else "REJECTED",
        "partition_counts": {key: len(value) for key, value in partitions.items()},
        "candidate_count": len(training),
        "selected_train": selected,
        "development": development,
        "development_passed": development_passed,
        "final": final,
        "final_consumed": final is not None,
        "prospective_qualification": qualified,
        "limitations": [
            "SPOT_PRICE_SHORT_PROXY",
            "FIXED_COST_MODEL",
            "PUBLIC_WEBSOCKET_TRADE_TAPE",
            "NO_LIVE_ORDER_AUTHORITY",
        ],
    }
    result["result_sha256"] = sha256(json.dumps(
        result, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return result
