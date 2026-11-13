#!/usr/bin/env python3
"""Run course-derived strategies on rolling public WhiteBIT futures history.

This durable supervisor retrains no paid model and has no order path.  It
refreshes completed 15-minute BTC/ETH/SOL perpetual candles, runs only the
strict Daily -> H1 -> M15 Quasimodo and confirmed consolidation/range families
through locked chronological partitions, and persists failures and survivors.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
from tools.windows_power_status import running_on_battery


SCHEMA = "autotrade-whitebit-course-supervisor-v1"
MARKETS = ("BTC_PERP", "ETH_PERP", "SOL_PERP")
INTERVAL = "15m"
INTERVAL_SECONDS = 900
ACTIVE_FAMILIES = ("TREND_QUASIMODO_TOP_DOWN", "RANGE_DEVIATION")
ACTIVE_PHASES = (None, "RUNNING", "PAUSED", "ANALYZING")


class CourseStopped(Exception):
    """Stop at a durable course phase boundary."""


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
            "net_pnl", "fees", "funding", "signal_count", "detected_setups",
            "expired_setups", "detection_rejections", "confirmation_rejections",
            "pretrade_holds", "pretrade_hold_reasons")}
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


def _digest(value: dict) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()


def _screen_cycle(*, state_dir: Path, bars: int, now: datetime) -> dict:
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
    trend_episodes: dict[str, list[dict]] = {}
    range_episodes: dict[str, list[dict]] = {}
    trend_top_down = screen_trend(datasets, train_count=train_count,
        development_count=development_count,
        configuration=CourseConfiguration(minimum_shift_confirmation_bars=5,
            shift_expiry_bars=8, require_true_top_down=True),
        minimum_train_cycles=minimum_train,
        minimum_development_cycles=2, minimum_final_cycles=2,
        train_episodes=trend_episodes)
    ranges = screen_range(datasets, train_count=train_count,
        development_count=development_count,
        configuration=RangeDeviationConfiguration(),
        minimum_train_cycles=minimum_train,
        minimum_development_cycles=2, minimum_final_cycles=2,
        train_episodes=range_episodes)
    return {"schema": SCHEMA, "observed_at": now.astimezone(UTC).isoformat(),
        "window": {"start_open_epoch": start, "end_open_epoch": end, "bars": bars,
            "train_count": train_count, "development_count": development_count,
            "final_count": final_count}, "manifests": manifests,
        "families": {"TREND_QUASIMODO_TOP_DOWN": trend_top_down,
            "RANGE_DEVIATION": ranges},
        "train_episodes": {"TREND_QUASIMODO_TOP_DOWN": trend_episodes,
            "RANGE_DEVIATION": range_episodes},
        "entry_policy": {"mode": "STRICT_ANALYSIS_BEFORE_ENTRY",
            "active_families": list(ACTIVE_FAMILIES),
            "legacy_loose_trend_disabled": True,
            "no_complete_thesis_action": "HOLD"},
        "automatic_promotion": False, "network_orders": False,
        "trading_authority": False, "paid_model_calls": 0}


def _review_training(full: dict) -> dict:
    families = {}
    for name, result in full["families"].items():
        rows = [episode for episodes in full["train_episodes"][name].values() for episode in episodes]
        exits: dict[str, dict] = defaultdict(
            lambda: {"cycles": 0, "wins": 0, "fees_or_funding_turned_win_to_loss": 0,
                     "net": Decimal(0),
                     "fees": Decimal(0), "funding": Decimal(0)}
        )
        for episode in rows:
            exit_group = exits[episode["exit_reason"]]
            net = Decimal(episode["pnl_after_costs"])
            fees = Decimal(episode["fees"])
            funding = Decimal(episode["funding"])
            exit_group["cycles"] += 1
            exit_group["wins"] += int(episode["profitable"])
            exit_group["fees_or_funding_turned_win_to_loss"] += int(net <= 0 < net + fees + funding)
            exit_group["net"] += net
            exit_group["fees"] += fees
            exit_group["funding"] += funding
        families[name] = {
            "recorded_cycles": len(rows),
            "train_gate_reasons": result["train_rejection_reasons"],
            "pretrade_hold_reasons": result["train"]["pretrade_hold_reasons"],
            "process_pause_reasons": result["train_process_review"]["pause_reasons"],
            "loss_and_cost_by_exit": {
                reason: {**group, "net": str(group["net"]), "fees": str(group["fees"]),
                         "funding": str(group["funding"])}
                for reason, group in sorted(exits.items())
            },
            "process_review_sha256": result["train_process_review"]["review_sha256"],
        }
    review = {"source_partition": "TRAIN_ONLY", "families": families,
        "strategy_changed": False, "automatic_promotion": False}
    review["review_sha256"] = _digest(review)
    return review


def run_cycle(*, state_dir: Path, bars: int, now: datetime, advance,
              staged_path: Path, staged_required: bool) -> dict:
    if staged_path.is_symlink():
        raise ValueError("staged course screen is redirected")
    if staged_path.exists():
        if staged_path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("staged course screen is redirected or unbounded")
        staged = json.loads(staged_path.read_bytes())
        full = staged["screen"]
        if (staged["screen_sha256"] != _digest(full) or full["schema"] != SCHEMA
                or full["window"]["bars"] != bars
                or full["observed_at"] != now.astimezone(UTC).isoformat()):
            raise ValueError("staged course screen does not match the interrupted cycle")
    else:
        if staged_required:
            raise ValueError("staged course screen is missing after pause")
        full = _screen_cycle(state_dir=state_dir, bars=bars, now=now)
        atomic_write_json(staged_path, {"screen": full, "screen_sha256": _digest(full)})
    advance("PAUSED")
    advance("ANALYZING")
    review = _review_training(full)
    full["training_review"] = review
    full["session_lifecycle"] = {
        "phase": "ANALYSIS_COMPLETE",
        "phase_transitions": ["RUNNING", "PAUSED", "ANALYZING", "COOLDOWN"],
        "analysis_partition": "TRAIN_ONLY",
        "review_sha256": review["review_sha256"],
        "same_frozen_strategies_required": True,
        "resume_condition": "NEXT_SCHEDULED_COMPLETED_WINDOW_AFTER_COOLDOWN",
        "family_process_phases": {
            name: result["train_process_review"]["phase"]
            for name, result in full["families"].items()
        },
        "strategy_changed": False,
    }
    identity = _digest(full)
    full["cycle_sha256"] = identity
    run_path = state_dir / "runs" / f"{identity}.json"
    atomic_write_json(run_path, full)
    return {"cycle_sha256": identity, "run_path": str(run_path),
        "window": full["window"], "manifests": full["manifests"],
        "families": {name: _compact(value) for name, value in full["families"].items()},
        "training_review": review,
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
    parser.add_argument("--defer-on-battery", action="store_true")
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
    checkpoint.setdefault("status", "COOLDOWN")
    checkpoint.update(schema=SCHEMA,
        updated_at=datetime.now(UTC).isoformat(), automatic_promotion=False,
        network_orders=False, trading_authority=False, paid_model_calls=0)
    atomic_write_json(checkpoint_path, checkpoint)
    while True:
        now = datetime.now(UTC); day = now.date().isoformat()
        cycles_today = int(checkpoint.get("cycles_today", 0)) if checkpoint.get("utc_day") == day else 0
        if args.stop_file and args.stop_file.exists():
            checkpoint.update(status="STOPPED", updated_at=now.isoformat())
            atomic_write_json(checkpoint_path, checkpoint); return 0
        if args.defer_on_battery and running_on_battery():
            if checkpoint["status"] != "WAITING_FOR_AC_POWER":
                checkpoint.update(status="WAITING_FOR_AC_POWER", updated_at=now.isoformat())
                atomic_write_json(checkpoint_path, checkpoint)
            if args.once:
                return 0
            sleep(15)
            continue
        due_text = checkpoint.get("next_run_at")
        due = datetime.fromisoformat(due_text) if type(due_text) is str else now
        if cycles_today >= args.max_cycles_per_utc_day:
            due = max(due, datetime.combine(now.date() + timedelta(days=1),
                datetime.min.time(), UTC))
        pending = checkpoint.get("pending_cycle")
        if now < due and checkpoint["status"] in ("WAITING_FOR_AC_POWER", "RUNNING", "STOPPED"):
            checkpoint.update(status="RECOVERING" if pending else "COOLDOWN", updated_at=now.isoformat())
            atomic_write_json(checkpoint_path, checkpoint)
        if args.once and now < due:
            return 0
        if now >= due:
            started = monotonic()
            next_cycle_count = int(checkpoint.get("cycle_count", 0)) + 1
            if pending is None:
                pending = {"observed_at": now.isoformat(), "bars": args.bars,
                           "cycle_count": next_cycle_count, "phase": None}
                checkpoint["pending_cycle"] = pending

            def advance(phase: str) -> None:
                if args.stop_file and args.stop_file.exists():
                    raise CourseStopped
                previous = pending["phase"]
                if ACTIVE_PHASES.index(phase) <= ACTIVE_PHASES.index(previous):
                    if checkpoint["status"] != previous:
                        checkpoint.update(status=previous, updated_at=datetime.now(UTC).isoformat())
                        atomic_write_json(checkpoint_path, checkpoint)
                    return
                clock = datetime.now(UTC)
                pending["phase"] = phase
                checkpoint.update(status=phase, updated_at=clock.isoformat())
                atomic_write_json(checkpoint_path, checkpoint)
                _append_session_event(session_events_path, phase=phase,
                    cycle_count=next_cycle_count, observed_at=clock)

            try:
                if pending["cycle_count"] != next_cycle_count:
                    raise ValueError("staged course cycle count differs")
                cycle_now = datetime.fromisoformat(pending["observed_at"])
                staged_path = args.state_dir / f"pending-course-screen-{next_cycle_count}.json"
                advance("RUNNING")
                result = run_cycle(state_dir=args.state_dir, bars=pending["bars"], now=cycle_now,
                    advance=advance, staged_path=staged_path,
                    staged_required=pending["phase"] in ("PAUSED", "ANALYZING"))
                status, error = "COOLDOWN", None
            except CourseStopped:
                checkpoint.update(status="STOPPED", updated_at=datetime.now(UTC).isoformat())
                atomic_write_json(checkpoint_path, checkpoint)
                return 0
            except (HistoricalArchiveError, OSError, ValueError, TypeError,
                    KeyError, RuntimeError) as exc:
                result = checkpoint.get("last_result")
                status, error = "RECOVERING", type(exc).__name__
            completed_at = datetime.now(UTC)
            checkpoint = {"schema": SCHEMA, "status": status, "utc_day": day,
                "cycles_today": cycles_today + 1,
                "cycle_count": next_cycle_count - int(error is not None),
                "last_result": result, "last_error_class": error,
                "last_cycle_seconds": monotonic() - started,
                "updated_at": completed_at.isoformat(),
                "next_run_at": (completed_at + timedelta(seconds=args.interval_seconds)).isoformat(),
                "automatic_promotion": False, "network_orders": False,
                "trading_authority": False, "paid_model_calls": 0}
            if error is not None:
                checkpoint["pending_cycle"] = pending
            atomic_write_json(checkpoint_path, checkpoint)
            if error is None:
                _append_session_event(session_events_path, phase="COOLDOWN",
                    cycle_count=next_cycle_count, observed_at=completed_at,
                    cycle_sha256=result["cycle_sha256"])
                staged_path.unlink(missing_ok=True)
            if args.once:
                checkpoint.update(status="STOPPED", updated_at=datetime.now(UTC).isoformat())
                atomic_write_json(checkpoint_path, checkpoint)
                return 0 if error is None else 2
        sleep(min(15, max(1, (due - now).total_seconds())))


if __name__ == "__main__":
    raise SystemExit(main())
