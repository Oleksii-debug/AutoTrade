#!/usr/bin/env python3
"""Collect restart-safe public WhiteBIT L2 features for selective research."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import signal
from threading import Event
import sys

ROOT = Path(__file__).resolve().parents[1]
for dependency_root in (ROOT, ROOT / "research"):
    if str(dependency_root) not in sys.path:
        sys.path.insert(0, str(dependency_root))

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.artifacts.resource_lock import ResourceLock
from research.autotrade_research.integrations.whitebit_depth import (
    WhiteBitDepthWebSocketTransport,
)
from research.autotrade_research.integrations.whitebit_public import _symbol
from research.autotrade_research.learning.whitebit_depth_tape import (
    WhiteBitDepthTapeStore,
)


def _checkpoint(path: Path, store: WhiteBitDepthTapeStore, *, phase: str,
                markets: tuple[str, ...], observations: int,
                errors: dict[str, str]) -> None:
    atomic_write_json(path, {
        **store.status(), "phase": phase,
        "transport": "WHITEBIT_PUBLIC_DEPTH_V1", "markets": list(markets),
        "new_observations_last_checkpoint": observations, "errors": errors,
        "checkpointed_at": datetime.now(timezone.utc).isoformat(),
        "secrets_in_state": False, "paid_model_calls": 0,
    })


async def _collect(args, stopped: Event, store: WhiteBitDepthTapeStore,
                   checkpoint: Path, markets: tuple[str, ...]) -> None:
    stop = asyncio.Event()
    phases = {market: "STARTING" for market in markets}
    errors: dict[str, str] = {}
    added = 0

    def diagnostic(market: str, value) -> None:
        if value.code == "SUBSCRIBED":
            phases[market] = "RUNNING"
            errors.pop(market, None)
        elif value.code == "STOPPED":
            phases[market] = "STOPPED"
        else:
            phases[market] = "DEGRADED"
            errors[market] = value.code

    async def consume(market: str) -> None:
        nonlocal added
        transport = WhiteBitDepthWebSocketTransport(
            market=market, limit=args.depth_limit,
            diagnostic=lambda value: diagnostic(market, value),
        )
        async for observation in transport.observations(stop):
            store.ingest(observation)
            added += 1

    consumers = [asyncio.create_task(consume(market)) for market in markets]
    try:
        while not stopped.is_set() and not args.stop_file.exists():
            if all(value == "RUNNING" for value in phases.values()):
                phase = "RUNNING"
            elif any(value == "RUNNING" for value in phases.values()):
                phase = "DEGRADED"
            else:
                phase = "STARTING" if not errors else "DEGRADED"
            _checkpoint(checkpoint, store, phase=phase, markets=markets,
                        observations=added, errors=errors)
            added = 0
            await asyncio.sleep(args.poll_seconds)
    finally:
        stop.set()
        done, pending = await asyncio.wait(consumers, timeout=10)
        for task in pending:
            task.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)
        _checkpoint(checkpoint, store, phase="STOPPED", markets=markets,
                    observations=added, errors=errors)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--market", action="append", required=True)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--bucket-seconds", type=int, default=1)
    parser.add_argument("--retention-days", type=int, default=3)
    parser.add_argument("--depth-limit", type=int, default=20,
                        choices=(1, 5, 10, 20, 50, 100))
    parser.add_argument("--stop-file", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= len(args.market) <= 12:
        raise SystemExit("one to twelve markets are required")
    markets = tuple(_symbol(value) for value in args.market)
    if len(markets) != len(set(markets)):
        raise SystemExit("markets must be unique")
    if not 1 <= args.poll_seconds <= 60:
        raise SystemExit("poll-seconds must be 1..60")
    state_dir = args.state_dir.resolve(strict=False)
    state_dir.mkdir(parents=True, exist_ok=True)
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise SystemExit("state-dir must be an ordinary directory")
    args.stop_file = args.stop_file.resolve(strict=False)
    try:
        args.stop_file.relative_to(state_dir)
    except ValueError:
        raise SystemExit("stop-file must remain inside state-dir") from None
    checkpoint = state_dir / "checkpoint.json"
    stopped = Event()
    previous = {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda *_: stopped.set())
    try:
        with ResourceLock(state_dir / "depth-tape.lock"):
            args.stop_file.unlink(missing_ok=True)
            store = WhiteBitDepthTapeStore(
                state_dir / "depth-tape.sqlite3",
                bucket_seconds=args.bucket_seconds,
                retention_days=args.retention_days,
            )
            try:
                asyncio.run(_collect(args, stopped, store, checkpoint, markets))
            finally:
                store.close()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

