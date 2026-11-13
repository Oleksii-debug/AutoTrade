#!/usr/bin/env python3
"""Collect bounded public WhiteBIT trade flow over one shared WebSocket."""
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
from research.autotrade_research.integrations.whitebit_public import _symbol
from research.autotrade_research.integrations.whitebit_trade_websocket import (
    WhiteBitPublicTradeWebSocketTransport,
)
from research.autotrade_research.learning.whitebit_trade_tape import (
    OPT_IN_EXECUTION_PRINT_MARKETS,
    WhiteBitTradeTapeStore,
)


def _checkpoint(path: Path, store: WhiteBitTradeTapeStore, *, phase: str,
                markets: tuple[str, ...], new_trades: int,
                errors: dict[str, str], exact_print_coverage: dict[str, dict]) -> None:
    atomic_write_json(path, {
        **store.status(),
        "phase": phase,
        "transport": "WHITEBIT_PUBLIC_WEBSOCKET_TRADES_V1",
        "markets": list(markets),
        "new_trades_last_checkpoint": new_trades,
        "errors": errors,
        "exact_print_coverage": exact_print_coverage,
        "checkpointed_at": datetime.now(timezone.utc).isoformat(),
        "secrets_in_state": False,
        "paid_model_calls": 0,
    })


async def _collect(args, stopped: Event, store: WhiteBitTradeTapeStore,
                   checkpoint: Path, markets: tuple[str, ...]) -> None:
    stop = asyncio.Event()
    phase = "STARTING"
    errors: dict[str, str] = {}
    added = 0
    exact_markets = store.execution_print_markets.intersection(markets)
    pending_snapshot: set[str] = set()
    coverage = {
        market: {"state": "AWAITING_FIRST_BATCH", "unverified_boundaries_since_start": 0}
        for market in sorted(exact_markets)
    }

    def diagnostic(code: str) -> None:
        nonlocal phase
        if code == "SUBSCRIBED":
            phase = "RUNNING"
            errors.clear()
            pending_snapshot.update(exact_markets)
            for row in coverage.values():
                row["state"] = "AWAITING_FIRST_BATCH"
        else:
            phase = "DEGRADED"
            errors["websocket"] = code
            for row in coverage.values():
                row["state"] = "DISCONNECTED"

    transport = WhiteBitPublicTradeWebSocketTransport(
        markets=markets, diagnostic=diagnostic,
    )

    async def consume() -> None:
        nonlocal added
        async for batch in transport.batches(stop):
            if batch.market not in pending_snapshot:
                added += store.ingest(batch)
                continue
            state, prior_receipt = store.execution_snapshot_coverage(batch)
            added += store.ingest(batch)
            row = coverage[batch.market]
            row["state"] = state
            if state != "CURSOR_OVERLAP":
                row["unverified_boundaries_since_start"] += 1
                row["last_unverified_boundary"] = {
                    "prior_receipt_utc": prior_receipt,
                    "first_batch_receipt_utc": batch.received_at.isoformat(),
                }
            pending_snapshot.remove(batch.market)

    consumer = asyncio.create_task(consume())
    try:
        while not stopped.is_set() and not args.stop_file.exists():
            if consumer.done():
                break
            _checkpoint(checkpoint, store, phase=phase, markets=markets,
                        new_trades=added, errors=errors, exact_print_coverage=coverage)
            added = 0
            await asyncio.sleep(args.poll_seconds)
    finally:
        stop.set()
        _, pending = await asyncio.wait((consumer,), timeout=10)
        if pending:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        failed = not consumer.cancelled() and consumer.exception() is not None
        if failed:
            errors["collector"] = "TRADE_CONSUMER_FAILED"
        _checkpoint(checkpoint, store, phase="FAILED" if failed else "STOPPED", markets=markets,
                    new_trades=added, errors=errors, exact_print_coverage=coverage)
        if failed:
            await consumer


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--market", action="append", required=True)
    parser.add_argument("--poll-seconds", type=float, default=1.0,
                        help="durable checkpoint cadence")
    parser.add_argument("--bucket-seconds", type=int, default=5)
    parser.add_argument("--retention-days", type=int, default=14)
    parser.add_argument(
        "--exact-print-market", action="append", choices=sorted(OPT_IN_EXECUTION_PRINT_MARKETS),
        default=[], help="add exact execution prints for an already subscribed futures market",
    )
    parser.add_argument("--stop-file", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= len(args.market) <= 100:
        raise SystemExit("one to one hundred markets are required")
    markets = tuple(_symbol(value) for value in args.market)
    if len(set(markets)) != len(markets):
        raise SystemExit("markets must be unique")
    extra_execution_print_markets = tuple(args.exact_print_market)
    if (
        len(set(extra_execution_print_markets)) != len(extra_execution_print_markets)
        or not set(extra_execution_print_markets).issubset(markets)
    ):
        raise SystemExit("exact print markets must be unique and already subscribed")
    if not 1.0 <= args.poll_seconds <= 60.0:
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
        with ResourceLock(state_dir / "trade-tape.lock"):
            args.stop_file.unlink(missing_ok=True)
            store = WhiteBitTradeTapeStore(
                state_dir / "trade-tape.sqlite3",
                bucket_seconds=args.bucket_seconds,
                retention_days=args.retention_days,
                extra_execution_print_markets=extra_execution_print_markets,
            )
            _checkpoint(checkpoint, store, phase="STARTING", markets=markets,
                        new_trades=0, errors={}, exact_print_coverage={})
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
