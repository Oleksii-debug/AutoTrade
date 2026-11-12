#!/usr/bin/env python3
"""Daily costed locked-final screen of liquid WhiteBIT USDT opportunities.

This process owns no credentials or order route.  It ranks the current public
spot universe by reported quote volume, acquires completed hourly candles and
screens a bounded predefined long/short grid without exposing DEVELOPMENT or
FINAL to configurations that fail earlier gates.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from time import monotonic, sleep

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.integrations.whitebit_public import (
    PublicDataError,
    WhiteBitPublicClient,
)
from tools.historical_public_klines import HistoricalArchiveError
from tools.screen_whitebit_trend_grid import default_grid, locked_final_grid_screen
from tools.screen_whitebit_trend_regime import authenticated_bars
from tools.screen_whitebit_volatility_grid import (
    default_grid as volatility_grid,
    locked_final_grid_screen as volatility_screen,
)
from tools.whitebit_historical_klines import collect_whitebit_history


SCHEMA = "autotrade-whitebit-opportunity-supervisor-v1"
INTERVAL_SECONDS = 3600
CORE_MARKETS = frozenset({"BTC_USDT", "ETH_USDT", "SOL_USDT"})
STABLE_BASES = frozenset({
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "PYUSD", "EUR", "USD",
})


def fast_volatility_grid() -> tuple:
    """Span the full grid with 16 deterministic configurations."""
    full = tuple(volatility_grid())
    indices = tuple(round(index * (len(full) - 1) / 15) for index in range(16))
    return tuple(full[index] for index in indices)


def completed_window(now: datetime, bars: int) -> tuple[int, int]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("opportunity clock must be timezone-aware")
    if type(bars) is not int or not 800 <= bars <= 4096:
        raise ValueError("opportunity bars must be 800..4096")
    current = int(now.astimezone(UTC).timestamp()) // INTERVAL_SECONDS * INTERVAL_SECONDS
    end = current - INTERVAL_SECONDS
    return end - (bars - 1) * INTERVAL_SECONDS, end


def rank_markets(markets: tuple[dict, ...], tickers: tuple, *, limit: int,
                 minimum_quote_volume: Decimal) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 32:
        raise ValueError("opportunity market limit must be 1..32")
    available = {
        row["market"] for row in markets
        if row.get("quote_asset") == "USDT"
        and row.get("base_asset", row["market"].split("_", 1)[0]) not in STABLE_BASES
        and row["market"] not in CORE_MARKETS
    }
    ranked = []
    for ticker in tickers:
        if ticker.market not in available or ticker.market in CORE_MARKETS or ticker.is_frozen:
            continue
        base = ticker.market.removesuffix("_USDT")
        if base in STABLE_BASES:
            continue
        try:
            volume = Decimal(ticker.quote_volume)
        except (InvalidOperation, TypeError):
            continue
        if not volume.is_finite() or volume < minimum_quote_volume:
            continue
        ranked.append({"market": ticker.market, "quote_volume_24h": str(volume)})
    ranked.sort(key=lambda row: (-Decimal(row["quote_volume_24h"]), row["market"]))
    return ranked[:limit]


def _compact_trend(result: dict) -> dict:
    selected = None
    if result["selected_configuration_sha256"] is not None:
        selected = next(row for row in result["screened"]
                        if row["configuration_sha256"] == result["selected_configuration_sha256"])
    return {
        "dataset_identity_sha256": result["dataset_identity_sha256"],
        "configuration_count": result["configuration_count"],
        "train_survivor_count": sum(not row["train_rejection_reasons"]
                                    for row in result["screened"]),
        "development_survivor_count": sum(
            row["development"] is not None and not row["development_rejection_reasons"]
            for row in result["screened"]),
        "selected_configuration_sha256": result["selected_configuration_sha256"],
        "selected_train_net_pnl": None if selected is None else selected["train"]["net_pnl"],
        "selected_development_net_pnl": (
            None if selected is None or selected["development"] is None
            else selected["development"]["net_pnl"]),
        "final_consumed": result["final_consumed"],
        "prospective_qualification": result["prospective_qualification"],
        "final_rejection_reasons": result["final_rejection_reasons"],
        "network_orders": False,
        "trading_authority": False,
    }


def _compact_volatility(result: dict) -> dict:
    return {
        "dataset_identity_sha256": result["dataset_sha256"],
        "configuration_count": result["configuration_count"],
        "train_survivor_count": sum(not row["train_rejection_reasons"]
                                    for row in result["screened"]),
        "development_survivor_count": sum(
            row["development"] is not None and not row["development_rejection_reasons"]
            for row in result["screened"]),
        "selected_configuration_sha256": result["selected_configuration_sha256"],
        "final_consumed": result["final_consumed"],
        "prospective_qualification": result["prospective_qualification"],
        "final_rejection_reasons": result["final_rejection_reasons"],
        "network_orders": False,
        "trading_authority": False,
    }


def run_cycle(*, state_dir: Path, bars: int, market_limit: int,
              minimum_quote_volume: Decimal, now: datetime,
              client: WhiteBitPublicClient | None = None) -> dict:
    client = client or WhiteBitPublicClient()
    selected = rank_markets(client.markets(), client.market_activities(), limit=market_limit,
                            minimum_quote_volume=minimum_quote_volume)
    start, end = completed_window(now, bars)
    train_count, development_count = bars * 3 // 5, bars // 5
    history_root, result_root = state_dir / "history", state_dir / "results"
    reports = {}
    for rank, row in enumerate(selected, 1):
        market = row["market"]
        try:
            manifest = collect_whitebit_history(
                market=market, interval="1h", start=start, end=end,
                output=history_root, now=now.astimezone(UTC),
            )
            manifest_path = (history_root / "spot" / market / "1h" /
                             f"{start}-{end}.manifest.json")
            candle_rows, dataset, _ = authenticated_bars(manifest_path)
            trend = locked_final_grid_screen(
                candle_rows, train_count=train_count,
                development_count=development_count,
                dataset_sha256=dataset, configurations=default_grid(),
            )
            fast_volatility = volatility_screen(
                candle_rows, train_count=train_count,
                development_count=development_count,
                dataset_sha256=dataset, configurations=fast_volatility_grid(),
            )
            fast_train_survivors = sum(not row["train_rejection_reasons"]
                                       for row in fast_volatility["screened"])
            volatility = fast_volatility
            volatility_stage = "FAST_16"
            if fast_train_survivors:
                volatility = volatility_screen(
                    candle_rows, train_count=train_count,
                    development_count=development_count,
                    dataset_sha256=dataset, configurations=volatility_grid(),
                )
                volatility_stage = "FULL_96_AFTER_FAST_TRAIN_SURVIVOR"
            result_path = result_root / market.lower() / f"{trend['result_sha256']}.json"
            volatility_path = (result_root / market.lower() /
                               f"{volatility['result_sha256']}.volatility.json")
            atomic_write_json(result_path, {
                "market": market, "rank": rank, "quote_volume_24h": row["quote_volume_24h"],
                "manifest": str(manifest_path), "manifest_identity": manifest["dataset_identity_sha256"],
                "family": "LONG_SHORT_TREND", "result": trend,
            })
            atomic_write_json(volatility_path, {
                "market": market, "rank": rank, "quote_volume_24h": row["quote_volume_24h"],
                "manifest": str(manifest_path), "manifest_identity": manifest["dataset_identity_sha256"],
                "family": "LONG_VOLATILITY_EXPANSION", "stage": volatility_stage,
                "fast_train_survivor_count": fast_train_survivors,
                "result": volatility,
            })
            family_qualified = (trend["prospective_qualification"] or
                                volatility["prospective_qualification"])
            reports[market] = {**row, "rank": rank, "status": "SCREENED",
                               "prospective_qualification": family_qualified,
                               "trend_result_path": str(result_path),
                               "volatility_result_path": str(volatility_path),
                               "trend": _compact_trend(trend),
                               "volatility_stage": volatility_stage,
                               "volatility": _compact_volatility(volatility)}
        except (HistoricalArchiveError, PublicDataError, OSError, ValueError,
                TypeError, KeyError, RuntimeError) as exc:
            reports[market] = {**row, "rank": rank, "status": "REJECTED_DATA",
                               "error_class": type(exc).__name__}
    return {
        "window": {"start_open_epoch": start, "end_open_epoch": end, "bars": bars},
        "selected_market_count": len(selected), "markets": reports,
        "qualified_market_count": sum(
            report.get("prospective_qualification") is True
            for report in reports.values()),
        "network_orders": False, "trading_authority": False,
        "automatic_promotion": False, "paid_model_calls": 0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--bars", type=int, default=2000)
    parser.add_argument("--market-limit", type=int, default=12)
    parser.add_argument("--minimum-quote-volume", type=Decimal, default=Decimal("1000000"))
    parser.add_argument("--interval-seconds", type=int, default=86400)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not 800 <= args.bars <= 4096 or not 1 <= args.market_limit <= 32:
        parser.error("bars or market limit outside bounded range")
    if (not args.minimum_quote_volume.is_finite() or args.minimum_quote_volume < 0
            or not 3600 <= args.interval_seconds <= 604800):
        parser.error("volume or interval outside bounded range")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = args.state_dir / "checkpoint.json"
    checkpoint = {}
    if checkpoint_path.is_file():
        if checkpoint_path.is_symlink() or checkpoint_path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("opportunity checkpoint is redirected or unbounded")
        checkpoint = json.loads(checkpoint_path.read_bytes())
        if checkpoint.get("schema") != SCHEMA:
            raise ValueError("opportunity checkpoint schema differs")
    checkpoint.update(schema=SCHEMA, status="RUNNING", updated_at=datetime.now(UTC).isoformat(),
                      network_orders=False, trading_authority=False, automatic_promotion=False)
    atomic_write_json(checkpoint_path, checkpoint)
    while True:
        now = datetime.now(UTC)
        if args.stop_file and args.stop_file.exists():
            checkpoint.update(status="STOPPED", updated_at=now.isoformat())
            atomic_write_json(checkpoint_path, checkpoint)
            return 0
        due_text = checkpoint.get("next_run_at")
        due = datetime.fromisoformat(due_text) if type(due_text) is str else now
        if now >= due:
            started = monotonic()
            try:
                result = run_cycle(state_dir=args.state_dir, bars=args.bars,
                                   market_limit=args.market_limit,
                                   minimum_quote_volume=args.minimum_quote_volume, now=now)
                status, error = "RUNNING", None
            except (PublicDataError, HistoricalArchiveError, OSError, ValueError,
                    TypeError, KeyError, RuntimeError) as exc:
                result, status, error = checkpoint.get("last_result"), "RECOVERING", type(exc).__name__
            checkpoint = {
                "schema": SCHEMA, "status": status,
                "cycle_count": int(checkpoint.get("cycle_count", 0)) + 1,
                "last_result": result, "last_error_class": error,
                "last_cycle_seconds": monotonic() - started,
                "updated_at": datetime.now(UTC).isoformat(),
                "next_run_at": (now + timedelta(seconds=args.interval_seconds)).isoformat(),
                "network_orders": False, "trading_authority": False,
                "automatic_promotion": False, "paid_model_calls": 0,
            }
            atomic_write_json(checkpoint_path, checkpoint)
            if args.once:
                checkpoint["status"] = "STOPPED"
                atomic_write_json(checkpoint_path, checkpoint)
                return 0 if error is None else 2
        sleep(min(30, max(1, (due - now).total_seconds())))


if __name__ == "__main__":
    raise SystemExit(main())

