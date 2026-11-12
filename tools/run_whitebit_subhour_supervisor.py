#!/usr/bin/env python3
"""Run a small 30/45/60-minute WhiteBIT technical research protocol."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from time import monotonic, sleep

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.strategies.technical_confluence import (
    TechnicalConfiguration, TechnicalCosts, simulate, summarize_daily_returns,
)
from tools.historical_public_klines import HistoricalArchiveError
from tools.screen_whitebit_technical_confluence import locked_final_screen
from tools.screen_whitebit_trend_regime import authenticated_bars
from tools.whitebit_historical_klines import collect_whitebit_history


SCHEMA = "autotrade-whitebit-subhour-supervisor-v1"
MARKETS = ("SOL_PERP",)
FROZEN_STRATEGY_ID = "SOL_BOLLINGER_RSI_60M_V1"


def configurations() -> tuple[TechnicalConfiguration, ...]:
    common = dict(stop_atr="1", take_profit_atr="4",
        minimum_expected_move_fraction="0.006", cooldown_bars=4,
        maximum_entries_per_utc_day=1)
    return (
        TechnicalConfiguration(family="BOLLINGER_RSI_REVERSION", fast_ema=8,
            slow_ema=32, rsi_long_min="30", rsi_short_max="70",
            bollinger_period=20, bollinger_width="2.5", minimum_hold_bars=2,
            maximum_hold_bars=4, **common),
    )


def _perfect_timing_benchmark(rows: list[dict], hold_cap: int) -> dict:
    """Find the best single same-day fill pair with hindsight at M15 boundaries."""
    costs = TechnicalCosts()
    allocation = Decimal(costs.capital_fraction)
    fee = Decimal(costs.fee_bps_per_fill) / 10_000
    adverse = (Decimal(costs.spread_bps) / 2 + Decimal(costs.slippage_bps_per_fill)) / 10_000
    days: dict[date, list[dict]] = {}
    for bar in rows:
        days.setdefault(bar["opened_at"].astimezone(UTC).date(), []).append(bar)
    returns = []
    for day in days.values():
        start = day[0]["opened_at"].astimezone(UTC)
        if (start != start.replace(hour=0, minute=0, second=0, microsecond=0)
                or day[-1]["closed_at"] - start != timedelta(days=1)):
            continue
        prices = [Decimal(bar["open"]) for bar in day] + [Decimal(day[-1]["close"])]
        best = Decimal(0)
        for entry_index, entry in enumerate(prices[:-2]):
            for exit_reference in prices[entry_index + 2:entry_index + hold_cap + 1]:
                long_ratio = exit_reference * (1 - adverse) / (entry * (1 + adverse))
                short_ratio = exit_reference * (1 + adverse) / (entry * (1 - adverse))
                best = max(best, allocation * (long_ratio - 1 - fee * (1 + long_ratio)),
                           allocation * (1 - short_ratio - fee * (1 + short_ratio)))
        returns.append(best)
    return {
        "method": "ONE_SAME_UTC_DAY_TRADE_AT_M15_BOUNDARIES_WITH_PERFECT_TIMING",
        "funding_included": False,
        "is_strategy_performance": False,
        **summarize_daily_returns(returns),
    }


def _compare_variant(cut: list[dict], hold_cap: int) -> dict:
    result = simulate(cut, candidate="TECHNICAL_CONFLUENCE",
                      configuration=replace(configurations()[0], maximum_hold_bars=hold_cap),
                      report_daily_returns=True)
    result["perfect_timing_benchmark"] = _perfect_timing_benchmark(cut, hold_cap)
    return result


def compare_horizons(rows: list[dict], *, train_count: int,
                     development_count: int) -> dict:
    """Compare only the holding cap. Leave the final cut to the frozen protocol."""
    cuts = {
        "TRAIN": rows[:train_count],
        "DEVELOPMENT": rows[train_count:train_count + development_count],
    }
    return {
        "entry_policy": FROZEN_STRATEGY_ID,
        "final_used_for_comparison": False,
        "variants": {
            str(15 * cap): {
                stage: _compare_variant(cut, cap)
                for stage, cut in cuts.items()
            }
            for cap in (2, 3, 4)
        },
    }


def run_cycle(*, state_dir: Path, bars: int, now: datetime) -> dict:
    current = int(now.astimezone(UTC).timestamp()) // 900 * 900
    end = current - 900; start = end - (bars - 1) * 900
    train = int(bars * .6); development = int(bars * .2)
    results = {}; history_root = state_dir / "history"
    for market in MARKETS:
        collect_whitebit_history(market=market, interval="15m", start=start,
            end=end, output=history_root, now=now.astimezone(UTC))
        manifest = history_root / "spot" / market / "15m" / f"{start}-{end}.manifest.json"
        rows, identity, _ = authenticated_bars(manifest)
        value = locked_final_screen(rows, train_count=train,
            development_count=development, dataset_sha256=identity,
            configurations=configurations(), minimum_cycles=5,
            gate_policy="POSITIVE_AFTER_COSTS")
        full_path = state_dir / "runs" / market.lower() / f"{value['result_sha256']}.json"
        atomic_write_json(full_path, value)
        horizons = compare_horizons(rows, train_count=train, development_count=development)
        horizons["dataset_identity_sha256"] = identity
        horizons["report_sha256"] = sha256(json.dumps(horizons, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        horizon_path = full_path.parent / f"{horizons['report_sha256']}.horizons.json"
        atomic_write_json(horizon_path, horizons)
        results[market] = {"dataset_identity_sha256": identity,
            "selected_configuration": value["selected_configuration"],
            "final_consumed": value["final_consumed"],
            "prospective_qualification": value["prospective_qualification"],
            "final_rejection_reasons": value["final_rejection_reasons"],
            "result_sha256": value["result_sha256"], "result_path": str(full_path),
            "horizon_report_path": str(horizon_path)}
    result = {"schema": SCHEMA, "observed_at": now.astimezone(UTC).isoformat(),
        "window": {"start_open_epoch": start, "end_open_epoch": end, "bars": bars,
            "train_count": train, "development_count": development,
            "final_count": bars - train - development},
        "time_horizons_minutes": [30, 45, 60], "configuration_count": 1,
        "frozen_strategy_id": FROZEN_STRATEGY_ID,
        "strategy_mutation_allowed": False,
        "qualification_objective": "ABSOLUTE_POSITIVE_AFTER_COSTS",
        "controls_retained_for_COMPARISON": True,
        "configurations": [asdict(value) for value in configurations()],
        "markets": results,
        "qualified_market_count": sum(row["prospective_qualification"]
            for row in results.values()), "network_orders": False,
        "trading_authority": False, "automatic_promotion": False,
        "paid_model_calls": 0}
    result["cycle_sha256"] = sha256(json.dumps(result, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    atomic_write_json(state_dir / "runs" / f"{result['cycle_sha256']}.json", result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--bars", type=int, default=6000)
    parser.add_argument("--interval-seconds", type=int, default=21600)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not 4000 <= args.bars <= 30000:
        parser.error("bars must be 4000..30000")
    if not 3600 <= args.interval_seconds <= 86400:
        parser.error("interval-seconds must be 3600..86400")
    state_dir = args.state_dir.resolve(strict=False); state_dir.mkdir(parents=True, exist_ok=True)
    stop_file = args.stop_file.resolve(strict=False)
    try: stop_file.relative_to(state_dir)
    except ValueError: parser.error("stop-file must remain inside state-dir")
    stop_file.unlink(missing_ok=True); checkpoint = state_dir / "checkpoint.json"
    cycle_count = 0
    if checkpoint.is_file():
        prior = json.loads(checkpoint.read_bytes()); cycle_count = int(prior.get("cycle_count", 0))
    while not stop_file.exists():
        now = datetime.now(UTC); started = monotonic(); cycle_count += 1
        try:
            result = run_cycle(state_dir=state_dir, bars=args.bars, now=now)
            error = None; status = "RUNNING"
        except (HistoricalArchiveError, OSError, ValueError, TypeError,
                KeyError, RuntimeError) as exc:
            result = None; error = type(exc).__name__; status = "RECOVERY"
        value = {"schema": SCHEMA, "status": status, "cycle_count": cycle_count,
            "last_cycle_seconds": monotonic() - started, "last_error_class": error,
            "last_result": result, "next_run_at": (now + timedelta(
                seconds=args.interval_seconds)).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(), "network_orders": False,
            "trading_authority": False, "automatic_promotion": False,
            "paid_model_calls": 0}
        atomic_write_json(checkpoint, value)
        if args.once:
            atomic_write_json(checkpoint, {**value, "status": "STOPPED",
                "updated_at": datetime.now(UTC).isoformat()})
            return 0 if error is None else 2
        deadline = monotonic() + args.interval_seconds
        while not stop_file.exists() and monotonic() < deadline:
            sleep(min(15, max(1, deadline - monotonic())))
    atomic_write_json(checkpoint, {**value, "status": "STOPPED",
        "updated_at": datetime.now(UTC).isoformat()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
