"""Train and persist one causal moving-average policy for later ZERO holdout.

This training command never sends an order or opens a financial account.
Source archive validation and immutable feeder identity are shared with the
existing historical runner. Learning consumes only completed, reconciled ZERO
position cycles supplied by the existing simulator; later candle direction is
never used as a substitute outcome label.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp.adaptive_ma import verify_model
from research.autotrade_research.io.strict_json import strict_json_loads
from tools.historical_causal_replay import closed_candle_simulation_input
from tools.historical_learning_adapter import train_closed_simulation_outcomes
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
    parser.add_argument(
        "--simulation-results", required=True,
        help=("bounded JSON object containing candidate_simulations produced by "
              "completed canonical ZERO runs"),
    )
    parser.add_argument("--memory-path", default=None,
                        help="append-only ExperienceMemory trail; defaults beside model")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--embargo-seconds", type=int, default=3600)
    args = parser.parse_args(argv)
    try:
        root = Path(args.data_root)
        dates, _opens, closes, dataset_digest = closed_candle_simulation_input(
            root, symbol=args.symbol, interval=args.interval,
            from_month=args.from_month, through_month=args.through_month,
            artifact_store=ArtifactStore(root / ".artifact-store"),
        )
        results_path = Path(args.simulation_results)
        _reject_symlink_ancestors(results_path)
        if (results_path.is_symlink() or not results_path.is_file()
                or results_path.stat().st_size > 5_000_000):
            raise ValueError("simulation result bundle is missing, redirected or unbounded")
        bundle = strict_json_loads(results_path.read_text(encoding="utf-8"))
        if type(bundle) is not dict or set(bundle) != {"candidate_simulations"}:
            raise ValueError("simulation result bundle schema differs")
        model_out = Path(args.model_out)
        memory_path = (Path(args.memory_path) if args.memory_path is not None
                       else model_out.with_suffix(model_out.suffix + ".memory.sqlite3"))
        model = train_closed_simulation_outcomes(
            candidate_simulations=bundle["candidate_simulations"],
            memory_path=memory_path,
            dataset_sha256=dataset_digest,
            symbol=args.symbol,
            interval=args.interval,
            first_execution_at=dates[1],
            seed=args.seed,
            embargo_seconds=args.embargo_seconds,
            expected_execution_count=len(dates) - 1,
        )
        from datetime import datetime, timedelta
        last_close = datetime.fromisoformat(model["training_last_close_utc"])
        verify_model(model, symbol=args.symbol, interval=args.interval,
                     holdout_start_open=last_close + timedelta(seconds=args.embargo_seconds))
        save_model(model_out, model)
        print(json.dumps({
            "status": "TRAINED_CANDIDATE_NOT_PROFITABILITY_EVIDENCE",
            "model_path": str(args.model_out),
            "model_sha256": model["model_sha256"],
            "model_version": model["model_registry"]["version"],
            "strategy_parameters": model["selected_parameters"],
            "training_last_close_utc": model["training_last_close_utc"],
            "closed_outcomes": [row["closed_outcomes"] for row in model["candidates"]],
            "learning_population_root": model["learning_audit"]["population_root"],
            "rng_seed": model["rng_seed"],
        }, ensure_ascii=False))
        return 0
    except (HistoricalArchiveError, OSError, ValueError, TypeError, ArithmeticError) as exc:
        print(f"AutoTrade training blocked: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
