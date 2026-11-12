"""Small causal TRAIN/DEV/FINAL screen for WhiteBIT L2 order-flow imbalance."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from ..integrations.whitebit_public import _symbol


SCHEMA = "autotrade-whitebit-depth-ofi-screen-v1"


@dataclass(frozen=True, slots=True)
class OfiCandidate:
    lookback: int
    horizon: int
    threshold: str = "0.05"
    minimum_persistence: str = "0.60"


def candidates() -> tuple[OfiCandidate, ...]:
    # Four predeclared hypotheses, not a broad parameter search.
    return tuple(OfiCandidate(lookback, horizon)
        for lookback, horizon in ((5, 5), (5, 20), (20, 5), (20, 20)))


def _read_rows(path: Path, market: str, *, maximum_gap_seconds: int = 10) -> list[dict]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("ordinary depth-tape database is required")
    market = _symbol(market)
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        start = connection.execute(
            "SELECT value FROM metadata WHERE key='collection_started_at'").fetchone()
        if start is None:
            raise ValueError("depth tape lacks causal collection boundary")
        start_epoch = int(datetime.fromisoformat(start[0]).timestamp())
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM buckets WHERE market=? AND bucket_epoch>=? ORDER BY bucket_epoch",
            (market, start_epoch))]
    finally:
        connection.close()
    segment: list[dict] = []
    for row in rows:
        if segment:
            gap = int(row["bucket_epoch"]) - int(segment[-1]["bucket_epoch"])
            if gap > maximum_gap_seconds:
                segment = []
            elif gap > 1:
                prior = segment[-1]
                for offset in range(1, gap):
                    segment.append({**prior,
                        "bucket_epoch": int(prior["bucket_epoch"]) + offset,
                        "open_mid": prior["close_mid"], "close_mid": prior["close_mid"],
                        "order_flow_imbalance": "0", "synthetic_quiet_second": True})
        row["synthetic_quiet_second"] = False
        segment.append(row)
    return segment


def _score(row: dict) -> Decimal:
    depth = Decimal(row["bid_depth_top5"]) + Decimal(row["ask_depth_top5"])
    if depth <= 0:
        return Decimal(0)
    return max(Decimal(-1), min(Decimal(1),
        Decimal(row["order_flow_imbalance"]) / depth))


def _simulate(rows: list[dict], candidate: OfiCandidate, cost: Decimal,
              maximum_spread_bps: Decimal) -> dict:
    equity = peak = Decimal(1)
    drawdown = Decimal(0)
    trades = wins = 0
    threshold = Decimal(candidate.threshold)
    persistence_required = Decimal(candidate.minimum_persistence)
    index = candidate.lookback - 1
    while index + candidate.horizon < len(rows):
        window = rows[index - candidate.lookback + 1:index + 1]
        scores = [_score(row) for row in window]
        mean = sum(scores, Decimal(0)) / Decimal(len(scores))
        sign = Decimal(1) if mean > 0 else Decimal(-1)
        persistence = (sum(score * sign >= threshold for score in scores)
                       / Decimal(len(scores)))
        direction = int(sign) if (abs(mean) >= threshold
            and persistence >= persistence_required
            and max(Decimal(row["max_spread_bps"]) for row in window)
                <= maximum_spread_bps) else 0
        if direction == 0:
            index += 1
            continue
        entry = Decimal(rows[index + 1]["open_mid"])
        exit_price = Decimal(rows[index + candidate.horizon]["close_mid"])
        net = Decimal(direction) * (exit_price / entry - Decimal(1)) - cost
        equity *= Decimal(1) + net
        trades += 1; wins += int(net > 0)
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
        index += candidate.horizon
    return {"candidate": asdict(candidate), "trade_count": trades,
        "win_count": wins, "net_return_fraction": format(equity - 1, "f"),
        "maximum_drawdown_fraction": format(drawdown, "f")}


def screen_depth_ofi(path: Path, market: str, *, minimum_buckets: int = 3600,
                     minimum_trades: int = 10, round_trip_cost_bps: str = "15",
                     maximum_spread_bps: str = "8",
                     maximum_drawdown_fraction: str = "0.20") -> dict:
    if type(minimum_buckets) is not int or minimum_buckets < 300:
        raise ValueError("minimum_buckets must be at least 300")
    if type(minimum_trades) is not int or not 1 <= minimum_trades <= 10000:
        raise ValueError("minimum_trades is invalid")
    cost = Decimal(round_trip_cost_bps) / Decimal(10000)
    spread = Decimal(maximum_spread_bps)
    drawdown_limit = Decimal(maximum_drawdown_fraction)
    if not (Decimal(0) <= cost < 1 and Decimal(0) < spread < 1000
            and Decimal(0) <= drawdown_limit <= 1):
        raise ValueError("screen economics are invalid")
    rows = _read_rows(path, market)
    base = {"schema": SCHEMA, "market": _symbol(market),
        "available_causal_buckets": len(rows), "minimum_buckets": minimum_buckets,
        "candidate_count": len(candidates()),
        "round_trip_cost_bps": format(Decimal(round_trip_cost_bps), "f"),
        "network_orders": False, "trading_authority": False,
        "automatic_promotion": False, "paid_model_calls": 0}
    if len(rows) < minimum_buckets:
        result = {**base, "status": "WAITING_FOR_CAUSAL_BUCKETS",
            "prospective_qualification": False, "final_consumed": False}
    else:
        rows = rows[-minimum_buckets:]
        train_end = int(len(rows) * .6); development_end = int(len(rows) * .8)
        train, development, final = (rows[:train_end],
            rows[train_end:development_end], rows[development_end:])
        trials = [_simulate(train, candidate, cost, spread)
                  for candidate in candidates()]
        eligible = [row for row in trials if row["trade_count"] >= minimum_trades
            and Decimal(row["net_return_fraction"]) > 0
            and Decimal(row["maximum_drawdown_fraction"]) <= drawdown_limit]
        selected = max(eligible, key=lambda row: Decimal(row["net_return_fraction"]),
                       default=None)
        dev = final_result = None
        if selected is not None:
            selected_candidate = OfiCandidate(**selected["candidate"])
            dev = _simulate(development, selected_candidate, cost, spread)
            if (dev["trade_count"] >= minimum_trades
                    and Decimal(dev["net_return_fraction"]) > 0
                    and Decimal(dev["maximum_drawdown_fraction"]) <= drawdown_limit):
                final_result = _simulate(final, selected_candidate, cost, spread)
        qualified = bool(final_result is not None
            and final_result["trade_count"] >= minimum_trades
            and Decimal(final_result["net_return_fraction"]) > 0
            and Decimal(final_result["maximum_drawdown_fraction"]) <= drawdown_limit)
        result = {**base, "status": "QUALIFIED" if qualified else "REJECTED",
            "partition_counts": {"TRAIN": len(train), "DEVELOPMENT": len(development),
                "FINAL": len(final)}, "best_train_observed": max(trials,
                key=lambda row: Decimal(row["net_return_fraction"])),
            "selected_train": selected, "development": dev,
            "development_passed": final_result is not None,
            "final": final_result, "final_consumed": final_result is not None,
            "prospective_qualification": qualified,
            "limitations": ["BOUNDED_FOUR_CANDIDATE_REGISTRY",
                "FULL_ROUND_TRIP_COST", "PUBLIC_L2_DEPTH_TAPE",
                "NO_LIVE_ORDER_AUTHORITY"]}
    result["result_sha256"] = sha256(json.dumps(result, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return result

