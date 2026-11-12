#!/usr/bin/env python3
"""Repeat bounded local-only WhiteBIT trade-flow research without authority."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import signal
import sys
from threading import Event
from time import monotonic


ROOT = Path(__file__).resolve().parents[1]
for dependency_root in (ROOT, ROOT / "research", ROOT / "mvp", ROOT / "src"):
    if str(dependency_root) not in sys.path:
        sys.path.insert(0, str(dependency_root))

from autotrade_research.artifacts.durable_publish import atomic_write_json
from autotrade_research.artifacts.resource_lock import ResourceLock
from autotrade_research.strategies.flow_volatility_screen import (
    screen_flow_volatility,
)
from autotrade_research.strategies.medium_horizon_trade_flow import (
    screen_medium_horizon_flow,
)
from tools.screen_whitebit_flow_lead_lag import DEFAULT_MARKETS, run as run_lead_lag


SCHEMA = "autotrade-whitebit-flow-research-supervisor-v1"
RUN_SCHEMA = "autotrade-whitebit-flow-research-run-v1"
MEDIUM_MINIMUM_BUCKETS = 900
MEDIUM_ROUND_TRIP_COST_BPS = "26"


def _digest(payload: dict) -> str:
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _bounded_runs(path: Path, maximum: int) -> None:
    runs = sorted(
        (item for item in path.glob("*.json") if item.is_file() and not item.is_symlink()),
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    )
    for stale in runs[maximum:]:
        stale.unlink(missing_ok=True)


def execute_cycle(*, trade_tape: Path, state_dir: Path, markets: list[str],
                  minimum_buckets: int, round_trip_cost_bps: str,
                  maximum_runs: int, now: datetime) -> dict:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("flow-research clock must be timezone-aware")
    volatility = {}
    volatility_errors = []
    for market in markets:
        try:
            row = screen_flow_volatility(
                trade_tape, market, minimum_buckets=minimum_buckets,
                minimum_trades=3, round_trip_cost_bps=round_trip_cost_bps,
            )
            volatility[row["market"]] = row
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            volatility_errors.append({
                "market": market, "error_class": type(exc).__name__,
            })
    lead_lag = run_lead_lag(
        trade_tape, markets, minimum_buckets=minimum_buckets,
        minimum_trades=6, round_trip_cost_bps=round_trip_cost_bps,
    )
    medium_horizon = {}
    medium_horizon_errors = []
    for market in markets:
        try:
            row = screen_medium_horizon_flow(
                trade_tape, market,
                minimum_buckets=MEDIUM_MINIMUM_BUCKETS,
                minimum_trades=2,
                round_trip_cost_bps=MEDIUM_ROUND_TRIP_COST_BPS,
            )
            medium_horizon[row["market"]] = row
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            medium_horizon_errors.append({
                "market": market, "error_class": type(exc).__name__,
            })
    report = {
        "schema": RUN_SCHEMA,
        "created_at": now.astimezone(UTC).isoformat(),
        "minimum_buckets": minimum_buckets,
        "round_trip_cost_bps": round_trip_cost_bps,
        "volatility": {
            "market_count": len(volatility),
            "qualified_count": sum(
                bool(row["prospective_qualification"]) for row in volatility.values()
            ),
            "waiting_count": sum(
                row["status"] == "WAITING_FOR_CAUSAL_BUCKETS" for row in volatility.values()
            ),
            "errors": volatility_errors,
            "results": volatility,
        },
        "lead_lag": lead_lag,
        "medium_horizon": {
            "minimum_buckets": MEDIUM_MINIMUM_BUCKETS,
            "round_trip_cost_bps": MEDIUM_ROUND_TRIP_COST_BPS,
            "market_count": len(medium_horizon),
            "qualified_count": sum(
                bool(row["prospective_qualification"])
                for row in medium_horizon.values()
            ),
            "waiting_count": sum(
                row["status"] == "WAITING_FOR_CAUSAL_BUCKETS"
                for row in medium_horizon.values()
            ),
            "rejected_count": sum(
                row["status"] == "REJECTED"
                for row in medium_horizon.values()
            ),
            "errors": medium_horizon_errors,
            "results": medium_horizon,
        },
        "paid_model_calls": 0,
        "network_requests": 0,
        "network_orders": False,
        "trading_authority": False,
        "automatic_promotion": False,
    }
    report["result_sha256"] = _digest(report)
    runs = state_dir / "runs"
    atomic_write_json(runs / f"{report['result_sha256']}.json", report)
    _bounded_runs(runs, maximum_runs)
    return report


def _wait(stopped: Event, stop_file: Path, seconds: float) -> bool:
    deadline = monotonic() + max(0, seconds)
    while monotonic() < deadline:
        if stopped.wait(min(1.0, deadline - monotonic())) or stop_file.exists():
            return True
    return stopped.is_set() or stop_file.exists()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--market", action="append", dest="markets")
    parser.add_argument("--minimum-buckets", type=int, default=300)
    parser.add_argument("--round-trip-cost-bps", default="26")
    parser.add_argument("--interval-seconds", type=int, default=1800)
    parser.add_argument("--maximum-runs", type=int, default=48)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    markets = args.markets or list(DEFAULT_MARKETS)
    if len(markets) < 2 or len(set(markets)) != len(markets):
        parser.error("at least two unique markets are required")
    if args.minimum_buckets < 300:
        parser.error("minimum-buckets must be at least 300")
    if not 300 <= args.interval_seconds <= 604800:
        parser.error("interval-seconds must be 300..604800")
    if not 1 <= args.maximum_runs <= 336:
        parser.error("maximum-runs must be 1..336")
    state_dir = args.state_dir.resolve(strict=False)
    state_dir.mkdir(parents=True, exist_ok=True)
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise SystemExit("state-dir must be an ordinary directory")
    trade_tape = args.trade_tape.resolve(strict=False)
    stop_file = args.stop_file.resolve(strict=False)
    try:
        stop_file.relative_to(state_dir)
    except ValueError:
        raise SystemExit("stop-file must remain inside state-dir") from None
    checkpoint_path = state_dir / "checkpoint.json"
    stopped = Event()
    previous = {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda *_: stopped.set())
    try:
        with ResourceLock(state_dir / "flow-research-supervisor.lock"):
            stop_file.unlink(missing_ok=True)
            checkpoint = {}
            if checkpoint_path.is_file():
                if checkpoint_path.is_symlink() or checkpoint_path.stat().st_size > 4 << 20:
                    raise ValueError("flow-research checkpoint is redirected or unbounded")
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                if checkpoint.get("schema") != SCHEMA:
                    raise ValueError("flow-research supervisor schema differs")
                last_result = checkpoint.get("last_result")
                if (type(last_result) is dict
                        and "medium_horizon_qualified_count" not in last_result):
                    checkpoint.pop("next_run_at", None)
            if checkpoint:
                checkpoint.update(
                    status="RUNNING",
                    updated_at=datetime.now(UTC).isoformat(),
                    paid_model_calls=0,
                    network_requests=0,
                    network_orders=False,
                    trading_authority=False,
                    automatic_promotion=False,
                )
                atomic_write_json(checkpoint_path, checkpoint)
            while not stopped.is_set() and not stop_file.exists():
                now = datetime.now(UTC)
                due_text = checkpoint.get("next_run_at")
                due = datetime.fromisoformat(due_text) if type(due_text) is str else now
                if now < due and not args.once:
                    if _wait(stopped, stop_file, (due - now).total_seconds()):
                        break
                    continue
                started = monotonic()
                try:
                    report = execute_cycle(
                        trade_tape=trade_tape, state_dir=state_dir,
                        markets=markets, minimum_buckets=args.minimum_buckets,
                        round_trip_cost_bps=args.round_trip_cost_bps,
                        maximum_runs=args.maximum_runs, now=now,
                    )
                    result = {
                        "result_sha256": report["result_sha256"],
                        "evidence_path": str(
                            state_dir / "runs" / f"{report['result_sha256']}.json"
                        ),
                        "volatility_qualified_count": report["volatility"]["qualified_count"],
                        "lead_lag_qualified_count": report["lead_lag"]["qualified_pair_count"],
                        "medium_horizon_qualified_count": report["medium_horizon"]["qualified_count"],
                        "medium_horizon_waiting_count": report["medium_horizon"]["waiting_count"],
                        "medium_horizon_rejected_count": report["medium_horizon"]["rejected_count"],
                        "medium_horizon_error_count": len(report["medium_horizon"]["errors"]),
                        "isolated_error_count": (
                            len(report["volatility"]["errors"])
                            + len(report["lead_lag"]["errors"])
                            + len(report["medium_horizon"]["errors"])
                        ),
                    }
                    status, error = "RUNNING", None
                except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                    result = checkpoint.get("last_result")
                    status, error = "RECOVERING", type(exc).__name__
                checkpoint = {
                    "schema": SCHEMA,
                    "status": status,
                    "cycle_count": int(checkpoint.get("cycle_count", 0)) + 1,
                    "last_result": result,
                    "last_error_class": error,
                    "last_cycle_seconds": monotonic() - started,
                    "updated_at": datetime.now(UTC).isoformat(),
                    "next_run_at": (now + timedelta(seconds=args.interval_seconds)).isoformat(),
                    "paid_model_calls": 0,
                    "network_requests": 0,
                    "network_orders": False,
                    "trading_authority": False,
                    "automatic_promotion": False,
                }
                atomic_write_json(checkpoint_path, checkpoint)
                if args.once:
                    return 0 if error is None else 1
            checkpoint.update(status="STOPPED", updated_at=datetime.now(UTC).isoformat())
            atomic_write_json(checkpoint_path, checkpoint)
            return 0
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
