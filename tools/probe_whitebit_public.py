"""Read WhiteBIT public prices without a key; never places an order.

This operator/worker probe is not the final graphical interface. Its inert
status dictionary is intended for the existing Host/Web data projection.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research"))
from autotrade_research.integrations.whitebit_public import WhiteBitPublicClient, PublicDataError


def main(argv=None):
    parser = argparse.ArgumentParser(description="WhiteBIT: лише публічні дані, без реальних заявок")
    parser.add_argument("--market", default="BTC_USDT")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--interval", type=int, default=5)
    args = parser.parse_args(argv)
    if not 1 <= args.samples <= 1000 or not 2 <= args.interval <= 3600:
        parser.error("samples must be 1..1000; interval must be 2..3600 seconds")
    client = WhiteBitPublicClient()
    try:
        for sample in range(args.samples):
            if sample:
                time.sleep(args.interval)
            book = client.orderbook(args.market)
            status = book.accessible_status(now=datetime.now(timezone.utc))
            if args.format == "json":
                print(json.dumps(status, ensure_ascii=False, sort_keys=True))
            else:
                print(f"WhiteBIT. {status['market']}. Дані справжнього ринку. "
                      f"Купівля за найкращою пропозицією: {status['ask']}. "
                      f"Продаж за найкращим попитом: {status['bid']}. "
                      f"Спред: {status['spread']}. Час джерела: {status['source_time']}. "
                      "Виконання лише симуляційне. Реальні заявки вимкнені.")
    except PublicDataError as error:
        print(json.dumps({"status": "UNAVAILABLE", "error_code": str(error),
                          "real_orders_enabled": False}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
