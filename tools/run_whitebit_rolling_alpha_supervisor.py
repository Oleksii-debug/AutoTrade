#!/usr/bin/env python3
"""Durably retrain native Alpha models on rolling public WhiteBIT history.

The supervisor is intentionally research-only: it uses public completed candles,
local DLinear/XGBoost/CatBoost implementations and a frozen untouched-holdout
economic gate.  It has no credential input, broker route or promotion authority.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from time import monotonic, sleep

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.integrations.alpha_economics import AlphaInputError
from tools.historical_public_klines import HistoricalArchiveError
from tools.run_whitebit_alpha_economic_gate import evaluate
from tools.run_whitebit_alpha_research import _durable_worker
from tools.run_whitebit_historical_alpha import build_alpha_input
from tools.whitebit_historical_klines import collect_whitebit_history


SCHEMA = "autotrade-whitebit-rolling-native-alpha-v1"
MARKETS = ("BTC_USDT", "ETH_USDT", "SOL_USDT")
INTERVAL_SECONDS = 3600
FUNDING_BORROW_BPS_PER_HOUR = "0.083333333333333333"
CALIBRATION_ALPHAS = ("0.1", "0.2", "0.3", "0.4")


def completed_window(now: datetime, bars: int) -> tuple[int, int]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("rolling Alpha clock must be timezone-aware")
    if type(bars) is not int or not 160 <= bars <= 20_000:
        raise ValueError("rolling Alpha bars must be 160..20000")
    current_hour = int(now.astimezone(UTC).timestamp()) // INTERVAL_SECONDS * INTERVAL_SECONDS
    end = current_hour - INTERVAL_SECONDS
    start = end - (bars - 1) * INTERVAL_SECONDS
    if start < 0:
        raise ValueError("rolling Alpha window predates the epoch")
    return start, end


def compact_evaluation(result: dict) -> dict:
    models = result.get("models")
    if type(models) is not dict:
        raise ValueError("rolling Alpha evaluation has no model reports")
    projected = {}
    for name, report in sorted(models.items()):
        if type(report) is not dict:
            raise ValueError("rolling Alpha model report is invalid")
        projected[name] = {key: report.get(key) for key in (
            "signal_count", "executed_trade_count", "long_trade_count",
            "short_trade_count", "winning_trade_count", "net_pnl_after_costs_quote",
            "net_return_ratio", "maximum_drawdown_ratio", "research_gate_passed",
            "rejection_reasons")}
        selection = report.get("calibration_policy_selection")
        projected[name]["calibration_selected_alpha"] = (
            selection.get("selected_alpha") if type(selection) is dict else None)
        projected[name]["calibration_selection_passed"] = (
            selection.get("selection_passed") if type(selection) is dict else None)
    return {
        "market": result.get("market"),
        "evaluation_id": result.get("evaluation_id"),
        "dataset_identity_sha256": result.get("dataset_identity_sha256"),
        "models": projected,
        "financial_operation_executed": False,
        "live_authority": False,
        "automatic_promotion": False,
    }


def run_cycle(*, state_dir: Path, bars: int, steps: int, now: datetime) -> dict:
    start, end = completed_window(now, bars)
    history_root = state_dir / "history"
    alpha_root = state_dir / "alpha"
    evaluation_root = state_dir / "evaluation"
    reports = {}
    for market in MARKETS:
        manifest = collect_whitebit_history(
            market=market, interval="1h", start=start, end=end,
            output=history_root, now=now.astimezone(UTC),
        )
        manifest_path = (history_root / "spot" / market / "1h" /
                         f"{start}-{end}.manifest.json")
        payload, selection = build_alpha_input(
            manifest_path, bars=bars,
            entry_fee_ratio="0.001", exit_fee_ratio="0.001", spread_bps="4",
            entry_slippage_bps="1", exit_slippage_bps="1",
            funding_borrow_bps=FUNDING_BORROW_BPS_PER_HOUR,
            model_cost_quote="0", notional_quote="1000", min_notional_quote="5",
        )
        worker_state = alpha_root / market.lower()
        worker = _durable_worker(worker_state)
        job, created = worker.enqueue(
            payload, lookback=24, horizon=1, steps=steps, seed=7,
            backends=("DLINEAR", "XGBOOST", "CATBOOST"),
        )
        worker.run(job["job_id"])
        evaluation, reused = evaluate(
            alpha_state_dir=worker_state, job_id=job["job_id"],
            manifest_path=manifest_path,
            evaluation_state_dir=evaluation_root / market.lower(),
            minimum_trades=5, maximum_drawdown_ratio="0.10",
            calibration_alphas=CALIBRATION_ALPHAS,
        )
        reports[market] = {
            "job_id": job["job_id"], "job_created": created,
            "evaluation_reused": reused,
            "selection": selection,
            "manifest_dataset_identity_sha256": manifest["dataset_identity_sha256"],
            "evaluation": compact_evaluation(evaluation),
        }
    return {
        "window": {"start_open_epoch": start, "end_open_epoch": end, "bars": bars},
        "markets": reports,
        "financial_operation_executed": False,
        "network_orders": False,
        "trading_authority": False,
        "automatic_promotion": False,
    }


def _next_utc_day(now: datetime) -> datetime:
    current = now.astimezone(UTC)
    return datetime.combine(current.date() + timedelta(days=1), datetime.min.time(), UTC)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--bars", type=int, default=4096)
    parser.add_argument("--steps", type=int, default=32)
    parser.add_argument("--interval-seconds", type=int, default=21600)
    parser.add_argument("--max-cycles-per-utc-day", type=int, default=4)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not 160 <= args.bars <= 20_000 or not 1 <= args.steps <= 128:
        parser.error("bars or steps outside bounded range")
    if not 3600 <= args.interval_seconds <= 86400:
        parser.error("interval must be 3600..86400 seconds")
    if not 1 <= args.max_cycles_per_utc_day <= 24:
        parser.error("daily cycle budget must be 1..24")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = args.state_dir / "checkpoint.json"
    checkpoint = {}
    if checkpoint_path.is_file():
        if checkpoint_path.is_symlink() or checkpoint_path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("rolling Alpha checkpoint is redirected or unbounded")
        checkpoint = json.loads(checkpoint_path.read_bytes())
        if checkpoint.get("schema") != SCHEMA:
            raise ValueError("rolling Alpha checkpoint schema differs")
    checkpoint.update(
        schema=SCHEMA, status="RUNNING", updated_at=datetime.now(UTC).isoformat(),
        network_orders=False, trading_authority=False, automatic_promotion=False,
    )
    atomic_write_json(checkpoint_path, checkpoint)
    while True:
        now = datetime.now(UTC)
        day = now.date().isoformat()
        cycles_today = int(checkpoint.get("cycles_today", 0)) if checkpoint.get("utc_day") == day else 0
        if args.stop_file and args.stop_file.exists():
            checkpoint.update(schema=SCHEMA, status="STOPPED", updated_at=now.isoformat(),
                              network_orders=False, trading_authority=False)
            atomic_write_json(checkpoint_path, checkpoint)
            return 0
        due_text = checkpoint.get("next_run_at")
        due = datetime.fromisoformat(due_text) if type(due_text) is str else now
        if cycles_today >= args.max_cycles_per_utc_day:
            due = max(due, _next_utc_day(now))
        if now >= due:
            started = monotonic()
            try:
                result = run_cycle(state_dir=args.state_dir, bars=args.bars,
                                   steps=args.steps, now=now)
                status, error = "RUNNING", None
            except (AlphaInputError, HistoricalArchiveError, OSError, ValueError,
                    TypeError, KeyError, RuntimeError, ImportError) as exc:
                result = checkpoint.get("last_result")
                status, error = "RECOVERING", type(exc).__name__
            cycles_today += 1
            checkpoint = {
                "schema": SCHEMA, "status": status, "utc_day": day,
                "cycles_today": cycles_today, "cycle_count": int(checkpoint.get("cycle_count", 0)) + 1,
                "last_result": result, "last_error_class": error,
                "last_cycle_seconds": monotonic() - started,
                "updated_at": datetime.now(UTC).isoformat(),
                "next_run_at": (now + timedelta(seconds=args.interval_seconds)).isoformat(),
                "network_orders": False, "trading_authority": False,
                "automatic_promotion": False,
            }
            atomic_write_json(checkpoint_path, checkpoint)
            if args.once:
                checkpoint["status"] = "STOPPED"
                atomic_write_json(checkpoint_path, checkpoint)
                return 0 if error is None else 2
        sleep(min(15, max(1, (due - now).total_seconds())))


if __name__ == "__main__":
    raise SystemExit(main())

