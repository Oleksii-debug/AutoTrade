"""Validate an immutable trained model on later historical closed bars."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp.adaptive_ma import validate_closed_candles
from tools.historical_causal_replay import closed_candle_simulation_input
from tools.historical_public_klines import HistoricalArchiveError
from tools.train_historical_agent import read_model, save_model


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validate an AutoTrade model without trading or updates")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--from-month", required=True)
    parser.add_argument("--through-month", required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--report-out", required=True)
    args = parser.parse_args(argv)
    try:
        root = Path(args.data_root)
        dates, _opens, closes, source_digest = closed_candle_simulation_input(
            root, symbol=args.symbol, interval=args.interval,
            from_month=args.from_month, through_month=args.through_month,
            artifact_store=ArtifactStore(root / ".artifact-store"),
        )
        report = validate_closed_candles(
            read_model(Path(args.model_path)), dates=dates, prices=closes,
            dataset_sha256=source_digest, symbol=args.symbol, interval=args.interval,
        )
        save_model(Path(args.report_out), report)
        print(json.dumps({**report, "report_path": str(args.report_out)}, ensure_ascii=False))
        return 0
    except (HistoricalArchiveError, OSError, ValueError, TypeError, ArithmeticError) as exc:
        print(f"AutoTrade validation blocked: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
