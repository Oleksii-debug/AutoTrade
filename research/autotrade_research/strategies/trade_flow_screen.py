"""Causal locked-final screen for WhiteBIT five-second aggressor flow."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from ..integrations.whitebit_public import _symbol


SCHEMA = "autotrade-whitebit-trade-flow-screen-v2"
CANDIDATE_REGISTRY_VERSION = "FLOW_PRICE_RESPONSE_V2"
FAMILIES = (
    "MOMENTUM",
    "CONTRARIAN",
    "CONFIRMED_MOMENTUM",
    "ABSORPTION_REVERSAL",
)


@dataclass(frozen=True)
class FlowCandidate:
    family: str
    lookback: int
    threshold: str
    horizon: int


def candidates() -> tuple[FlowCandidate, ...]:
    return tuple(
        FlowCandidate(family, lookback, threshold, horizon)
        for family in FAMILIES
        for lookback in (1, 3, 12)
        for threshold in ("0.2", "0.5", "0.8")
        for horizon in (1, 3, 12)
    )


def _read_rows(path: Path, market: str, *, maximum_gap_buckets: int = 12) -> list[dict]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("ordinary trade-tape database is required")
    market = _symbol(market)
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        started = connection.execute(
            "SELECT value FROM metadata WHERE key='collection_started_at'"
        ).fetchone()
        if started is None:
            raise ValueError("trade tape lacks causal collection boundary")
        start_epoch = int(datetime.fromisoformat(started[0]).timestamp())
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM buckets WHERE market=? AND bucket_epoch>=? "
            "ORDER BY bucket_epoch", (market, start_epoch)
        )]
    finally:
        connection.close()
    if not rows:
        return []
    seconds = int(rows[0]["bucket_seconds"])
    segment: list[dict] = []
    for row in rows:
        if int(row["bucket_seconds"]) != seconds:
            raise ValueError("mixed trade-flow bucket duration")
        if segment:
            gap = (int(row["bucket_epoch"]) - int(segment[-1]["bucket_epoch"])) // seconds
            if gap > maximum_gap_buckets:
                segment = []
            elif gap > 1:
                prior = segment[-1]
                for offset in range(1, gap):
                    segment.append({
                        **prior,
                        "bucket_epoch": int(prior["bucket_epoch"]) + seconds * offset,
                        "first_price": prior["last_price"],
                        "last_price": prior["last_price"],
                        "high_price": prior["last_price"],
                        "low_price": prior["last_price"],
                        "aggressive_buy_count": 0,
                        "aggressive_sell_count": 0,
                        "aggressive_buy_base_quantity": "0",
                        "aggressive_sell_base_quantity": "0",
                        "aggressive_buy_quote_quantity": "0",
                        "aggressive_sell_quote_quantity": "0",
                        "rpi_count": 0,
                        "synthetic_no_trade_bucket": True,
                    })
        row["synthetic_no_trade_bucket"] = False
        segment.append(row)
    return segment


def _simulate(rows: list[dict], candidate: FlowCandidate, cost: Decimal) -> dict:
    if candidate.family not in FAMILIES:
        raise ValueError("unknown trade-flow family")
    equity = Decimal("1")
    peak = equity
    maximum_drawdown = Decimal(0)
    trades = wins = 0
    index = candidate.lookback - 1
    threshold = Decimal(candidate.threshold)
    while index + candidate.horizon < len(rows):
        window = rows[index - candidate.lookback + 1:index + 1]
        buy = sum((Decimal(row["aggressive_buy_quote_quantity"]) for row in window), Decimal(0))
        sell = sum((Decimal(row["aggressive_sell_quote_quantity"]) for row in window), Decimal(0))
        total = buy + sell
        imbalance = Decimal(0) if total == 0 else (buy - sell) / total
        direction = 1 if imbalance >= threshold else -1 if imbalance <= -threshold else 0
        if candidate.family == "CONTRARIAN":
            direction = -direction
        elif direction and candidate.family in (
            "CONFIRMED_MOMENTUM", "ABSORPTION_REVERSAL",
        ):
            first = Decimal(window[0]["first_price"])
            response = Decimal(window[-1]["last_price"]) / first - Decimal(1)
            aligned = Decimal(direction) * response > 0
            if candidate.family == "CONFIRMED_MOMENTUM":
                direction = direction if aligned else 0
            else:
                direction = 0 if aligned else -direction
        if direction == 0:
            index += 1
            continue
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


def _hold(rows: list[dict], cost: Decimal) -> Decimal:
    if len(rows) < 2:
        return Decimal(0)
    return Decimal(rows[-1]["last_price"]) / Decimal(rows[0]["first_price"]) - 1 - cost


def screen_trade_flow(
    path: Path, market: str, *, minimum_buckets: int = 2160,
    minimum_trades: int = 10, round_trip_cost_bps: str = "26",
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
        "schema": SCHEMA, "market": _symbol(market),
        "candidate_registry_version": CANDIDATE_REGISTRY_VERSION,
        "bucket_count": len(rows), "available_causal_buckets": len(rows),
        "minimum_buckets": minimum_buckets,
        "round_trip_cost_bps": format(Decimal(round_trip_cost_bps), "f"),
        "network_orders": False, "trading_authority": False,
        "paid_model_calls": 0,
    }
    if len(rows) < minimum_buckets:
        result = {**base, "status": "WAITING_FOR_CAUSAL_BUCKETS",
                  "prospective_qualification": False, "final_consumed": False}
        result["result_sha256"] = sha256(json.dumps(result, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
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
    training_hold = _hold(partitions["TRAIN"], cost)
    training_benchmark = max(Decimal(0), training_hold)
    best_train = max(
        training, key=lambda row: Decimal(row["net_return_fraction"]),
        default=None,
    )
    eligible = [row for row in training
                if row["trade_count"] >= minimum_trades
                and Decimal(row["maximum_drawdown_fraction"]) <= drawdown_limit
                and Decimal(row["net_return_fraction"]) > max(
                    Decimal(0), training_hold)]
    selected = None if not eligible else max(
        eligible, key=lambda row: Decimal(row["net_return_fraction"]))
    development = final = None
    development_passed = False
    if selected is not None:
        candidate = FlowCandidate(**selected["candidate"])
        development = _simulate(partitions["DEVELOPMENT"], candidate, cost)
        development_passed = (
            development["trade_count"] >= minimum_trades
            and Decimal(development["maximum_drawdown_fraction"]) <= drawdown_limit
            and Decimal(development["net_return_fraction"]) > max(
                Decimal(0), _hold(partitions["DEVELOPMENT"], cost))
        )
        if development_passed:
            final = _simulate(partitions["FINAL"], candidate, cost)
    qualified = bool(final is not None
        and final["trade_count"] >= minimum_trades
        and Decimal(final["maximum_drawdown_fraction"]) <= drawdown_limit
        and Decimal(final["net_return_fraction"]) > max(
            Decimal(0), _hold(partitions["FINAL"], cost)))
    train_failure_reasons: list[str] = []
    if selected is None and best_train is not None:
        if best_train["trade_count"] < minimum_trades:
            train_failure_reasons.append("INSUFFICIENT_TRADES")
        if Decimal(best_train["maximum_drawdown_fraction"]) > drawdown_limit:
            train_failure_reasons.append("MAXIMUM_DRAWDOWN_EXCEEDED")
        if Decimal(best_train["net_return_fraction"]) <= 0:
            train_failure_reasons.append("NOT_POSITIVE_AFTER_COSTS")
        if Decimal(best_train["net_return_fraction"]) <= training_benchmark:
            train_failure_reasons.append("DID_NOT_BEAT_CASH_OR_HOLD")
    result = {
        **base, "status": "QUALIFIED" if qualified else "REJECTED",
        "partition_counts": {key: len(value) for key, value in partitions.items()},
        "candidate_count": len(training), "selected_train": selected,
        "best_train_observed": best_train,
        "train_hold_return_fraction": format(training_hold, "f"),
        "train_required_return_fraction": format(training_benchmark, "f"),
        "train_failure_reasons": train_failure_reasons,
        "development": development, "development_passed": development_passed,
        "final": final, "final_consumed": final is not None,
        "prospective_qualification": qualified,
        "automatic_promotion": False,
        "limitations": ["SPOT_PRICE_SHORT_PROXY", "FIXED_COST_MODEL",
                        "PRICE_RESPONSE_USES_CURRENT_COMPLETED_BUCKET_ONLY",
                        "PUBLIC_WEBSOCKET_TRADE_TAPE", "NO_LIVE_ORDER_AUTHORITY"],
    }
    result["result_sha256"] = sha256(json.dumps(result, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    return result

