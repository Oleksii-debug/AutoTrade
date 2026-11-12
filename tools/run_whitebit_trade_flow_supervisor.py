#!/usr/bin/env python3
"""Periodically screen accumulated WhiteBIT trade flow with locked FINAL."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import signal
import sys
from threading import Event
import time

ROOT = Path(__file__).resolve().parents[1]
for dependency_root in (ROOT, ROOT / "research"):
    if str(dependency_root) not in sys.path:
        sys.path.insert(0, str(dependency_root))

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.artifacts.resource_lock import ResourceLock
from research.autotrade_research.integrations.whitebit_public import _symbol
from research.autotrade_research.strategies.trade_flow_screen import (
    screen_trade_flow,
)


SCHEMA = "autotrade-whitebit-trade-flow-supervisor-v1"


def _wait(seconds: float, stopped: Event, stop_file: Path) -> None:
    deadline = time.monotonic() + seconds
    while not stopped.is_set() and not stop_file.exists():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        stopped.wait(min(1.0, remaining))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--market", action="append", required=True)
    parser.add_argument("--minimum-buckets", type=int, default=2160)
    parser.add_argument("--interval-seconds", type=float, default=900)
    parser.add_argument("--stop-file", type=Path, required=True)
    args = parser.parse_args(argv)
    markets = tuple(_symbol(value) for value in args.market)
    if not 1 <= len(markets) <= 16 or len(set(markets)) != len(markets):
        raise SystemExit("one to sixteen unique markets are required")
    if not 60 <= args.interval_seconds <= 86400:
        raise SystemExit("interval-seconds must be 60..86400")
    state_dir = args.state_dir.resolve(strict=False)
    state_dir.mkdir(parents=True, exist_ok=True)
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise SystemExit("state-dir must be an ordinary directory")
    stop_file = args.stop_file.resolve(strict=False)
    try:
        stop_file.relative_to(state_dir)
    except ValueError:
        raise SystemExit("stop-file must remain inside state-dir") from None
    stopped = Event()
    previous = {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda *_: stopped.set())
    try:
        with ResourceLock(state_dir / "trade-flow-supervisor.lock"):
            stop_file.unlink(missing_ok=True)
            last_checkpoint = None
            while not stopped.is_set() and not stop_file.exists():
                results = {}
                errors = {}
                for market in markets:
                    try:
                        result = screen_trade_flow(
                            args.trade_tape, market,
                            minimum_buckets=args.minimum_buckets,
                        )
                        results[market] = result
                        atomic_write_json(state_dir / f"{market.lower()}-latest.json", result)
                    except (OSError, ValueError) as error:
                        errors[market] = type(error).__name__
                checkpoint = {
                    "schema": SCHEMA,
                    "phase": "RUNNING" if results else "WAITING_FOR_TRADE_TAPE",
                    "checkpointed_at": datetime.now(timezone.utc).isoformat(),
                    "markets": list(markets),
                    "minimum_buckets": args.minimum_buckets,
                    "results": {market: {
                        "status": result["status"],
                        "available_causal_buckets": result["available_causal_buckets"],
                        "prospective_qualification": result["prospective_qualification"],
                        "final_consumed": result["final_consumed"],
                        "result_sha256": result["result_sha256"],
                    } for market, result in results.items()},
                    "errors": errors,
                    "network_orders": False,
                    "trading_authority": False,
                    "paid_model_calls": 0,
                }
                atomic_write_json(state_dir / "checkpoint.json", checkpoint)
                last_checkpoint = checkpoint
                _wait(args.interval_seconds, stopped, stop_file)
            if last_checkpoint is not None:
                atomic_write_json(state_dir / "checkpoint.json", {
                    **last_checkpoint,
                    "phase": "STOPPED",
                    "checkpointed_at": datetime.now(timezone.utc).isoformat(),
                })
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

