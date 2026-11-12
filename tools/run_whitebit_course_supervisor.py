#!/usr/bin/env python3
"""Run course-derived strategies on rolling public WhiteBIT futures history.

This durable supervisor retrains no paid model and has no order path.  It
refreshes completed 15-minute BTC/ETH/SOL perpetual candles, runs only the
strict Daily -> H1 -> M15 Quasimodo and confirmed consolidation/range families
through locked chronological partitions, and persists failures and survivors.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from time import monotonic, sleep

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.strategies.course_quasimodo import CourseConfiguration
from research.autotrade_research.strategies.course_range_deviation import RangeDeviationConfiguration
from tools.historical_public_klines import HistoricalArchiveError
from tools.screen_whitebit_course_quasimodo_universe import screen_universe as screen_trend
from tools.screen_whitebit_course_range_universe import screen_universe as screen_range
from tools.screen_whitebit_trend_regime import authenticated_bars
from tools.whitebit_historical_klines import collect_whitebit_history


SCHEMA = "autotrade-whitebit-course-supervisor-v1"
MARKETS = ("BTC_PERP", "ETH_PERP", "SOL_PERP")
INTERVAL = "15m"
INTERVAL_SECONDS = 900
ACTIVE_FAMILIES = ("TREND_QUASIMODO_TOP_DOWN", "RANGE_DEVIATION")


def completed_window(now: datetime, bars: int) -> tuple[int, int]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("course supervisor clock must be timezone-aware")
    if type(bars) is not int or not 4000 <= bars <= 80_000:
        raise ValueError("course supervisor bars must be 4000..80000")
    current = int(now.astimezone(UTC).timestamp()) // INTERVAL_SECONDS * INTERVAL_SECONDS
    end = current - INTERVAL_SECONDS
    return end - (bars - 1) * INTERVAL_SECONDS, end


def partition_counts(bars: int) -> tuple[int, int, int]:
    final = max(1000, bars // 5)
    development = max(1000, bars // 5)
    train = bars - development - final
    if train < 2000:
        raise ValueError("course supervisor needs at least 2000 TRAIN bars")
    return train, development, final


def _compact(result: dict) -> dict:
    def part(name: str) -> dict | None:
        value = result.get(name)
        if value is None:
            return None
        return {key: value.get(key) for key in (
            "market_count", "closed_cycles", "wins", "losses", "win_rate",
            "net_pnl", "fees", "funding")}
    review = result.get("train_process_review")
    review_projection = None
    if type(review) is dict:
        review_projection = {key: review.get(key) for key in (
            "schema", "source_partition", "phase", "episode_count",
            "market_count", "journal_complete", "incomplete_journal_count",
            "maximum_observed_consecutive_losses", "pause_reasons",
            "resume_condition", "same_frozen_strategy_required",
            "review_sha256")}
        review_projection["loss_streak_pause_event_count"] = len(
            review.get("loss_streak_pause_events", []))
        review_projection["daily_process_event_count"] = len(
            review.get("daily_process_events", []))
        review_projection["weekly_process_event_count"] = len(
            review.get("weekly_process_events", []))
    return {"strategy_schema": result.get("strategy_schema"),
        "train": part("train"), "train_rejection_reasons": result.get("train_rejection_reasons"),
        "train_process_review": review_projection,
        "development": part("development"),
        "development_rejection_reasons": result.get("development_rejection_reasons"),
        "final": part("final"), "final_rejection_reasons": result.get("final_rejection_reasons"),
        "development_consumed": result.get("development_consumed"),
        "final_consumed": result.get("final_consumed"),
        "prospective_qualification": result.get("prospective_qualification"),
        "result_sha256": result.get("result_sha256"),
        "automatic_promotion": False, "network_orders": False,
        "trading_authority": False, "paid_model_calls": 0}


def _append_session_event(path: Path, *, phase: str, cycle_count: int,
                          observed_at: datetime, cycle_sha256: str | None = None) -> None:
    value = {"schema": "autotrade-course-session-event-v1", "phase": phase,
        "cycle_count": cycle_count, "observed_at": observed_at.astimezone(UTC).isoformat(),
        "cycle_sha256": cycle_sha256, "strategy_changed": False,
        "network_orders": False, "trading_authority": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush(); os.fsync(stream.fileno())


def run_cycle(*, state_dir: Path, bars: int, now: datetime) -> dict:
    start, end = completed_window(now, bars)
    train_count, development_count, final_count = partition_counts(bars)
    history_root = state_dir / "history"
    datasets = []
    manifests = {}
    for market in MARKETS:
        manifest = collect_whitebit_history(market=market, interval=INTERVAL,
            start=start, end=end, output=history_root, now=now.astimezone(UTC))
        path = history_root / "spot" / market / INTERVAL / f"{start}-{end}.manifest.json"
        rows, identity, parsed = authenticated_bars(path)
        datasets.append({"market": market, "bars": rows,
            "dataset_identity_sha256": identity})
        manifests[market] = {"path": str(path),
            "dataset_identity_sha256": manifest["dataset_identity_sha256"],
            "row_count": parsed["row_count"]}
    minimum_train = max(3, len(MARKETS))
    trend_top_down = screen_trend(datasets, train_count=train_count,
        development_count=development_count,
        configuration=CourseConfiguration(minimum_shift_confirmation_bars=5,
            shift_expiry_bars=8, require_true_top_down=True),
        minimum_train_cycles=minimum_train,
        minimum_development_cycles=2, minimum_final_cycles=2)
    ranges = screen_range(datasets, train_count=train_count,
        development_count=development_count,
        configuration=RangeDeviationConfiguration(),
        minimum_train_cycles=minimum_train,
        minimum_development_cycles=2, minimum_final_cycles=2)
    full = {"schema": SCHEMA, "observed_at": now.astimezone(UTC).isoformat(),
        "window": {"start_open_epoch": start, "end_open_epoch": end, "bars": bars,
            "train_count": train_count, "development_count": development_count,
            "final_count": final_count}, "manifests": manifests,
        "families": {"TREND_QUASIMODO_TOP_DOWN": trend_top_down,
            "RANGE_DEVIATION": ranges},
        "entry_policy": {"mode": "STRICT_ANALYSIS_BEFORE_ENTRY",
            "active_families": list(ACTIVE_FAMILIES),
            "legacy_loose_trend_disabled": True,
            "no_complete_thesis_action": "HOLD"},
        "session_lifecycle": {"phase": "ANALYSIS_COMPLETE",
            "phase_transitions": ["RUNNING", "PAUSED", "ANALYZING", "COOLDOWN"],
            "analysis_partition": "TRAIN_ONLY",
            "same_frozen_strategies_required": True,
            "resume_condition": "NEXT_SCHEDULED_COMPLETED_WINDOW_AFTER_COOLDOWN",
            "family_process_phases": {
                "TREND_QUASIMODO_TOP_DOWN": trend_top_down["train_process_review"]["phase"],
                "RANGE_DEVIATION": ranges["train_process_review"]["phase"]},
            "strategy_changed": False},
        "automatic_promotion": False, "network_orders": False,
        "trading_authority": False, "paid_model_calls": 0}
    identity = sha256(json.dumps(full, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()
    full["cycle_sha256"] = identity
    run_path = state_dir / "runs" / f"{identity}.json"
    atomic_write_json(run_path, full)
    return {"cycle_sha256": identity, "run_path": str(run_path),
        "window": full["window"], "manifests": manifests,
        "families": {name: _compact(value) for name, value in full["families"].items()},
        "session_lifecycle": full["session_lifecycle"],
        "automatic_promotion": False, "network_orders": False,
        "trading_authority": False, "paid_model_calls": 0}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--bars", type=int, default=20_000)
    parser.add_argument("--interval-seconds", type=int, default=21600)
    parser.add_argument("--max-cycles-per-utc-day", type=int, default=4)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    completed_window(datetime.now(UTC), args.bars)
    if not 3600 <= args.interval_seconds <= 86400:
        parser.error("interval must be 3600..86400 seconds")
    if not 1 <= args.max_cycles_per_utc_day <= 24:
        parser.error("daily cycle budget must be 1..24")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = args.state_dir / "checkpoint.json"
    session_events_path = args.state_dir / "course-session-events.jsonl"
    checkpoint = {}
    if checkpoint_path.is_file():
        if checkpoint_path.is_symlink() or checkpoint_path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("course supervisor checkpoint is redirected or unbounded")
        checkpoint = json.loads(checkpoint_path.read_bytes())
        if checkpoint.get("schema") != SCHEMA:
            raise ValueError("course supervisor checkpoint schema differs")
    checkpoint.update(schema=SCHEMA, status="RUNNING",
        updated_at=datetime.now(UTC).isoformat(), automatic_promotion=False,
        network_orders=False, trading_authority=False, paid_model_calls=0)
    atomic_write_json(checkpoint_path, checkpoint)
    while True:
        now = datetime.now(UTC); day = now.date().isoformat()
        cycles_today = int(checkpoint.get("cycles_today", 0)) if checkpoint.get("utc_day") == day else 0
        if args.stop_file and args.stop_file.exists():
            checkpoint.update(status="STOPPED", updated_at=now.isoformat())
            atomic_write_json(checkpoint_path, checkpoint); return 0
        due_text = checkpoint.get("next_run_at")
        due = datetime.fromisoformat(due_text) if type(due_text) is str else now
        if cycles_today >= args.max_cycles_per_utc_day:
            due = max(due, datetime.combine(now.date() + timedelta(days=1),
                datetime.min.time(), UTC))
        if now >= due:
            started = monotonic()
            next_cycle_count = int(checkpoint.get("cycle_count", 0)) + 1
            checkpoint.update(status="RUNNING", updated_at=now.isoformat())
            atomic_write_json(checkpoint_path, checkpoint)
            _append_session_event(session_events_path, phase="RUNNING",
                cycle_count=next_cycle_count, observed_at=now)
            try:
                result = run_cycle(state_dir=args.state_dir, bars=args.bars, now=now)
                status, error = "COOLDOWN", None
            except (HistoricalArchiveError, OSError, ValueError, TypeError,
                    KeyError, RuntimeError) as exc:
                result = checkpoint.get("last_result")
                status, error = "RECOVERING", type(exc).__name__
            cycles_today += 1
            checkpoint = {"schema": SCHEMA, "status": status, "utc_day": day,
                "cycles_today": cycles_today,
                "cycle_count": next_cycle_count,
                "last_result": result, "last_error_class": error,
                "last_cycle_seconds": monotonic() - started,
                "updated_at": datetime.now(UTC).isoformat(),
                "next_run_at": (now + timedelta(seconds=args.interval_seconds)).isoformat(),
                "automatic_promotion": False, "network_orders": False,
                "trading_authority": False, "paid_model_calls": 0}
            atomic_write_json(checkpoint_path, checkpoint)
            if error is None:
                for phase in ("PAUSED", "ANALYZING", "COOLDOWN"):
                    _append_session_event(session_events_path, phase=phase,
                        cycle_count=next_cycle_count, observed_at=datetime.now(UTC),
                        cycle_sha256=result["cycle_sha256"])
            if args.once:
                checkpoint["status"] = "STOPPED"
                atomic_write_json(checkpoint_path, checkpoint)
                return 0 if error is None else 2
        sleep(min(15, max(1, (due - now).total_seconds())))


if __name__ == "__main__":
    raise SystemExit(main())

