"""Train and persist one causal moving-average policy for later ZERO holdout.

This training command never sends an order or opens a financial account.
Source archive validation and immutable feeder identity are shared with the
existing historical runner; only a later independent period can evaluate it.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp.adaptive_ma import train_closed_candles, verify_model
from research.autotrade_research.io.strict_json import strict_json_loads
from tools.historical_causal_replay import closed_candle_simulation_input
from tools.historical_public_klines import HistoricalArchiveError, _reject_symlink_ancestors


def read_model(path: Path) -> dict:
    _reject_symlink_ancestors(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 20000:
        raise ValueError("trained model is missing, redirected or unbounded")
    data = strict_json_loads(path.read_text(encoding="utf-8"))
    if type(data) is not dict:
        raise ValueError("trained model must be a JSON object")
    return data


def save_model(path: Path, model: dict) -> None:
    _reject_symlink_ancestors(path)
    data = (json.dumps(model, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.exists():
        if path.is_symlink() or path.read_bytes() != data:
            raise ValueError("existing model differs; choose a new versioned output path")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # Publish only if this versioned name is still free.  os.replace would
        # overwrite another process's completed model after both observed an
        # absent path, silently changing the model used by a resumed campaign.
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != data:
                raise ValueError("existing model differs; choose a new versioned output path")
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Train a causal AutoTrade model from verified closed candles")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--from-month", default="2024-01")
    parser.add_argument("--through-month", required=True)
    parser.add_argument("--model-out", required=True)
    args = parser.parse_args(argv)
    try:
        root = Path(args.data_root)
        dates, _opens, closes, dataset_digest = closed_candle_simulation_input(
            root, symbol=args.symbol, interval=args.interval,
            from_month=args.from_month, through_month=args.through_month,
            artifact_store=ArtifactStore(root / ".artifact-store"),
        )
        model = train_closed_candles(
            dates=dates, prices=closes, dataset_sha256=dataset_digest,
            symbol=args.symbol, interval=args.interval,
        )
        from datetime import timedelta
        from tools.historical_public_klines import INTERVAL_SECONDS
        verify_model(model, symbol=args.symbol, interval=args.interval,
                     holdout_start_open=dates[-1] + timedelta(seconds=INTERVAL_SECONDS[args.interval]))
        save_model(Path(args.model_out), model)
        print(json.dumps({
            "status": "TRAINED_CANDIDATE_NOT_PROFITABILITY_EVIDENCE",
            "model_path": str(args.model_out),
            "model_sha256": model["model_sha256"],
            "strategy_parameters": model["selected_parameters"],
            "training_last_close_utc": model["training_last_close_utc"],
            "matured_labels": [row["matured_labels"] for row in model["candidates"]],
        }, ensure_ascii=False))
        return 0
    except (HistoricalArchiveError, OSError, ValueError, TypeError, ArithmeticError) as exc:
        print(f"AutoTrade training blocked: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
