#!/usr/bin/env python3
"""Collect restart-safe public WhiteBIT L2 features for selective research."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
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
    DepthBookObservation,
    WhiteBitDepthWebSocketTransport,
)
from research.autotrade_research.integrations.whitebit_public import _symbol
from research.autotrade_research.learning.whitebit_depth_tape import (
    ARCHIVE_SETTLE_SECONDS, ARCHIVE_WINDOW_SECONDS, WhiteBitDepthTapeStore,
)


TIMING_WINDOW_SECONDS = 300
INGEST_BATCH_SIZE = 32
INGEST_BATCH_SECONDS = 0.05
INGEST_QUEUE_LIMIT = 512


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


async def _collect(args, stopped: Event, store: WhiteBitDepthTapeStore | None,
                   checkpoint: Path, markets: tuple[str, ...], *,
                   offload_sqlite: bool = False) -> None:
    stop = asyncio.Event()
    phases = {market: "STARTING" for market in markets}
    errors: dict[str, str] = {}
    transport_events = {
        market: {"DEPTH_UPDATE_TIMEOUT": 0, "CONNECTION_LOST": 0, "SCHEMA_REJECTED": 0}
        for market in markets
    }
    rejection_counts: dict[str, dict[str, int]] = {market: {} for market in markets}
    added = 0
    last_archive_boundary = None
    timing_samples: deque[tuple[float, dict[str, float], dict[str, float], dict[str, float]]] = deque()
    last_observation: dict[str, tuple[float, int]] = {}
    observation_gaps: dict[str, float] = {}
    message_lags: dict[str, float] = {}
    pending_observations: deque[tuple[float, DepthBookObservation]] = deque()
    queue_space = asyncio.Event()
    queue_space.set()
    executor = ThreadPoolExecutor(max_workers=1) if offload_sqlite else None
    write_task: asyncio.Task | None = None
    sqlite_barrier = False
    last_persisted_received_at: dict[str, datetime] = {}
    last_commit_at: datetime | None = None
    inflight_received_at: float | None = None
    peak_pending_observations = 0
    flush_timer: asyncio.TimerHandle | None = None
    flush_error: Exception | None = None
    max_ingest = 0.0
    checkpoint_write = 0.0
    checkpoint_retries = 0
    expected_wakeup = monotonic()
    loop = asyncio.get_running_loop()

    async def sqlite_call(operation, *args, **kwargs):
        if executor is None:
            return operation(*args, **kwargs)
        call = loop.run_in_executor(executor, lambda: operation(*args, **kwargs))
        try:
            return await asyncio.shield(call)
        except asyncio.CancelledError:
            while not call.done():
                try:
                    await asyncio.shield(call)
                except asyncio.CancelledError:
                    pass
                except Exception:
                    break
            raise

    owns_store = store is None
    if owns_store:
        opening = asyncio.create_task(sqlite_call(
            WhiteBitDepthTapeStore,
            checkpoint.parent / "depth-tape.sqlite3",
            bucket_seconds=args.bucket_seconds,
            retention_days=args.retention_days,
        ))
        try:
            store = await asyncio.shield(opening)
        except asyncio.CancelledError:
            try:
                while not opening.done():
                    try:
                        await asyncio.shield(opening)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        break
                if opening.exception() is None:
                    await sqlite_call(opening.result().close)
            finally:
                if executor is not None:
                    executor.shutdown(wait=True, cancel_futures=False)
            raise
        except BaseException:
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=False)
            raise

    async def write_batch(batch: list[DepthBookObservation]) -> None:
        nonlocal added, max_ingest, flush_error, last_commit_at, inflight_received_at
        started = monotonic()
        try:
            await sqlite_call(store.ingest_many, batch)
        except Exception as exc:
            flush_error = exc
            stop.set()
            queue_space.set()
            return
        finally:
            inflight_received_at = None
        max_ingest = max(max_ingest, monotonic() - started)
        added += len(batch)
        last_commit_at = datetime.now(timezone.utc)
        for observation in batch:
            last_persisted_received_at[observation.market] = observation.received_at
        if pending_observations and not sqlite_barrier:
            loop.call_soon(flush_observations)

    def next_batch(maximum: int) -> list[DepthBookObservation]:
        nonlocal inflight_received_at
        inflight_received_at = pending_observations[0][0]
        batch = [pending_observations.popleft()[1]
                 for _ in range(min(maximum, len(pending_observations)))]
        queue_space.set()
        return batch

    def flush_observations(*, at_barrier: bool = False, maximum: int | None = None) -> None:
        nonlocal added, max_ingest, flush_timer, flush_error, write_task
        if flush_timer is not None:
            flush_timer.cancel()
            flush_timer = None
        if not pending_observations or flush_error is not None:
            return
        if executor is not None:
            if sqlite_barrier and not at_barrier:
                return
            if write_task is None or write_task.done():
                write_task = asyncio.create_task(write_batch(next_batch(
                    INGEST_BATCH_SIZE if maximum is None else maximum,
                )))
            return
        started = monotonic()
        try:
            store.ingest_many([observation for _, observation in pending_observations])
        except Exception as exc:
            # Timer callbacks run outside the collector task. Propagate their
            # failure through the next heartbeat without retrying this batch.
            flush_error = exc
            stop.set()
            return
        max_ingest = max(max_ingest, monotonic() - started)
        added += len(pending_observations)
        pending_observations.clear()

    async def drain_observations() -> None:
        # Freeze the work already received at this boundary. New socket frames
        # can queue while the SQLite thread commits this finite prefix.
        if executor is None:
            flush_observations()
        else:
            remaining = len(pending_observations)
            if write_task is not None:
                await asyncio.shield(write_task)
            while remaining > 0 and flush_error is None:
                count = min(INGEST_BATCH_SIZE, remaining)
                flush_observations(at_barrier=True, maximum=count)
                if write_task is not None:
                    await asyncio.shield(write_task)
                remaining -= count
        if flush_error is not None:
            raise flush_error

    async def publish(phase: str, *, wakeup_delay: float, archive_time: float) -> None:
        nonlocal added, max_ingest, checkpoint_write, checkpoint_retries, sqlite_barrier
        nonlocal peak_pending_observations
        sqlite_barrier = True
        try:
            await drain_observations()
            status_start = monotonic()
            status = await sqlite_call(store.status)
        finally:
            sqlite_barrier = False
            flush_observations()
        measured_at = monotonic()
        timing_samples.append((measured_at, {
            "wakeup_delay": wakeup_delay,
            "sqlite_ingest": max_ingest,
            "sqlite_archive": archive_time,
            "sqlite_status": measured_at - status_start,
            "previous_checkpoint_write": checkpoint_write,
        }, observation_gaps.copy(), message_lags.copy()))
        max_ingest = 0.0
        observation_gaps.clear()
        message_lags.clear()
        while measured_at - timing_samples[0][0] > TIMING_WINDOW_SECONDS:
            timing_samples.popleft()
        maxima: dict[str, float] = {}
        max_gaps: dict[str, float] = {}
        max_message_lags: dict[str, float] = {}
        for _, durations, gaps, lags in timing_samples:
            for name, seconds in durations.items():
                maxima[name] = max(maxima.get(name, 0), seconds)
            for market, seconds in gaps.items():
                max_gaps[market] = max(max_gaps.get(market, 0), seconds)
            for market, seconds in lags.items():
                max_message_lags[market] = max(max_message_lags.get(market, seconds), seconds)
        timing = {
            "recent_window_seconds": TIMING_WINDOW_SECONDS,
            "last_wakeup_delay_ms": round(wakeup_delay * 1000, 3),
            "recent_max_ms": {name: round(value * 1000, 3) for name, value in maxima.items()},
            "checkpoint_retries_since_start": checkpoint_retries,
            **({"sqlite_writer_pending_observations": len(pending_observations),
                "sqlite_writer_queue_limit": INGEST_QUEUE_LIMIT,
                "sqlite_writer_peak_pending_observations": peak_pending_observations,
                "sqlite_writer_oldest_pending_ms": (
                    round((measured_at - inflight_received_at) * 1000, 3)
                    if inflight_received_at is not None else
                    round((measured_at - pending_observations[0][0]) * 1000, 3)
                    if pending_observations else None
                ),
                "last_sqlite_commit_at": last_commit_at.isoformat() if last_commit_at else None,
                "last_persisted_received_at": {
                    market: at.isoformat() for market, at in last_persisted_received_at.items()
                }} if executor is not None else {}),
            "transport_events_since_start": {
                market: counts.copy() for market, counts in transport_events.items()
            },
            "schema_rejections_by_reason_since_start": {
                market: counts.copy() for market, counts in rejection_counts.items()
            },
            "markets": {
                market: {
                    "observation_age_ms": round((measured_at - at) * 1000, 3),
                    "recent_max_observation_gap_ms": (
                        round(max_gaps[market] * 1000, 3) if market in max_gaps else None
                    ),
                    "generation": generation,
                    "recent_max_raw_message_lag_ms": (
                        round(max_message_lags[market] * 1000, 3)
                        if market in max_message_lags else None
                    ),
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
        if value.code == "SCHEMA_REJECTED" and value.rejection_reason is not None:
            reasons = rejection_counts[market]
            reasons[value.rejection_reason] = reasons.get(value.rejection_reason, 0) + 1
        if value.code == "SUBSCRIBED":
            phases[market] = "RUNNING"
            errors.pop(market, None)
        elif value.code == "STOPPED":
            phases[market] = "STOPPED"
        else:
            phases[market] = "DEGRADED"
            errors[market] = value.code

    async def consume(market: str) -> None:
        nonlocal flush_timer, peak_pending_observations
        transport = WhiteBitDepthWebSocketTransport(
            market=market, limit=args.depth_limit,
            diagnostic=lambda value: diagnostic(market, value),
        )
        async for observation in transport.observations(stop):
            received = monotonic()
            while executor is not None and len(pending_observations) >= INGEST_QUEUE_LIMIT:
                queue_space.clear()
                await queue_space.wait()
                if flush_error is not None:
                    break
            if flush_error is not None:
                break
            if market in last_observation:
                gap = received - last_observation[market][0]
                observation_gaps[market] = max(observation_gaps.get(market, 0), gap)
            last_observation[market] = (received, observation.generation)
            if observation.message_time is not None:
                # Diagnostic only. This difference includes host clock offset.
                lag = (observation.received_at - observation.message_time).total_seconds()
                message_lags[market] = max(message_lags.get(market, lag), lag)
            pending_observations.append((received, observation))
            peak_pending_observations = max(peak_pending_observations, len(pending_observations))
            if len(pending_observations) >= INGEST_BATCH_SIZE:
                flush_observations()
            elif flush_timer is None:
                flush_timer = loop.call_later(INGEST_BATCH_SECONDS, flush_observations)

    consumers = [asyncio.create_task(consume(market)) for market in markets]
    try:
        while not stopped.is_set() and not args.stop_file.exists():
            if flush_error is not None:
                raise flush_error
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
                sqlite_barrier = True
                try:
                    await drain_observations()
                    archive_start = monotonic()
                    await sqlite_call(store.archive_completed_windows, markets, now_epoch=now_epoch)
                    archive_time = monotonic() - archive_start
                    last_archive_boundary = archive_boundary
                finally:
                    sqlite_barrier = False
                    flush_observations()
            await publish(phase, wakeup_delay=wakeup_delay, archive_time=archive_time)
            if stopped.is_set() or args.stop_file.exists():
                break
            expected_wakeup = monotonic() + args.poll_seconds
            await asyncio.sleep(args.poll_seconds)
    finally:
        stop.set()
        try:
            if executor is not None:
                # Let producers publish the frame already received while
                # they were waiting for bounded writer capacity.
                while any(not task.done() for task in consumers) and pending_observations:
                    await drain_observations()
                    await asyncio.sleep(0)
            _, pending = await asyncio.wait(consumers, timeout=10)
            for task in pending:
                task.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
            await publish("STOPPED", wakeup_delay=0, archive_time=0)
        finally:
            for task in consumers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
            try:
                if write_task is not None and not write_task.done():
                    await asyncio.shield(write_task)
                if owns_store:
                    await sqlite_call(store.close)
            finally:
                if executor is not None:
                    executor.shutdown(wait=True, cancel_futures=False)


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
            asyncio.run(_collect(args, stopped, None, checkpoint, markets, offload_sqlite=True))
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
