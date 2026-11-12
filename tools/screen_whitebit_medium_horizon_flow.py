"""Run the bounded medium-horizon WhiteBIT flow screen."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from autotrade_research.strategies.medium_horizon_trade_flow import (
    screen_medium_horizon_flow,
)


DEFAULT_MARKETS = (
    "BTC_USDT", "ETH_USDT", "SOL_USDT", "PEPE_USDT", "ENA_USDT",
    "WBT_USDT", "XRP_USDT", "NEAR_USDT", "ZEC_USDT", "UNI_USDT",
    "SUI_USDT", "DOGE_USDT", "TRX_USDT", "TAO_USDT", "HYPE_USDT",
)


def _atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, raw = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent),
    )
    temporary = Path(raw)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--market", action="append", dest="markets")
    parser.add_argument("--minimum-buckets", type=int, default=900)
    parser.add_argument("--maximum-buckets", type=int, default=4320)
    parser.add_argument("--minimum-trades", type=int, default=2)
    parser.add_argument("--round-trip-cost-bps", default="26")
    args = parser.parse_args(argv)

    markets = args.markets or list(DEFAULT_MARKETS)
    results: dict[str, dict] = {}
    errors: dict[str, dict] = {}
    for market in markets:
        try:
            row = screen_medium_horizon_flow(
                args.trade_tape, market,
                minimum_buckets=args.minimum_buckets,
                maximum_buckets=args.maximum_buckets,
                minimum_trades=args.minimum_trades,
                round_trip_cost_bps=args.round_trip_cost_bps,
            )
            results[row["market"]] = row
            _atomic(args.output_dir / f"{row['market'].lower()}-latest.json", row)
        except Exception as exc:
            errors[market] = {
                "error_type": type(exc).__name__, "message": str(exc),
            }

    report = {
        "schema": "autotrade-whitebit-medium-horizon-flow-run-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "market_count": len(results),
        "qualified_count": sum(
            bool(row["prospective_qualification"]) for row in results.values()
        ),
        "waiting_count": sum(
            row["status"] == "WAITING_FOR_CAUSAL_BUCKETS"
            for row in results.values()
        ),
        "rejected_count": sum(
            row["status"] == "REJECTED" for row in results.values()
        ),
        "errors": errors,
        "network_orders": False,
        "trading_authority": False,
        "automatic_promotion": False,
        "paid_model_calls": 0,
        "results": results,
    }
    _atomic(args.output_dir / "run-latest.json", report)
    print(json.dumps({
        key: report[key] for key in (
            "market_count", "qualified_count", "waiting_count",
            "rejected_count", "errors",
        )
    }, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
