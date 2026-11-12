"""Run the bounded WhiteBIT flow/volatility locked-final screen."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from autotrade_research.strategies.flow_volatility_screen import (
    screen_flow_volatility,
)


def _atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--market", action="append", required=True)
    parser.add_argument("--minimum-buckets", type=int, default=300)
    parser.add_argument("--minimum-trades", type=int, default=3)
    parser.add_argument("--round-trip-cost-bps", default="26")
    args = parser.parse_args(argv)
    results = {}
    for market in args.market:
        result = screen_flow_volatility(
            args.trade_tape,
            market,
            minimum_buckets=args.minimum_buckets,
            minimum_trades=args.minimum_trades,
            round_trip_cost_bps=args.round_trip_cost_bps,
        )
        results[result["market"]] = result
        _atomic(args.output_dir / f"{result['market'].lower()}-latest.json", result)
    report = {
        "schema": "autotrade-whitebit-flow-volatility-run-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "market_count": len(results),
        "qualified_count": sum(
            row["prospective_qualification"] for row in results.values()
        ),
        "network_orders": False,
        "trading_authority": False,
        "automatic_promotion": False,
        "results": results,
    }
    _atomic(args.output_dir / "run-latest.json", report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
