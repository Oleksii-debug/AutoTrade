"""One recoverable train/validate/two-account historical ZERO research cut.

The training and validation commands are read-only with respect to financial
state. Both holdouts use the existing canonical simulation and separate durable
accounts. This is a finite research cut, not an online model promotion loop.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from decimal import Decimal, InvalidOperation
from io import StringIO
import json
from pathlib import Path

from tools.historical_public_klines import _reject_symlink_ancestors
from tools.run_historical_zero import main as run_holdout
from tools.train_historical_agent import main as train, save_model
from tools.validate_historical_agent import main as validate
from mvp.autotrade_mvp.adaptive_ma import EXPERTS
from research.autotrade_research.io.strict_json import strict_json_loads


MAX_SIMULATION_BUNDLE_BYTES = 5_000_000


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


def _read_simulation_bundle(path: Path) -> dict:
    """Read the bounded candidate bundle without weakening model validation."""
    _reject_symlink_ancestors(path)
    if (path.is_symlink() or not path.is_file()
            or path.stat().st_size > MAX_SIMULATION_BUNDLE_BYTES):
        raise ValueError("frozen training simulation bundle is missing, redirected or unbounded")
    try:
        bundle = strict_json_loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError, OSError) as exc:
        raise ValueError("frozen training simulation bundle is malformed") from exc
    if type(bundle) is not dict or set(bundle) != {"candidate_simulations"}:
        raise ValueError("frozen training simulation bundle schema differs")
    return bundle


def _has_closed_position_cycle(simulation: dict) -> bool:
    """Return true only after a positive position later becomes exactly flat."""
    if type(simulation) is not dict or type(simulation.get("decisions")) is not list:
        raise ValueError("candidate simulation decisions are malformed")
    opened = False
    for decision in simulation["decisions"]:
        if type(decision) is not dict or type(decision.get("position")) is not str:
            raise ValueError("candidate simulation position is malformed")
        try:
            position = Decimal(decision["position"])
        except InvalidOperation as exc:
            raise ValueError("candidate simulation position is malformed") from exc
        if not position.is_finite() or position < 0:
            raise ValueError("candidate simulation position is malformed")
        if position > 0:
            opened = True
        elif opened:
            return True
    return False


def campaign(*, data_root: Path, workspace: Path, symbol: str,
             train_from: str, train_through: str,
             validate_from: str, validate_through: str,
             holdout_from: str, holdout_through: str,
             episode_limit: int | None = None,
             qualification_cut: bool = False) -> dict:
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
    if type(qualification_cut) is not bool:
        raise TypeError("qualification cut flag must be exact bool")
    if qualification_cut and (episode_limit is None or episode_limit < 12):
        raise ValueError("qualification cut requires at least 12 bounded episodes")
    _reject_symlink_ancestors(workspace)
    model_path = workspace / "models" / f"{symbol}-{train_from}-{train_through}.json"
    validation_path = workspace / "validation" / (
        f"{symbol}-{validate_from}-{validate_through}-model-{train_through}.json"
    )
    common = ["--data-root", str(data_root), "--symbol", symbol, "--interval", "1h"]
    # Produce a bounded canonical ZERO prefix for every frozen DAG expert.  The
    # learning adapter will ignore an open tail and admit only reconciled
    # flat-to-position-to-flat outcomes.  A real campaign can extend the prefix
    # by rerunning these durable states; no next-candle proxy label is used.
    simulation_results_path = workspace / "training" / (
        f"{symbol}-{train_from}-{train_through}-closed-outcomes.json"
    )
    if simulation_results_path.exists():
        _read_simulation_bundle(simulation_results_path)
    else:
        candidate_runs = []

        def run_candidate(fast: int, slow: int, limit: int) -> dict:
            return _run(run_holdout, [
                *common, "--from-month", train_from, "--through-month", train_through,
                "--next-bar-open", "--candidate-fast", str(fast),
                "--candidate-slow", str(slow), "--virtual-cash", "1000",
                "--state-dir", str(workspace / "training-simulations" / symbol /
                                   f"{train_from}-{train_through}" / f"ma-{fast}-{slow}"),
                "--run-id", f"training-{symbol}-{train_from}-{train_through}-ma-{fast}-{slow}",
                "--stop-after-episodes", str(limit),
            ], f"closed-outcome candidate {fast}/{slow}")

        for fast, slow in EXPERTS:
            training_limit = max(12, min(168, episode_limit or 168))
            while True:
                result = run_candidate(fast, slow, training_limit)
                simulation = result["simulation"]
                if _has_closed_position_cycle(simulation) or training_limit == 168:
                    break
                training_limit = min(168, training_limit * 2)
            candidate_runs.append((fast, slow, result))

        # Comparing experts on different prefixes would introduce a hidden
        # selection advantage. Extend every durable simulation to the longest
        # prefix that any expert needed to produce a real closed outcome.
        common_training_cut = max(
            row[2]["simulation"]["completed_episodes"] for row in candidate_runs
        )
        candidate_simulations = []
        for fast, slow, result in candidate_runs:
            if result["simulation"]["completed_episodes"] < common_training_cut:
                result = run_candidate(fast, slow, common_training_cut)
            simulation = result["simulation"]
            candidate_simulations.append({
                "parameters": {"fast": fast, "slow": slow},
                "dataset_identity_sha256": result["dataset_identity_sha256"],
                "execution_count": result["execution_count"]
                                   if simulation["status"] == "COMPLETED"
                                   else simulation["completed_episodes"],
                "simulation": {
                    key: simulation[key] for key in (
                        "status", "mode", "reconciled", "economic_edge_status",
                        "protocol_digest", "completed_episodes", "cash", "position",
                        "decisions",
                    )
                },
            })
        save_model(simulation_results_path, {
            "candidate_simulations": candidate_simulations,
        })
    model = _run(train, [*common, "--from-month", train_from,
                         "--through-month", train_through,
                         "--simulation-results", str(simulation_results_path),
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
        bounded_complete = (
            qualification_cut
            and simulation["status"] == "PAUSED"
            and simulation["completed_episodes"] == episode_limit
        )
        accounts[budget] = {
            "status": ("BOUNDED_CAUSAL_PREFIX_COMPLETED_NOT_FULL_MONTH"
                       if bounded_complete else simulation["status"]),
            "completed_episodes": simulation["completed_episodes"],
            "cash_usd": simulation["cash"],
            "position": simulation["position"],
            "agent_mark_pnl_usd": benchmark["agent_mark_pnl_usd"],
            "agent_max_mark_drawdown_fraction": benchmark["agent_max_mark_drawdown_fraction"],
            "cash_baseline_equity_usd": benchmark["cash_baseline_equity_usd"],
            "buy_hold_liquidation_scenario_pnl_usd": benchmark[
                "buy_hold_liquidation_scenario_pnl_usd"],
            "missed_fill_metrics": benchmark["missed_fill_metrics"],
            "causal_regime_analysis": benchmark["causal_regime_analysis"],
            "conservative_uncertainty": benchmark["conservative_uncertainty"],
            "dataset_identity_sha256": result["dataset_identity_sha256"],
            "historical_risk": result["historical_risk"],
        }
        if qualification_cut:
            accounts[budget]["runtime_status"] = simulation["status"]
    report = {
        "status": "INTERNAL_TWO_ACCOUNT_HISTORICAL_CUT_NOT_PRODUCT_QUALIFICATION",
        "symbol": symbol, "interval": "1h",
        "periods": {"training": [train_from, train_through],
                    "validation": [validate_from, validate_through],
                    "holdout": [holdout_from, holdout_through]},
        "model_sha256": model["model_sha256"],
        "selected_strategy_parameters": model["strategy_parameters"],
        "closed_training_outcomes": model["closed_outcomes"],
        "validation_report_sha256": validation["report_sha256"],
        "accounts": accounts,
    }
    if qualification_cut:
        report["qualification_cut"] = {
            "scope": "BOUNDED_CAUSAL_PREFIX_NOT_FULL_MONTH_PERFORMANCE",
            "episode_limit": episode_limit,
            "all_accounts_reached_limit": all(
                account["status"] ==
                "BOUNDED_CAUSAL_PREFIX_COMPLETED_NOT_FULL_MONTH"
                for account in accounts.values()
            ),
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
    parser.add_argument(
        "--qualification-cut", action="store_true",
        help=("declare an explicitly bounded causal E2E cut; this never claims "
              "completion of the full holdout month"),
    )
    args = parser.parse_args(argv)
    try:
        result = campaign(data_root=Path(args.data_root), workspace=Path(args.workspace),
                          symbol=args.symbol, train_from=args.train_from,
                          train_through=args.train_through,
                          validate_from=args.validate_from,
                          validate_through=args.validate_through,
                          holdout_from=args.holdout_from,
                          holdout_through=args.holdout_through,
                          episode_limit=args.episode_limit,
                          qualification_cut=args.qualification_cut)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, ArithmeticError, KeyError) as exc:
        print(f"AutoTrade campaign blocked: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
