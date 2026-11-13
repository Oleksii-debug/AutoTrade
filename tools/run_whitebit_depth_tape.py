#!/usr/bin/env python3
"""Collect restart-safe public WhiteBIT L2 features for selective research."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import signal
from threading import Event
import sys
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
for dependency_root in (ROOT, ROOT / "research"):
    if str(dependency_root) not in sys.path:
        sys.path.insert(0, str(dependency_root))

from research.autotrade_research.artifacts.checkpoint_retry import publish_checkpoint
from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.artifacts.resource_lock import ResourceLock
from research.autotrade_research.integrations.whitebit_depth import (
    WhiteBitDepthWebSocketTransport,
)
from research.autotrade_research.integrations.whitebit_public import _symbol
from research.autotrade_research.learning.whitebit_depth_tape import (
    ARCHIVE_SETTLE_SECONDS, ARCHIVE_WINDOW_SECONDS, WhiteBitDepthTapeStore,
)


TIMING_WINDOW_SECONDS = 300


async def _checkpoint(path: Path, status: dict, *, phase: str,
                      markets: tuple[str, ...], observations: int,
                      errors: dict[str, str], timing: dict) -> int:
    payload = {
        **status, "phase": phase,
        "transport": "WHITEBIT_PUBLIC_DEPTH_V1", "markets": list(markets),
        "new_observations_last_checkpoint": observations, "errors": errors.copy(),
        "timing": timing,
        "checkpointed_at": datetime.now(timezone.utc).isoformat(),
        "secrets_in_state": False, "paid_model_calls": 0,
    }
    return await publish_checkpoint(path, payload, atomic_write_json, offload_write=True)


async def _collect(args, stopped: Event, store: WhiteBitDepthTapeStore,
                   checkpoint: Path, markets: tuple[str, ...]) -> None:
    stop = asyncio.Event()
    phases = {market: "STARTING" for market in markets}
    errors: dict[str, str] = {}
    transport_events = {
        market: {"DEPTH_UPDATE_TIMEOUT": 0, "CONNECTION_LOST": 0}
        for market in markets
    }
    added = 0
    last_archive_boundary = None
    timing_samples: deque[tuple[float, dict[str, float], dict[str, float]]] = deque()
    last_observation: dict[str, tuple[float, int]] = {}
    observation_gaps: dict[str, float] = {}
    max_ingest = 0.0
    checkpoint_write = 0.0
    checkpoint_retries = 0
    expected_wakeup = monotonic()

    async def publish(phase: str, *, wakeup_delay: float, archive_time: float) -> None:
        nonlocal added, max_ingest, checkpoint_write, checkpoint_retries
        status_start = monotonic()
        status = store.status()
        measured_at = monotonic()
        timing_samples.append((measured_at, {
            "wakeup_delay": wakeup_delay,
            "sqlite_ingest": max_ingest,
            "sqlite_archive": archive_time,
            "sqlite_status": measured_at - status_start,
            "previous_checkpoint_write": checkpoint_write,
        }, observation_gaps.copy()))
        max_ingest = 0.0
        observation_gaps.clear()
        while measured_at - timing_samples[0][0] > TIMING_WINDOW_SECONDS:
            timing_samples.popleft()
        maxima: dict[str, float] = {}
        max_gaps: dict[str, float] = {}
        for _, durations, gaps in timing_samples:
            for name, seconds in durations.items():
                maxima[name] = max(maxima.get(name, 0), seconds)
            for market, seconds in gaps.items():
                max_gaps[market] = max(max_gaps.get(market, 0), seconds)
        timing = {
            "recent_window_seconds": TIMING_WINDOW_SECONDS,
            "last_wakeup_delay_ms": round(wakeup_delay * 1000, 3),
            "recent_max_ms": {name: round(value * 1000, 3) for name, value in maxima.items()},
            "checkpoint_retries_since_start": checkpoint_retries,
            "transport_events_since_start": {
                market: counts.copy() for market, counts in transport_events.items()
            },
            "markets": {
                market: {
                    "observation_age_ms": round((measured_at - at) * 1000, 3),
                    "recent_max_observation_gap_ms": (
                        round(max_gaps[market] * 1000, 3) if market in max_gaps else None
                    ),
                    "generation": generation,
                }
                for market, (at, generation) in last_observation.items()
            },
        }
        write_start = monotonic()
        observations = added
        observed_errors = errors.copy()

        async def write() -> None:
            nonlocal added, checkpoint_write, checkpoint_retries
            checkpoint_retries += await _checkpoint(
                checkpoint, status, phase=phase, markets=markets,
                observations=observations, errors=observed_errors, timing=timing,
            )
            added -= observations
            checkpoint_write = monotonic() - write_start

        publication = asyncio.create_task(write())
        try:
            await asyncio.shield(publication)
        except asyncio.CancelledError:
            # A started thread cannot be cancelled. Join this publication
            # before any final checkpoint, including on repeated cancellation.
            completion = asyncio.gather(publication, return_exceptions=True)
            while not completion.done():
                try:
                    await asyncio.shield(completion)
                except asyncio.CancelledError:
                    pass
            raise

    def diagnostic(market: str, value) -> None:
        counts = transport_events[market]
        if value.code in counts:
            counts[value.code] += 1
        if value.code == "SUBSCRIBED":
            phases[market] = "RUNNING"
            errors.pop(market, None)
        elif value.code == "STOPPED":
            phases[market] = "STOPPED"
        else:
            phases[market] = "DEGRADED"
            errors[market] = value.code

    async def consume(market: str) -> None:
        nonlocal added, max_ingest
        transport = WhiteBitDepthWebSocketTransport(
            market=market, limit=args.depth_limit,
            diagnostic=lambda value: diagnostic(market, value),
        )
        async for observation in transport.observations(stop):
            received = monotonic()
            if market in last_observation:
                gap = received - last_observation[market][0]
                observation_gaps[market] = max(observation_gaps.get(market, 0), gap)
            last_observation[market] = (received, observation.generation)
            store.ingest(observation)
            max_ingest = max(max_ingest, monotonic() - received)
            added += 1

    consumers = [asyncio.create_task(consume(market)) for market in markets]
    try:
        while not stopped.is_set() and not args.stop_file.exists():
            wakeup_delay = max(0, monotonic() - expected_wakeup)
            if all(value == "RUNNING" for value in phases.values()):
                phase = "RUNNING"
            elif any(value == "RUNNING" for value in phases.values()):
                phase = "DEGRADED"
            else:
                phase = "STARTING" if not errors else "DEGRADED"
            now_epoch = int(datetime.now(timezone.utc).timestamp())
            archive_boundary = (now_epoch - ARCHIVE_SETTLE_SECONDS) // ARCHIVE_WINDOW_SECONDS
            archive_time = 0.0
            if archive_boundary != last_archive_boundary:
                archive_start = monotonic()
                store.archive_completed_windows(markets, now_epoch=now_epoch)
                archive_time = monotonic() - archive_start
                last_archive_boundary = archive_boundary
            await publish(phase, wakeup_delay=wakeup_delay, archive_time=archive_time)
            if stopped.is_set() or args.stop_file.exists():
                break
            expected_wakeup = monotonic() + args.poll_seconds
            await asyncio.sleep(args.poll_seconds)
    finally:
        stop.set()
        _, pending = await asyncio.wait(consumers, timeout=10)
        for task in pending:
            task.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)
        await publish("STOPPED", wakeup_delay=0, archive_time=0)


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
