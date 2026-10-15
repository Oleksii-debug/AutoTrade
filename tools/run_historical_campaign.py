"""One recoverable train/validate/two-account historical ZERO research cut.

The training and validation commands are read-only with respect to financial
state. Both holdouts use the existing canonical simulation and separate durable
accounts. This is a finite research cut, not an online model promotion loop.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path

from tools.historical_public_klines import _reject_symlink_ancestors
from tools.run_historical_zero import main as run_holdout
from tools.train_historical_agent import main as train, save_model
from tools.validate_historical_agent import main as validate


def _month(value: str) -> tuple[int, int]:
    if (len(value) != 7 or value[4] != "-" or
        not value[:4].isdigit() or not value[5:].isdigit()):
        raise ValueError("period must have exact YYYY-MM form")
    year, month = int(value[:4]), int(value[5:])
    if not 2024 <= year <= 2100 or not 1 <= month <= 12:
        raise ValueError("period is outside supported historical calendar")
    return year, month


def _run(command, argv: list[str], label: str) -> dict:
    output = StringIO()
    with redirect_stdout(output):
        code = command(argv)
    if code != 0:
        raise ValueError(f"{label} blocked: {output.getvalue().strip()}")
    try:
        result = json.loads(output.getvalue())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} did not emit one structured report") from exc
    if type(result) is not dict:
        raise ValueError(f"{label} report is malformed")
    return result


def campaign(*, data_root: Path, workspace: Path, symbol: str,
             train_from: str, train_through: str,
             validate_from: str, validate_through: str,
             holdout_from: str, holdout_through: str,
             episode_limit: int | None = None) -> dict:
    if symbol not in {"BTCUSDT", "ETHUSDT"}:
        raise ValueError("research campaign instrument is unsupported")
    periods = tuple(_month(value) for value in (
        train_from, train_through, validate_from, validate_through,
        holdout_from, holdout_through,
    ))
    if not (periods[0] <= periods[1] < periods[2] <= periods[3] <
            periods[4] <= periods[5]):
        raise ValueError("train, validation and holdout periods must be ordered and disjoint")
    if episode_limit is not None and (type(episode_limit) is not int or episode_limit < 1):
        raise ValueError("episode limit must be a positive integer")
    _reject_symlink_ancestors(workspace)
    model_path = workspace / "models" / f"{symbol}-{train_from}-{train_through}.json"
    validation_path = workspace / "validation" / (
        f"{symbol}-{validate_from}-{validate_through}-model-{train_through}.json"
    )
    common = ["--data-root", str(data_root), "--symbol", symbol, "--interval", "1h"]
    model = _run(train, [*common, "--from-month", train_from,
                         "--through-month", train_through,
                         "--model-out", str(model_path)], "training")
    validation = _run(validate, [*common, "--from-month", validate_from,
                                    "--through-month", validate_through,
                                    "--model-path", str(model_path),
                                    "--report-out", str(validation_path)], "validation")
    accounts = {}
    for budget in ("200", "1000"):
        args = [*common, "--from-month", holdout_from,
                "--through-month", holdout_through, "--next-bar-open",
                "--model-path", str(model_path),
                "--validation-report-path", str(validation_path),
                "--virtual-cash", budget,
                "--state-dir", str(workspace / "accounts" / symbol /
                                   f"{holdout_from}-{holdout_through}" / f"usd-{budget}"),
                "--run-id", f"{symbol}-{holdout_from}-{holdout_through}-usd-{budget}"]
        if episode_limit is not None:
            args += ["--stop-after-episodes", str(episode_limit)]
        result = _run(run_holdout, args, f"USD {budget} holdout")
        simulation, benchmark = result["simulation"], result["comparative_report"]
        if (result["trained_model_sha256"] != model["model_sha256"] or
            result["validation_report_sha256"] != validation["report_sha256"] or
            benchmark is None):
            raise ValueError("holdout did not bind the selected model and validation")
        accounts[budget] = {
            "status": simulation["status"],
            "completed_episodes": simulation["completed_episodes"],
            "cash_usd": simulation["cash"],
            "position": simulation["position"],
            "agent_mark_pnl_usd": benchmark["agent_mark_pnl_usd"],
            "agent_max_mark_drawdown_fraction": benchmark["agent_max_mark_drawdown_fraction"],
            "cash_baseline_equity_usd": benchmark["cash_baseline_equity_usd"],
            "buy_hold_liquidation_scenario_pnl_usd": benchmark[
                "buy_hold_liquidation_scenario_pnl_usd"],
            "dataset_identity_sha256": result["dataset_identity_sha256"],
        }
    report = {
        "status": "INTERNAL_TWO_ACCOUNT_HISTORICAL_CUT_NOT_PRODUCT_QUALIFICATION",
        "symbol": symbol, "interval": "1h",
        "periods": {"training": [train_from, train_through],
                    "validation": [validate_from, validate_through],
                    "holdout": [holdout_from, holdout_through]},
        "model_sha256": model["model_sha256"],
        "selected_strategy_parameters": model["strategy_parameters"],
        "matured_training_labels": model["matured_labels"],
        "validation_report_sha256": validation["report_sha256"],
        "accounts": accounts,
    }
    # Each completed prefix gets an immutable, human-readable local checkpoint.
    # If the second account fails, its retry still uses the first one's journal.
    episode_suffix = "full" if episode_limit is None else str(episode_limit)
    save_model(workspace / "reports" /
               f"{symbol}-{holdout_from}-{holdout_through}-{episode_suffix}.json", report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run a disjoint historical training, validation and two-account cut")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--symbol", choices=("BTCUSDT", "ETHUSDT"), required=True)
    for name in ("train-from", "train-through", "validate-from", "validate-through",
                 "holdout-from", "holdout-through"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--episode-limit", type=int)
    args = parser.parse_args(argv)
    try:
        result = campaign(data_root=Path(args.data_root), workspace=Path(args.workspace),
                          symbol=args.symbol, train_from=args.train_from,
                          train_through=args.train_through,
                          validate_from=args.validate_from,
                          validate_through=args.validate_through,
                          holdout_from=args.holdout_from,
                          holdout_through=args.holdout_through,
                          episode_limit=args.episode_limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, ArithmeticError, KeyError) as exc:
        print(f"AutoTrade campaign blocked: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
