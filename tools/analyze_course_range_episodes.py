#!/usr/bin/env python3
"""Diagnose completed TRAIN episodes from the frozen course range family."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
import json
from pathlib import Path

from research.autotrade_research.strategies.course_range_deviation import simulate
from tools.screen_whitebit_trend_regime import authenticated_bars


def _one(path_and_count: tuple[str, int]) -> tuple[str, list[dict]]:
    path, count = path_and_count
    bars, _, manifest = authenticated_bars(Path(path))
    return manifest["market"], simulate(bars[:count])["episodes"]


def _bin(value: Decimal, bounds: tuple[Decimal, ...], labels: tuple[str, ...]) -> str:
    for bound, label in zip(bounds, labels):
        if value <= bound:
            return label
    return labels[-1]


def _summary(rows: list[dict], key) -> dict:
    cohorts: dict[str, list[dict]] = {}
    for row in rows:
        cohorts.setdefault(key(row), []).append(row)
    result = {}
    for name, values in sorted(cohorts.items()):
        pnl = sum((Decimal(row["pnl_after_costs"]) for row in values), Decimal(0))
        wins = sum(Decimal(row["pnl_after_costs"]) > 0 for row in values)
        result[name] = {"cycles": len(values), "wins": wins,
            "win_rate": str(Decimal(wins) / Decimal(len(values))),
            "pnl_after_costs": str(pnl)}
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-root", type=Path, required=True)
    parser.add_argument("--train-count", type=int, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    paths = [str(path) for path in sorted(args.manifest_root.rglob("*.manifest.json"))]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        values = list(pool.map(_one, [(path, args.train_count) for path in paths]))
    episodes = [{**row, "market": market} for market, rows in values for row in rows]
    age = lambda row: _bin(Decimal(row["deviation_to_shift_bars"]),
        (Decimal(2), Decimal(4), Decimal(8)), ("1-2", "3-4", "5-8"))
    efficiency = lambda row: _bin(Decimal(row["directional_efficiency"]),
        (Decimal("0.10"), Decimal("0.15"), Decimal("0.20"), Decimal("0.25")),
        ("<=0.10", "0.10-0.15", "0.15-0.20", "0.20-0.25"))
    width = lambda row: _bin(Decimal(row["range_width_atr"]),
        (Decimal(6), Decimal(10), Decimal(20)), ("4-6", "6-10", "10-20"))
    reward_risk = lambda row: _bin(Decimal(row["planned_reward_risk"]),
        (Decimal(3), Decimal(5), Decimal("1e9")), ("2-3", "3-5", ">5"))
    output = {"schema": "autotrade-course-range-train-episode-analysis-v1",
        "partition": "TRAIN_ONLY", "train_count_per_market": args.train_count,
        "market_count": len(values), "episode_count": len(episodes),
        "by_market": _summary(episodes, lambda row: row["market"]),
        "by_direction": _summary(episodes, lambda row: row["direction"]),
        "by_confirmation_age": _summary(episodes, age),
        "by_directional_efficiency": _summary(episodes, efficiency),
        "by_range_width_atr": _summary(episodes, width),
        "by_planned_reward_risk": _summary(episodes, reward_risk),
        "development_consumed": False, "final_consumed": False,
        "network_orders": False, "trading_authority": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

