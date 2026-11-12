"""Measure two fixed course setups with a 60-minute exposure cap."""
from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.strategies import course_quasimodo, course_range_deviation
from research.autotrade_research.strategies.technical_confluence import digest
from tools.screen_whitebit_trend_regime import authenticated_bars


def compare(manifests: list[Path], *, bars_per_cut: int) -> dict:
    configurations = {
        "TREND_QUASIMODO_TOP_DOWN": (
            course_quasimodo.simulate,
            course_quasimodo.CourseConfiguration(minimum_shift_confirmation_bars=5,
                shift_expiry_bars=8, require_true_top_down=True, maximum_hold_bars=4),
        ),
        "RANGE_DEVIATION": (
            course_range_deviation.simulate,
            course_range_deviation.RangeDeviationConfiguration(maximum_hold_bars=4),
        ),
    }
    markets = {}
    for path in manifests:
        bars, identity, metadata = authenticated_bars(path)
        market = metadata["market"]
        if (market in markets or metadata["interval"] != "15m"
                or len(bars) < 2 * bars_per_cut):
            raise ValueError("each distinct market needs two disjoint M15 cuts")
        market_result = {"dataset_identity_sha256": identity, "cuts": {}}
        markets[market] = market_result
        for name, cut in (("EARLIER", bars[:bars_per_cut]), ("LATER", bars[-bars_per_cut:])):
            results = {}
            for family, (simulate, configuration) in configurations.items():
                results[family] = simulate(cut, configuration=configuration,
                                           report_hourly_returns=True)
            market_result["cuts"][name] = {
                "first_open_at": cut[0]["opened_at"].isoformat(),
                "last_close_at": cut[-1]["closed_at"].isoformat(),
                "families": results,
            }
    report = {
        "schema": "autotrade-whitebit-course-hourly-diagnostic-v1",
        "bars_per_cut": bars_per_cut,
        "holding_cap_minutes": 60,
        "markets": markets,
        "status": "HISTORICAL_DIAGNOSTIC",
        "automatic_promotion": False,
        "network_orders": False,
    }
    report["sha256"] = digest(report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", action="append", type=Path, required=True)
    parser.add_argument("--bars-per-cut", type=int, default=4000)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= len(args.manifest) <= 6 or not 2000 <= args.bars_per_cut <= 20000:
        parser.error("provide 1..6 manifests and 2000..20000 bars per cut")
    report = compare(args.manifest, bars_per_cut=args.bars_per_cut)
    path = args.output_dir / f"{report['sha256']}.json"
    atomic_write_json(path, report)
    for market, value in report["markets"].items():
        for stage, cut in value["cuts"].items():
            for family, result in cut["families"].items():
                print(market, stage, family, result["closed_cycles"],
                      f"{Decimal(result['net_pnl']):.4f}",
                      [row["target_hits"] for row in result["clock_window_returns"].values()])
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
