#!/usr/bin/env python3
"""Periodically evaluate the small WhiteBIT L2 OFI registry with locked FINAL."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import signal
import sqlite3
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
from research.autotrade_research.strategies.depth_ofi_screen import screen_depth_ofi


SCHEMA = "autotrade-whitebit-depth-ofi-supervisor-v1"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--depth-tape", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--market", action="append", required=True)
    parser.add_argument("--minimum-buckets", type=int, default=3600)
    parser.add_argument("--interval-seconds", type=float, default=900)
    parser.add_argument("--stop-file", type=Path, required=True)
    args = parser.parse_args(argv)
    markets = tuple(_symbol(value) for value in args.market)
    if not 1 <= len(markets) <= 8 or len(markets) != len(set(markets)):
        raise SystemExit("one to eight unique markets are required")
    if not 60 <= args.interval_seconds <= 86400:
        raise SystemExit("interval-seconds must be 60..86400")
    state_dir = args.state_dir.resolve(strict=False); state_dir.mkdir(parents=True, exist_ok=True)
    stop_file = args.stop_file.resolve(strict=False)
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise SystemExit("state-dir must be an ordinary directory")
    try: stop_file.relative_to(state_dir)
    except ValueError: raise SystemExit("stop-file must remain inside state-dir") from None
    stopped = Event(); previous = {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda *_: stopped.set())
    try:
        with ResourceLock(state_dir / "depth-ofi-supervisor.lock"):
            stop_file.unlink(missing_ok=True)
            while not stopped.is_set() and not stop_file.exists():
                results = {}; errors = {}
                for market in markets:
                    try:
                        result = screen_depth_ofi(args.depth_tape, market,
                            minimum_buckets=args.minimum_buckets)
                        results[market] = result
                        atomic_write_json(state_dir / f"{market.lower()}-latest.json", result)
                    except (OSError, ValueError, sqlite3.Error) as error:
                        errors[market] = type(error).__name__
                checkpoint = {"schema": SCHEMA,
                    "phase": "RUNNING" if results else "WAITING_FOR_DEPTH_TAPE",
                    "checkpointed_at": datetime.now(timezone.utc).isoformat(),
                    "markets": list(markets), "minimum_buckets": args.minimum_buckets,
                    "results": {market: {key: result.get(key) for key in (
                        "status", "available_causal_buckets", "prospective_qualification",
                        "final_consumed", "result_sha256")} for market, result in results.items()},
                    "errors": errors, "candidate_count_per_market": 4,
                    "network_orders": False, "trading_authority": False,
                    "paid_model_calls": 0}
                atomic_write_json(state_dir / "checkpoint.json", checkpoint)
                deadline = time.monotonic() + args.interval_seconds
                while not stopped.is_set() and not stop_file.exists() and time.monotonic() < deadline:
                    stopped.wait(min(1, max(0, deadline - time.monotonic())))
    finally:
        for signum, handler in previous.items(): signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

