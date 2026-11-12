"""Run the bounded WhiteBIT flow triple-barrier locked-final experiment."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from autotrade_research.strategies.flow_triple_barrier import (
    screen_flow_triple_barrier,
)


DEFAULT_MARKETS = (
    "BTC_USDT", "ETH_USDT", "SOL_USDT", "PEPE_USDT", "ENA_USDT",
    "WBT_USDT", "XRP_USDT", "NEAR_USDT", "ZEC_USDT", "UNI_USDT",
    "SUI_USDT", "DOGE_USDT", "TRX_USDT", "TAO_USDT", "HYPE_USDT",
)


def _atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                       dir=str(path.parent))
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
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
    parser.add_argument("--minimum-buckets", type=int, default=300)
    parser.add_argument("--minimum-trades", type=int, default=3)
    parser.add_argument("--round-trip-cost-bps", default="26")
    args = parser.parse_args(argv)
    results: dict[str, dict] = {}
    errors: dict[str, dict] = {}
    for market in args.markets or DEFAULT_MARKETS:
        try:
            result = screen_flow_triple_barrier(
                args.trade_tape, market, minimum_buckets=args.minimum_buckets,
                minimum_trades=args.minimum_trades,
                round_trip_cost_bps=args.round_trip_cost_bps,
            )
            results[result["market"]] = result
            _atomic(args.output_dir / f"{result['market'].lower()}-latest.json", result)
        except Exception as exc:
            errors[market] = {"type": type(exc).__name__, "message": str(exc)}
    report = {
        "schema": "autotrade-whitebit-flow-triple-barrier-run-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "market_count": len(results),
        "qualified_count": sum(
            bool(row["prospective_qualification"]) for row in results.values()
        ),
        "errors": errors,
        "network_orders": False,
        "trading_authority": False,
        "automatic_promotion": False,
        "results": results,
    }
    _atomic(args.output_dir / "run-latest.json", report)
    print(json.dumps({
        "market_count": report["market_count"],
        "qualified_count": report["qualified_count"],
        "error_count": len(errors),
    }, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
