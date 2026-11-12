"""Causal locked-final cross-market aggressor-flow lead/lag screen."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path

from .trade_flow_screen import _read_rows
from ..integrations.whitebit_public import _symbol


SCHEMA = "autotrade-cross-market-trade-flow-v1"
CANDIDATE_REGISTRY_VERSION = "CROSS_MARKET_FLOW_LEAD_LAG_V1"
FAMILIES = ("LEADER_MOMENTUM", "LEADER_CONTRARIAN")


@dataclass(frozen=True)
class LeadLagCandidate:
    family: str
    lookback: int
    threshold: str
    horizon: int


def candidates() -> tuple[LeadLagCandidate, ...]:
    return tuple(
        LeadLagCandidate(family, lookback, threshold, horizon)
        for family in FAMILIES
        for lookback in (1, 3, 12)
        for threshold in ("0.2", "0.5", "0.8")
        for horizon in (1, 3, 12)
    )


def _aligned_rows(leader_rows: list[dict], target_rows: list[dict]) -> list[dict]:
    """Return only the latest strictly contiguous common five-second segment."""
    leader = {int(row["bucket_epoch"]): row for row in leader_rows}
    target = {int(row["bucket_epoch"]): row for row in target_rows}
    epochs = sorted(set(leader).intersection(target))
    segment: list[dict] = []
    for epoch in epochs:
        leader_seconds = int(leader[epoch]["bucket_seconds"])
        target_seconds = int(target[epoch]["bucket_seconds"])
        if leader_seconds != 5 or target_seconds != 5:
            raise ValueError("lead/lag screen requires five-second buckets")
        if segment and epoch != int(segment[-1]["bucket_epoch"]) + 5:
            segment = []
        segment.append({
            "bucket_epoch": epoch,
            "leader_buy_quote": leader[epoch]["aggressive_buy_quote_quantity"],
            "leader_sell_quote": leader[epoch]["aggressive_sell_quote_quantity"],
            "target_first_price": target[epoch]["first_price"],
            "target_last_price": target[epoch]["last_price"],
        })
    return segment


def _simulate(rows: list[dict], candidate: LeadLagCandidate, cost: Decimal) -> dict:
    if candidate.family not in FAMILIES:
        raise ValueError("unknown cross-market flow family")
    equity = Decimal(1)
    peak = equity
    maximum_drawdown = Decimal(0)
    trades = wins = 0
    index = candidate.lookback - 1
    threshold = Decimal(candidate.threshold)
    while index + candidate.horizon < len(rows):
        window = rows[index - candidate.lookback + 1:index + 1]
        buy = sum((Decimal(row["leader_buy_quote"]) for row in window), Decimal(0))
        sell = sum((Decimal(row["leader_sell_quote"]) for row in window), Decimal(0))
        total = buy + sell
        imbalance = Decimal(0) if total == 0 else (buy - sell) / total
        direction = 1 if imbalance >= threshold else -1 if imbalance <= -threshold else 0
        if candidate.family == "LEADER_CONTRARIAN":
            direction = -direction
        if direction == 0:
            index += 1
            continue
        # The leader bucket at index is complete before target execution begins.
        entry = Decimal(rows[index + 1]["target_first_price"])
        exit_price = Decimal(rows[index + candidate.horizon]["target_last_price"])
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


def _hold(rows: list[dict], cost: Decimal) -> Decimal:
    if len(rows) < 2:
        return Decimal(0)
    return (Decimal(rows[-1]["target_last_price"])
            / Decimal(rows[0]["target_first_price"]) - Decimal(1) - cost)


def _passes(row: dict, rows: list[dict], *, minimum_trades: int,
            drawdown_limit: Decimal, cost: Decimal) -> bool:
    return (
        row["trade_count"] >= minimum_trades
        and Decimal(row["maximum_drawdown_fraction"]) <= drawdown_limit
        and Decimal(row["net_return_fraction"]) > max(Decimal(0), _hold(rows, cost))
    )


def screen_lead_lag(
    path: Path, leader_market: str, target_market: str, *,
    minimum_buckets: int = 300, minimum_trades: int = 6,
    round_trip_cost_bps: str = "26", maximum_drawdown_fraction: str = "0.20",
) -> dict:
    leader_market = _symbol(leader_market)
    target_market = _symbol(target_market)
    if leader_market == target_market:
        raise ValueError("leader and target markets must differ")
    if type(minimum_buckets) is not int or minimum_buckets < 180:
        raise ValueError("minimum_buckets must be at least 180")
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

    aligned = _aligned_rows(_read_rows(path, leader_market),
                            _read_rows(path, target_market))
    base = {
        "schema": SCHEMA,
        "candidate_registry_version": CANDIDATE_REGISTRY_VERSION,
        "leader_market": leader_market,
        "target_market": target_market,
        "available_aligned_buckets": len(aligned),
        "minimum_buckets": minimum_buckets,
        "round_trip_cost_bps": format(Decimal(round_trip_cost_bps), "f"),
        "evidence_class": "EARLY_HYPOTHESIS_ONLY",
        "network_orders": False,
        "trading_authority": False,
        "automatic_promotion": False,
        "paid_model_calls": 0,
    }
    if len(aligned) < minimum_buckets:
        result = {**base, "status": "WAITING_FOR_ALIGNED_BUCKETS",
                  "prospective_qualification": False, "final_consumed": False}
        result["result_sha256"] = sha256(json.dumps(
            result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return result

    rows = aligned[-minimum_buckets:]
    train_end = int(len(rows) * 0.6)
    development_end = int(len(rows) * 0.8)
    partitions = {
        "TRAIN": rows[:train_end],
        "DEVELOPMENT": rows[train_end:development_end],
        "FINAL": rows[development_end:],
    }
    training = [_simulate(partitions["TRAIN"], item, cost) for item in candidates()]
    eligible = [row for row in training if _passes(
        row, partitions["TRAIN"], minimum_trades=minimum_trades,
        drawdown_limit=drawdown_limit, cost=cost)]
    selected = None if not eligible else max(
        eligible, key=lambda row: Decimal(row["net_return_fraction"]))
    development = final = None
    development_passed = False
    if selected is not None:
        candidate = LeadLagCandidate(**selected["candidate"])
        development = _simulate(partitions["DEVELOPMENT"], candidate, cost)
        development_passed = _passes(
            development, partitions["DEVELOPMENT"], minimum_trades=minimum_trades,
            drawdown_limit=drawdown_limit, cost=cost)
        if development_passed:
            final = _simulate(partitions["FINAL"], candidate, cost)
    qualified = bool(final is not None and _passes(
        final, partitions["FINAL"], minimum_trades=minimum_trades,
        drawdown_limit=drawdown_limit, cost=cost))
    result = {
        **base,
        "status": "QUALIFIED_HYPOTHESIS" if qualified else "REJECTED",
        "partition_counts": {key: len(value) for key, value in partitions.items()},
        "candidate_count": len(training),
        "selected_train": selected,
        "development": development,
        "development_passed": development_passed,
        "final": final,
        "final_consumed": final is not None,
        "prospective_qualification": qualified,
        "limitations": [
            "SPOT_PRICE_SHORT_PROXY", "FIXED_COST_MODEL",
            "SHORT_ALIGNED_SAMPLE", "EARLY_HYPOTHESIS_ONLY",
            "NO_LIVE_ORDER_AUTHORITY",
        ],
    }
    result["result_sha256"] = sha256(json.dumps(
        result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result
