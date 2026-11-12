"""Causal locked-final triple-barrier screen for WhiteBIT aggressor flow."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path

from .trade_flow_screen import _hold, _read_rows
from ..integrations.whitebit_public import _symbol


SCHEMA = "autotrade-whitebit-flow-triple-barrier-v1"
CANDIDATE_REGISTRY_VERSION = "FLOW_TRIPLE_BARRIER_PREREGISTERED_V1"
FAMILIES = ("FLOW_MOMENTUM", "ABSORPTION_REVERSAL")


@dataclass(frozen=True)
class FlowBarrierCandidate:
    family: str
    lookback: int
    imbalance_threshold: str
    take_profit_bps: str
    stop_loss_bps: str
    maximum_holding_buckets: int


def candidates() -> tuple[FlowBarrierCandidate, ...]:
    return tuple(
        FlowBarrierCandidate(family, lookback, threshold, take, stop, horizon)
        for family in FAMILIES
        for lookback in (1, 12)
        for threshold in ("0.5", "0.8")
        for take in ("40", "80")
        for stop in ("20", "40")
        for horizon in (12, 36, 60)
    )


def _signal(rows: list[dict], index: int, candidate: FlowBarrierCandidate) -> int:
    window = rows[index - candidate.lookback + 1:index + 1]
    buy = sum((Decimal(row["aggressive_buy_quote_quantity"]) for row in window), Decimal(0))
    sell = sum((Decimal(row["aggressive_sell_quote_quantity"]) for row in window), Decimal(0))
    total = buy + sell
    if total <= 0:
        return 0
    imbalance = (buy - sell) / total
    threshold = Decimal(candidate.imbalance_threshold)
    direction = 1 if imbalance >= threshold else -1 if imbalance <= -threshold else 0
    if not direction or candidate.family == "FLOW_MOMENTUM":
        return direction
    if candidate.family != "ABSORPTION_REVERSAL":
        raise ValueError("unknown flow-barrier family")
    first = Decimal(window[0]["first_price"])
    last = Decimal(window[-1]["last_price"])
    response = last / first - Decimal(1)
    # Strong flow that failed to move price in its own direction is the event.
    return -direction if Decimal(direction) * response <= 0 else 0


def _resolve_trade(
    rows: list[dict], entry_index: int, direction: int,
    candidate: FlowBarrierCandidate,
) -> tuple[Decimal, int, str, bool]:
    if direction not in (-1, 1):
        raise ValueError("trade direction must be long or short")
    entry = Decimal(rows[entry_index]["first_price"])
    take = Decimal(candidate.take_profit_bps) / Decimal(10000)
    stop = Decimal(candidate.stop_loss_bps) / Decimal(10000)
    if entry <= 0 or take <= 0 or stop <= 0:
        raise ValueError("invalid barrier economics")
    last_index = min(entry_index + candidate.maximum_holding_buckets - 1, len(rows) - 1)
    for index in range(entry_index, last_index + 1):
        high = Decimal(rows[index]["high_price"])
        low = Decimal(rows[index]["low_price"])
        if direction > 0:
            favorable = high >= entry * (Decimal(1) + take)
            adverse = low <= entry * (Decimal(1) - stop)
            favorable_price = entry * (Decimal(1) + take)
            adverse_price = entry * (Decimal(1) - stop)
        else:
            favorable = low <= entry * (Decimal(1) - take)
            adverse = high >= entry * (Decimal(1) + stop)
            favorable_price = entry * (Decimal(1) - take)
            adverse_price = entry * (Decimal(1) + stop)
        # Intrabucket ordering is unknowable: choose the adverse path when both hit.
        if adverse:
            return adverse_price, index, "STOP_LOSS", favorable
        if favorable:
            return favorable_price, index, "TAKE_PROFIT", False
    return Decimal(rows[last_index]["last_price"]), last_index, "TIME_EXIT", False


def _simulate(rows: list[dict], candidate: FlowBarrierCandidate, cost: Decimal) -> dict:
    if candidate.family not in FAMILIES:
        raise ValueError("unknown flow-barrier family")
    equity = peak = Decimal(1)
    maximum_drawdown = Decimal(0)
    trades = wins = adverse_same_bucket = 0
    outcomes = {"TAKE_PROFIT": 0, "STOP_LOSS": 0, "TIME_EXIT": 0}
    index = candidate.lookback - 1
    while index + 1 < len(rows):
        direction = _signal(rows, index, candidate)
        if not direction:
            index += 1
            continue
        entry_index = index + 1
        entry = Decimal(rows[entry_index]["first_price"])
        exit_price, exit_index, outcome, both_hit = _resolve_trade(
            rows, entry_index, direction, candidate,
        )
        gross = Decimal(direction) * (exit_price / entry - Decimal(1))
        net = gross - cost
        equity *= Decimal(1) + net
        trades += 1
        wins += int(net > 0)
        outcomes[outcome] += 1
        adverse_same_bucket += int(both_hit)
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, (peak - equity) / peak)
        # The exit bucket may become a signal only after it is complete.
        index = max(index + 1, exit_index)
    return {
        "candidate": asdict(candidate),
        "net_return_fraction": format(equity - Decimal(1), "f"),
        "trade_count": trades,
        "win_count": wins,
        "maximum_drawdown_fraction": format(maximum_drawdown, "f"),
        "outcome_counts": outcomes,
        "adverse_same_bucket_count": adverse_same_bucket,
    }


def screen_flow_triple_barrier(
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
            **base, "status": "WAITING_FOR_CAUSAL_BUCKETS",
            "prospective_qualification": False, "final_consumed": False,
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
    train_hurdle = max(Decimal(0), _hold(partitions["TRAIN"], cost))
    eligible = [
        row for row in training
        if row["trade_count"] >= minimum_trades
        and Decimal(row["maximum_drawdown_fraction"]) <= drawdown_limit
        and Decimal(row["net_return_fraction"]) > train_hurdle
    ]
    selected = None if not eligible else max(
        eligible, key=lambda row: Decimal(row["net_return_fraction"]),
    )
    best_train = max(
        training, key=lambda row: Decimal(row["net_return_fraction"]), default=None,
    )
    development = final = None
    development_passed = False
    if selected is not None:
        candidate = FlowBarrierCandidate(**selected["candidate"])
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
        **base, "status": "QUALIFIED" if qualified else "REJECTED",
        "partition_counts": {key: len(value) for key, value in partitions.items()},
        "candidate_count": len(training), "best_train_observed": best_train,
        "selected_train": selected, "development": development,
        "development_passed": development_passed, "final": final,
        "final_consumed": final is not None,
        "prospective_qualification": qualified,
        "conservative_intrabar_order": "ADVERSE_FIRST",
        "limitations": [
            "SPOT_PRICE_SHORT_PROXY", "FIXED_COST_MODEL",
            "PUBLIC_WEBSOCKET_TRADE_TAPE", "FIVE_SECOND_OHLC_PATH_UNKNOWN",
            "NO_LIVE_ORDER_AUTHORITY",
        ],
    }
    result["result_sha256"] = sha256(json.dumps(
        result, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return result
