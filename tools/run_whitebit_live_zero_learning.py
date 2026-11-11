"""Continuously learn in ZERO mode from the existing WhiteBIT public supervisor."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sqlite3
import sys
from threading import Event
import time

ROOT = Path(__file__).resolve().parents[1]
for dependency_root in (ROOT, ROOT / "research", ROOT / "mvp"):
    if str(dependency_root) not in sys.path:
        sys.path.insert(0, str(dependency_root))

from research.autotrade_research.artifacts.resource_lock import ResourceLock
from research.autotrade_research.artifacts.store import ArtifactStore
from research.autotrade_research.learning.whitebit_live_zero import (
    WhiteBitLiveZeroLearning,
)
from research.autotrade_research.io.strict_json import strict_json_loads
from tools.run_whitebit_public_supervisor import _read_restart_state


def authenticated_latest(state_dir: Path) -> dict:
    """Read one coherent, artifact-authenticated public snapshot."""
    for _attempt in range(20):
        try:
            state = strict_json_loads(
                (state_dir / "supervisor-state.json").read_text(encoding="utf-8")
            )
            _count, latest = _read_restart_state(
                state_dir,
                market=state["market"],
                artifacts=ArtifactStore(state_dir / "artifacts"),
            )
        except (OSError, TypeError, ValueError):
            time.sleep(0.05)
            continue
        # _read_restart_state authenticates both the checkpoint evidence and
        # the current immutable snapshot, including the one-record crash
        # window.  A second status projection created an avoidable race on an
        # active BBO stream and could starve learning indefinitely.
        if latest is not None:
            return latest
        time.sleep(0.05)
    raise ValueError("WhiteBIT public supervisor cut changed during authenticated read")


def run(*, supervisor_state_dir: Path, state_dir: Path, interval_seconds: float,
        max_new_observations: int | None = None, stop: Event | None = None,
        stop_file: Path | None = None,
        cycle_episodes: int = 20, cycle_seconds: int = 1200,
        evaluation_episodes: int = 12, max_drawdown_usdt: str = "2",
        cooldown_seconds: int = 3600, max_trials: int = 3,
        max_observation_gap_seconds: int = 180, min_evaluation_trades: int = 3,
        outcome_horizon_seconds: int = 0) -> dict:
    if (isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float))
            or not 0.1 <= float(interval_seconds) <= 3600):
        raise ValueError("poll interval must be 0.1..3600 seconds")
    if max_new_observations is not None and (
        type(max_new_observations) is not int or max_new_observations < 1
    ):
        raise ValueError("max observations must be a positive integer")
    stop = stop or Event()
    processed = duplicates = unavailable = 0
    state_dir.mkdir(parents=True, exist_ok=True)
    with ResourceLock(state_dir / "whitebit-live-zero-learning.lock"):
        learner = WhiteBitLiveZeroLearning(state_dir, cycle_episodes=cycle_episodes,
            cycle_seconds=cycle_seconds, evaluation_episodes=evaluation_episodes,
            max_drawdown_usdt=max_drawdown_usdt, cooldown_seconds=cooldown_seconds,
            max_trials=max_trials, max_observation_gap_seconds=max_observation_gap_seconds,
            min_evaluation_trades=min_evaluation_trades,
            outcome_horizon_seconds=outcome_horizon_seconds)
        while not stop.is_set():
            if stop_file is not None and stop_file.is_file():
                stop.set()
                break
            if (state_dir / "learning-cycle.pause.json").exists():
                stop.set()
                break
            try:
                snapshot = authenticated_latest(supervisor_state_dir)
            except ValueError:
                unavailable += 1
                if stop.wait(float(interval_seconds)):
                    break
                continue
            result = learner.process_snapshot(snapshot)
            if result["status"] == "PROCESSED":
                processed += 1
                if max_new_observations is not None and processed >= max_new_observations:
                    break
            else:
                duplicates += 1
            if stop.wait(float(interval_seconds)):
                break
        return {
            "schema": "autotrade-whitebit-live-zero-run-v1",
            "status": "STOPPED_WITH_CHECKPOINT" if stop.is_set() else "COMPLETED_BOUND",
            "processed": processed,
            "duplicates": duplicates,
            "temporarily_unavailable_reads": unavailable,
            "state_dir": str(state_dir.absolute()),
            "checkpoint": learner.status(),
            "private_api_used": False,
            "network_orders": False,
            "trading_authority": False,
        }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Accumulate WhiteBIT public observations into restart-safe ZERO learning"
    )
    parser.add_argument("--supervisor-state-dir", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--max-new-observations", type=int)
    parser.add_argument("--cycle-episodes", type=int, default=20)
    parser.add_argument("--cycle-seconds", type=int, default=1200)
    parser.add_argument("--evaluation-episodes", type=int, default=12)
    parser.add_argument("--max-drawdown-usdt", default="2")
    parser.add_argument("--cooldown-seconds", type=int, default=3600)
    parser.add_argument("--max-trials", type=int, default=3)
    parser.add_argument("--max-observation-gap-seconds", type=int, default=180)
    parser.add_argument("--min-evaluation-trades", type=int, default=3)
    parser.add_argument("--outcome-horizon-seconds", type=int, default=0)
    parser.add_argument("--stop-file")
    args = parser.parse_args(argv)
    stop = Event()
    for name in ("SIGINT", "SIGTERM"):
        code = getattr(signal, name, None)
        if code is not None:
            signal.signal(code, lambda *_args: stop.set())
    try:
        result = run(
            supervisor_state_dir=Path(args.supervisor_state_dir),
            state_dir=Path(args.state_dir),
            interval_seconds=args.interval_seconds,
            max_new_observations=args.max_new_observations,
            stop=stop,
            stop_file=None if args.stop_file is None else Path(args.stop_file),
            cycle_episodes=args.cycle_episodes, cycle_seconds=args.cycle_seconds,
            evaluation_episodes=args.evaluation_episodes,
            max_drawdown_usdt=args.max_drawdown_usdt,
            cooldown_seconds=args.cooldown_seconds, max_trials=args.max_trials,
            max_observation_gap_seconds=args.max_observation_gap_seconds,
            min_evaluation_trades=args.min_evaluation_trades,
            outcome_horizon_seconds=args.outcome_horizon_seconds,
        )
        print(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        print(f"WhiteBIT live ZERO learning blocked: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
