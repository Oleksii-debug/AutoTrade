#!/usr/bin/env python3
"""Run a bounded cross-market WhiteBIT aggressor-flow lead/lag experiment."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from autotrade_research.strategies.cross_market_trade_flow import screen_lead_lag


DEFAULT_MARKETS = (
    "BTC_USDT", "ETH_USDT", "SOL_USDT", "PEPE_USDT", "ENA_USDT",
    "WBT_USDT", "XRP_USDT", "NEAR_USDT", "ZEC_USDT", "UNI_USDT",
    "SUI_USDT", "DOGE_USDT", "TRX_USDT", "TAO_USDT", "HYPE_USDT",
)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                   dir=str(path.parent))
    temporary = Path(raw)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run(path: Path, markets: list[str], *, minimum_buckets: int,
        minimum_trades: int, round_trip_cost_bps: str) -> dict:
    results = []
    errors = []
    for leader in markets:
        for target in markets:
            if leader == target:
                continue
            try:
                results.append(screen_lead_lag(
                    path, leader, target, minimum_buckets=minimum_buckets,
                    minimum_trades=minimum_trades,
                    round_trip_cost_bps=round_trip_cost_bps,
                ))
            except Exception as exc:
                errors.append({
                    "leader_market": leader, "target_market": target,
                    "error_type": type(exc).__name__, "message": str(exc),
                })
    qualified = [row for row in results if row["prospective_qualification"]]
    return {
        "schema": "autotrade-cross-market-flow-experiment-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "market_count": len(markets),
        "pair_count": len(results),
        "qualified_pair_count": len(qualified),
        "waiting_pair_count": sum(
            row["status"] == "WAITING_FOR_ALIGNED_BUCKETS" for row in results),
        "rejected_pair_count": sum(row["status"] == "REJECTED" for row in results),
        "minimum_buckets": minimum_buckets,
        "minimum_trades": minimum_trades,
        "round_trip_cost_bps": round_trip_cost_bps,
        "results": results,
        "errors": errors,
        "network_orders": False,
        "trading_authority": False,
        "automatic_promotion": False,
        "paid_model_calls": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--market", action="append", dest="markets")
    parser.add_argument("--minimum-buckets", type=int, default=300)
    parser.add_argument("--minimum-trades", type=int, default=6)
    parser.add_argument("--round-trip-cost-bps", default="26")
    args = parser.parse_args(argv)
    markets = args.markets or list(DEFAULT_MARKETS)
    if len(set(markets)) != len(markets) or len(markets) < 2:
        parser.error("at least two unique markets are required")
    report = run(
        args.trade_tape, markets, minimum_buckets=args.minimum_buckets,
        minimum_trades=args.minimum_trades,
        round_trip_cost_bps=args.round_trip_cost_bps,
    )
    _atomic_json(args.output, report)
    print(json.dumps({
        key: report[key] for key in (
            "pair_count", "qualified_pair_count", "waiting_pair_count",
            "rejected_pair_count", "minimum_buckets", "round_trip_cost_bps",
        )
    }, sort_keys=True))
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
