#!/usr/bin/env python3
"""Collect bounded public WhiteBIT trade flow for local microstructure research."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import signal
from pathlib import Path
from threading import Event
import sys

ROOT = Path(__file__).resolve().parents[1]
for dependency_root in (ROOT, ROOT / "research"):
    if str(dependency_root) not in sys.path:
        sys.path.insert(0, str(dependency_root))

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.artifacts.resource_lock import ResourceLock
from research.autotrade_research.integrations.whitebit_public import (
    PublicDataError,
    WhiteBitPublicClient,
    _symbol,
)
from research.autotrade_research.learning.whitebit_trade_tape import (
    SCHEMA,
    WhiteBitTradeTapeStore,
)


def _checkpoint(path: Path, store: WhiteBitTradeTapeStore, *, phase: str,
                markets: tuple[str, ...], new_trades: int,
                errors: dict[str, str]) -> None:
    status = store.status()
    atomic_write_json(path, {
        **status,
        "phase": phase,
        "markets": list(markets),
        "new_trades_last_poll": new_trades,
        "errors": errors,
        "checkpointed_at": datetime.now(timezone.utc).isoformat(),
        "secrets_in_state": False,
        "paid_model_calls": 0,
    })


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--market", action="append", required=True)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--bucket-seconds", type=int, default=5)
    parser.add_argument("--retention-days", type=int, default=14)
    parser.add_argument("--stop-file", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= len(args.market) <= 16:
        raise SystemExit("one to sixteen markets are required")
    markets = tuple(_symbol(value) for value in args.market)
    if len(set(markets)) != len(markets):
        raise SystemExit("markets must be unique")
    if not 1.0 <= args.poll_seconds <= 60.0:
        raise SystemExit("poll-seconds must be 1..60")
    state_dir = args.state_dir.resolve(strict=False)
    state_dir.mkdir(parents=True, exist_ok=True)
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise SystemExit("state-dir must be an ordinary directory")
    stop_file = args.stop_file.resolve(strict=False)
    try:
        stop_file.relative_to(state_dir)
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
            stop_file.unlink(missing_ok=True)
            store = WhiteBitTradeTapeStore(
                state_dir / "trade-tape.sqlite3",
                bucket_seconds=args.bucket_seconds,
                retention_days=args.retention_days,
            )
            clients = {market: WhiteBitPublicClient() for market in markets}
            _checkpoint(checkpoint, store, phase="STARTING", markets=markets,
                        new_trades=0, errors={})
            try:
                while not stopped.is_set() and not stop_file.exists():
                    batches = {}
                    errors = {}
                    with ThreadPoolExecutor(max_workers=len(markets)) as executor:
                        futures = {
                            executor.submit(clients[market].recent_trades, market): market
                            for market in markets
                        }
                        for future in as_completed(futures):
                            market = futures[future]
                            try:
                                batches[market] = future.result()
                            except PublicDataError as error:
                                errors[market] = str(error)
                            except Exception:
                                errors[market] = "UNEXPECTED_PUBLIC_READER_FAILURE"
                    added = sum(store.ingest(batches[market]) for market in markets
                                if market in batches)
                    _checkpoint(checkpoint, store,
                                phase="RUNNING" if batches else "DEGRADED",
                                markets=markets, new_trades=added, errors=errors)
                    stopped.wait(args.poll_seconds)
            finally:
                _checkpoint(checkpoint, store, phase="STOPPED", markets=markets,
                            new_trades=0, errors={})
                store.close()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

