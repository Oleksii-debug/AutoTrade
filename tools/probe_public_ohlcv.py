"""Inspect public exchange OHLCV via optional CCXT without keys or orders.

Usage:
  python -m pip install -r research/optional-open-source-requirements.txt
  python tools/probe_public_ohlcv.py --exchange binance --symbol BTC/USDT --limit 5
This is research observation, not trading or provider qualification.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research"))

from autotrade_research.integrations.open_source_shadow import (
    PUBLIC_CCXT_VENUES, TIMEFRAMES, public_ohlcv_ccxt,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only public exchange OHLCV probe")
    parser.add_argument("--exchange", choices=sorted(PUBLIC_CCXT_VENUES), required=True)
    parser.add_argument("--symbol", required=True, help="CCXT format, e.g. BTC/USDT")
    parser.add_argument("--timeframe", choices=sorted(TIMEFRAMES), default="1h")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)
    candles = public_ohlcv_ccxt(args.exchange, args.symbol, args.timeframe, args.limit)
    print(json.dumps({
        "environment": "PUBLIC_MARKET_DATA_ONLY",
        "execution_authority": "NONE",
        "source": "ccxt:" + args.exchange,
        "symbol": args.symbol,
        "timeframe": args.timeframe,
        "bars": candles,
        "profitability_evidence": "NONE",
    }, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
