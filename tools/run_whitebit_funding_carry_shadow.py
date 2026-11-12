#!/usr/bin/env python3
"""Costed WhiteBIT spot/perpetual funding-carry shadow screen.

The worker uses public data only.  It cannot submit orders, access an account,
or enable leverage.  Its purpose is to retain causal evidence for a potential
delta-neutral carry strategy and to reject candidates that do not cover four
fees, executable book spreads and an explicit adverse basis reserve.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN, localcontext
import json
from pathlib import Path
import signal
import sys
from threading import Event
import time

ROOT = Path(__file__).resolve().parents[1]
for dependency_root in (ROOT, ROOT / "research", ROOT / "mvp"):
    if str(dependency_root) not in sys.path:
        sys.path.insert(0, str(dependency_root))

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.integrations.whitebit_public import (
    PublicBook,
    PublicDataError,
    WhiteBitPublicClient,
)


SCHEMA = "autotrade-whitebit-funding-carry-shadow-v1"


def _text(value: Decimal) -> str:
    return format(value, "f")


def _vwap(book: PublicBook, side: str, quantity: Decimal) -> Decimal:
    if side not in ("BUY", "SELL") or quantity <= 0:
        raise ValueError("invalid executable VWAP request")
    levels = book.asks if side == "BUY" else book.bids
    remaining = quantity
    cash = Decimal(0)
    for price_text, size_text in levels:
        size = min(remaining, Decimal(size_text))
        cash += size * Decimal(price_text)
        remaining -= size
        if remaining == 0:
            return cash / quantity
    raise ValueError("INSUFFICIENT_PUBLIC_BOOK_DEPTH")


def evaluate_pair(*, future, perpetual, spot, history, spot_book: PublicBook,
                  future_book: PublicBook, notional: Decimal,
                  horizon_days: int, basis_stress_bps: Decimal) -> dict:
    if future.base_asset != spot.base_asset or future.quote_asset != spot.quote_asset:
        raise ValueError("spot/perpetual asset mismatch")
    step = max(Decimal(spot.step_size), Decimal(perpetual.step_size))
    reference = max(Decimal(spot_book.asks[0][0]), Decimal(future_book.bids[0][0]))
    quantity = (notional / reference / step).to_integral_value(rounding=ROUND_DOWN) * step
    if quantity < max(Decimal(spot.minimum_quantity), Decimal(perpetual.minimum_quantity)):
        raise ValueError("BELOW_MINIMUM_QUANTITY")

    spot_buy = _vwap(spot_book, "BUY", quantity)
    perp_sell = _vwap(future_book, "SELL", quantity)
    spot_sell = _vwap(spot_book, "SELL", quantity)
    perp_buy = _vwap(future_book, "BUY", quantity)
    spot_entry_notional = quantity * spot_buy
    perp_entry_notional = quantity * perp_sell
    if (spot_entry_notional < Decimal(spot.minimum_notional)
            or perp_entry_notional < Decimal(perpetual.minimum_notional)):
        raise ValueError("BELOW_MINIMUM_NOTIONAL")

    # Same-snapshot round trip isolates the presently executable spread/depth
    # cost; no fill or future exit price is invented.
    trading_cash = quantity * (perp_sell - spot_buy + spot_sell - perp_buy)
    fees = (
        spot_entry_notional * Decimal(spot.taker_fee_rate)
        + quantity * spot_sell * Decimal(spot.taker_fee_rate)
        + perp_entry_notional * Decimal(perpetual.taker_fee_rate)
        + quantity * perp_buy * Decimal(perpetual.taker_fee_rate)
    )
    executable_cost = -trading_cash + fees
    rates = [Decimal(row.funding_rate) for row in history.records]
    ordered = sorted(rates)
    p25 = ordered[max(0, (len(ordered) - 1) // 4)]
    mean = sum(rates) / Decimal(len(rates))
    positive_fraction = Decimal(sum(rate > 0 for rate in rates)) / Decimal(len(rates))
    current_rate = Decimal(future.funding_rate)
    conservative_rate = min(current_rate, mean, p25)
    intervals = Decimal((horizon_days * 1440) // future.funding_interval_minutes)
    projected_funding = max(Decimal(0), conservative_rate) * perp_entry_notional * intervals
    basis_reserve = perp_entry_notional * basis_stress_bps / Decimal(10000)
    projected_after_cost = projected_funding - executable_cost
    stressed_after_cost = projected_after_cost - basis_reserve
    break_even_intervals = (
        (executable_cost + basis_reserve) / (conservative_rate * perp_entry_notional)
        if conservative_rate > 0 else Decimal("Infinity")
    )
    basis = ((Decimal(future.mark_price) - Decimal(future.index_price))
             / Decimal(future.index_price) * Decimal(10000))
    qualified = (
        len(rates) >= 100
        and positive_fraction >= Decimal("0.90")
        and current_rate > 0
        and conservative_rate > 0
        and stressed_after_cost > 0
    )
    return {
        "market": future.market,
        "spot_market": spot.market,
        "quantity": _text(quantity),
        "entry_notional_quote": _text(perp_entry_notional),
        "current_funding_rate": _text(current_rate),
        "history_mean_rate": _text(mean),
        "history_p25_rate": _text(p25),
        "history_positive_fraction": _text(positive_fraction),
        "history_samples": len(rates),
        "funding_interval_minutes": future.funding_interval_minutes,
        "horizon_days": horizon_days,
        "projected_intervals": int(intervals),
        "projected_funding_quote": _text(projected_funding),
        "executable_round_trip_cost_quote": _text(executable_cost),
        "basis_stress_reserve_quote": _text(basis_reserve),
        "projected_after_cost_quote": _text(projected_after_cost),
        "stressed_after_cost_quote": _text(stressed_after_cost),
        "break_even_intervals": _text(break_even_intervals),
        "current_mark_index_basis_bps": _text(basis),
        "spot_book_sha256": spot_book.digest,
        "future_book_sha256": future_book.digest,
        "funding_history_sha256": history.digest,
        "qualified_for_shadow_observation": qualified,
        "execution_environment": "SIMULATION_ONLY",
        "account_access": False,
        "order_authority": False,
        "leverage_enabled": False,
    }


def screen_once(*, client: WhiteBitPublicClient, notional: Decimal,
                horizon_days: int, basis_stress_bps: Decimal,
                candidate_limit: int) -> dict:
    instruments = client.instruments()
    by_market = {row.market: row for row in instruments}
    futures = [row for row in client.futures_markets()
               if row.quote_asset == "USDT" and Decimal(row.funding_rate) > 0
               and Decimal(row.quote_volume) >= Decimal("1000000")
               and f"{row.base_asset}_{row.quote_asset}" in by_market
               and row.market in by_market]
    futures.sort(key=lambda row: (
        -(Decimal(row.funding_rate) * Decimal(525600)
          / Decimal(row.funding_interval_minutes)), row.market))
    candidates, rejected = [], []
    for future in futures[:candidate_limit]:
        spot = by_market[f"{future.base_asset}_{future.quote_asset}"]
        perpetual = by_market[future.market]
        try:
            history = client.funding_history(future.market, limit=100)
            spot_book = client.instrument_orderbook(spot.market)
            future_book = client.instrument_orderbook(future.market)
            candidates.append(evaluate_pair(
                future=future, perpetual=perpetual, spot=spot, history=history,
                spot_book=spot_book, future_book=future_book,
                notional=notional, horizon_days=horizon_days,
                basis_stress_bps=basis_stress_bps,
            ))
        except (PublicDataError, ValueError) as error:
            rejected.append({"market": future.market, "reason": str(error)})
    candidates.sort(key=lambda row: (-Decimal(row["stressed_after_cost_quote"]), row["market"]))
    qualified = [row for row in candidates if row["qualified_for_shadow_observation"]]
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "schema": SCHEMA,
        "observed_at": now,
        "provider": "WHITEBIT",
        "strategy": "DELTA_NEUTRAL_SPOT_LONG_PERPETUAL_SHORT_FUNDING_CARRY",
        "notional_quote": _text(notional),
        "horizon_days": horizon_days,
        "basis_stress_bps": _text(basis_stress_bps),
        "screened_count": len(candidates),
        "qualified_count": len(qualified),
        "selected_market": qualified[0]["market"] if qualified else None,
        "candidates": candidates,
        "collection_rejections": rejected,
        "execution_environment": "SIMULATION_ONLY",
        "private_account_connected": False,
        "real_orders_enabled": False,
        "leverage_enabled": False,
        "profit_guaranteed": False,
    }


def run(*, state_dir: Path, interval_seconds: int, notional: Decimal,
        horizon_days: int, basis_stress_bps: Decimal, candidate_limit: int,
        once: bool = False, stop: Event | None = None,
        stop_file: Path | None = None) -> dict:
    state_dir.mkdir(parents=True, exist_ok=True)
    stop = stop or Event()
    client = WhiteBitPublicClient()
    last = {}
    while not stop.is_set():
        if stop_file is not None and stop_file.is_file():
            stop.set()
            break
        started = time.monotonic()
        try:
            last = screen_once(client=client, notional=notional,
                               horizon_days=horizon_days,
                               basis_stress_bps=basis_stress_bps,
                               candidate_limit=candidate_limit)
            last["worker_status"] = "RUNNING"
        except (PublicDataError, OSError, ValueError) as error:
            last = {
                "schema": SCHEMA,
                "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "worker_status": "DEGRADED",
                "error": str(error),
                "real_orders_enabled": False,
            }
        atomic_write_json(state_dir / "checkpoint.json", last)
        atomic_write_json(state_dir / "heartbeat.json", {
            "schema": SCHEMA + "-heartbeat",
            "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "status": last["worker_status"],
            "selected_market": last.get("selected_market"),
            "qualified_count": last.get("qualified_count", 0),
        })
        if once:
            return last
        remaining = max(1.0, interval_seconds - (time.monotonic() - started))
        deadline = time.monotonic() + remaining
        while not stop.is_set() and time.monotonic() < deadline:
            if stop_file is not None and stop_file.is_file():
                stop.set()
                break
            stop.wait(min(5.0, deadline - time.monotonic()))
    last["worker_status"] = "STOPPED"
    atomic_write_json(state_dir / "checkpoint.json", last)
    return last


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=int, default=3600)
    parser.add_argument("--notional", default="1000")
    parser.add_argument("--horizon-days", type=int, default=14)
    parser.add_argument("--basis-stress-bps", default="25")
    parser.add_argument("--candidate-limit", type=int, default=8)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not 300 <= args.interval_seconds <= 86400:
        parser.error("interval-seconds must be 300..86400")
    if not 1 <= args.horizon_days <= 90 or not 1 <= args.candidate_limit <= 16:
        parser.error("invalid horizon or candidate limit")
    notional, stress = Decimal(args.notional), Decimal(args.basis_stress_bps)
    if notional <= 0 or not Decimal(0) <= stress <= Decimal(1000):
        parser.error("notional/stress out of bounds")
    stop = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    report = run(state_dir=args.state_dir, interval_seconds=args.interval_seconds,
                 notional=notional, horizon_days=args.horizon_days,
                 basis_stress_bps=stress, candidate_limit=args.candidate_limit,
                 once=args.once, stop=stop, stop_file=args.stop_file)
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report.get("worker_status") != "DEGRADED" else 2


if __name__ == "__main__":
    raise SystemExit(main())

