#!/usr/bin/env python3
"""Watch executable WhiteBIT stable/fiat triangles over public WebSockets."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
for dependency_root in (ROOT, ROOT / "research"):
    if str(dependency_root) not in sys.path:
        sys.path.insert(0, str(dependency_root))

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.artifacts.resource_lock import ResourceLock
from research.autotrade_research.integrations.whitebit_public import WhiteBitPublicClient
from research.autotrade_research.integrations.whitebit_websocket import (
    WhiteBitPublicWebSocketTransport,
)
from research.autotrade_research.strategies.triangular_shadow import (
    BookTop,
    SCHEMA,
    evaluate_cycle,
)


MARKETS = (
    "USDC_USDT", "USDT_TRY", "USDC_TRY", "USDT_EUR", "USDC_EUR",
    "USDT_USD", "USDC_USD", "USDT_GBP", "USDC_GBP",
)
PATHS = tuple(
    path
    for fiat in ("TRY", "EUR", "USD", "GBP")
    for path in (("USDT", "USDC", fiat, "USDT"),
                 ("USDT", fiat, "USDC", "USDT"))
)


def _append_bounded(path: Path, document: dict, *, maximum_bytes: int = 4 << 20) -> None:
    body = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if path.exists() and path.stat().st_size + len(body) > maximum_bytes:
        archive = path.with_name(path.name + ".1")
        archive.unlink(missing_ok=True)
        os.replace(path, archive)
    with path.open("ab") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())


async def _run(args) -> int:
    state_dir = args.state_dir.resolve(strict=False)
    state_dir.mkdir(parents=True, exist_ok=True)
    stop_file = args.stop_file.resolve(strict=False)
    try:
        stop_file.relative_to(state_dir)
    except ValueError:
        raise ValueError("stop file must remain inside state directory") from None
    stop_file.unlink(missing_ok=True)
    instruments = {
        item.market: item for item in WhiteBitPublicClient().instruments()
        if item.instrument_type == "spot" and item.market in MARKETS
    }
    if set(instruments) != set(MARKETS):
        raise RuntimeError("required stable/fiat markets are unavailable")
    fees = {market: instruments[market].taker_fee_rate for market in MARKETS}
    latest: dict[str, BookTop] = {}
    results: list[dict] = []
    opportunity_count = 0
    last_opportunity_key = None
    checkpoint_path = state_dir / "checkpoint.json"
    if checkpoint_path.is_file() and not checkpoint_path.is_symlink():
        prior = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if (type(prior) is not dict
                or prior.get("schema") != "autotrade-whitebit-triangular-shadow-supervisor-v1"
                or prior.get("network_orders") is not False
                or prior.get("trading_authority") is not False):
            raise ValueError("triangular-shadow checkpoint is invalid")
        opportunity_count = int(prior.get("opportunity_count", 0))
        last_opportunity_key = prior.get("last_opportunity_key_sha256")
    last_publish = 0.0
    stop = asyncio.Event()

    async def publish(*, force: bool = False, phase: str = "RUNNING") -> None:
        nonlocal last_publish
        now_mono = time.monotonic()
        if not force and now_mono - last_publish < 1:
            return
        last_publish = now_mono
        best = max(
            (row for row in results if row.get("status") != "INSUFFICIENT_TOP_DEPTH"),
            key=lambda row: float(row["net_after_reserve_bps"]), default=None,
        )
        atomic_write_json(checkpoint_path, {
            "schema": "autotrade-whitebit-triangular-shadow-supervisor-v1",
            "phase": phase,
            "checkpointed_at": datetime.now(timezone.utc).isoformat(),
            "markets": list(MARKETS), "paths": [list(path) for path in PATHS],
            "latest_market_count": len(latest),
            "cycle_result_count": len(results),
            "opportunity_count": opportunity_count,
            "last_opportunity_key_sha256": last_opportunity_key,
            "best_result": None if best is None else {
                "path": best["path"],
                "net_after_taker_fees_bps": best["net_after_taker_fees_bps"],
                "net_after_reserve_bps": best["net_after_reserve_bps"],
                "qualified_for_shadow_observation": best["qualified_for_shadow_observation"],
                "result_sha256": best["result_sha256"],
            },
            "impact_reserve_bps": args.impact_reserve_bps,
            "notional": args.notional,
            "network_orders": False, "trading_authority": False,
            "paid_model_calls": 0, "profit_guaranteed": False,
        })

    def evaluate() -> None:
        nonlocal results, opportunity_count, last_opportunity_key
        if set(latest) != set(MARKETS):
            return
        now = datetime.now(timezone.utc)
        results = [evaluate_cycle(
            path, books=latest, taker_fees=fees, notional=args.notional,
            impact_reserve_bps=args.impact_reserve_bps, now=now,
            maximum_age_seconds=args.maximum_age_seconds,
            maximum_skew_seconds=args.maximum_skew_seconds,
        ) for path in PATHS]
        for result in results:
            if (result["qualified_for_shadow_observation"]
                    and result["opportunity_key_sha256"] != last_opportunity_key):
                _append_bounded(state_dir / "qualified-opportunities.jsonl", result)
                opportunity_count += 1
                last_opportunity_key = result["opportunity_key_sha256"]

    async def collect(market: str) -> None:
        transport = WhiteBitPublicWebSocketTransport(market=market)
        try:
            async for observation in transport.observations(stop):
                ticker = observation.ticker
                latest[market] = BookTop(
                    market=market, bid_price=ticker.bid_price,
                    bid_quantity=ticker.bid_quantity, ask_price=ticker.ask_price,
                    ask_quantity=ticker.ask_quantity, received_at=ticker.received_at,
                    digest=ticker.digest,
                )
                evaluate()
                await publish()
        except Exception:
            stop.set()
            raise

    async def watch_stop() -> None:
        while not stop.is_set():
            if stop_file.exists():
                stop.set()
                return
            await asyncio.sleep(0.25)

    async def heartbeat() -> None:
        while not stop.is_set():
            await publish(force=True)
            await asyncio.sleep(1)

    loop = asyncio.get_running_loop()
    previous = {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
    try:
        await publish(force=True, phase="STARTING")
        tasks = [asyncio.create_task(collect(market)) for market in MARKETS]
        tasks += [asyncio.create_task(watch_stop()), asyncio.create_task(heartbeat())]
        await stop.wait()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                raise outcome
        await publish(force=True, phase="STOPPED")
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--notional", default="100")
    parser.add_argument("--impact-reserve-bps", default="2")
    parser.add_argument("--maximum-age-seconds", type=int, default=3)
    parser.add_argument("--maximum-skew-seconds", type=int, default=2)
    args = parser.parse_args(argv)
    state_dir = args.state_dir.resolve(strict=False)
    state_dir.mkdir(parents=True, exist_ok=True)
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise SystemExit("state-dir must be an ordinary directory")
    with ResourceLock(state_dir / "triangular-shadow.lock"):
        return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
