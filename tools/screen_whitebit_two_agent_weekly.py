"""Replay two local specialists over authenticated archived market data."""

from argparse import ArgumentParser
import json
from pathlib import Path

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.strategies.fast_screen import digest
from research.autotrade_research.strategies.two_agent_weekly import evaluate
from research.autotrade_research.strategies.technical_grid import evaluate_grid
from tools.screen_whitebit_trend_regime import authenticated_bars


def main(argv=None) -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, nargs=3, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--selective", action="store_true",
                        help="use the lower-turnover, quarter-sleeve confirmation policy")
    mode.add_argument("--technical-grid", action="store_true",
                      help="compare range and trend order ladders with protective stops")
    mode.add_argument("--confirmed-breakout", action="store_true",
                      help="compare 12- and 48-hour initial breakout retest ladders")
    parser.add_argument("--first-level-only", action="store_true",
                        help="remove the second entry level from the technical grid")
    args = parser.parse_args(argv)
    sources = {}
    inputs = {}
    intervals = set()
    for path in args.manifest:
        bars, identity, metadata = authenticated_bars(path)
        market, interval = metadata["market"], metadata["interval"]
        if market in inputs:
            parser.error("duplicate archive market")
        inputs[market] = bars
        sources[market] = identity
        intervals.add(interval)
    if intervals not in ({"1m"}, {"5m"}, {"15m"}, {"1h"}):
        parser.error("use three archives of the same 1m, 5m, 15m or 1h cadence")
    if args.selective and intervals != {"15m"}:
        parser.error("selective mode requires M15 archives")
    grid_mode = args.technical_grid or args.confirmed_breakout
    if grid_mode and intervals not in ({"15m"}, {"1h"}):
        parser.error("grid modes require M15 perpetual or H1 spot archives")
    if not grid_mode and intervals == {"1h"}:
        parser.error("H1 archives require a grid mode")
    if args.first_level_only and not args.technical_grid:
        parser.error("--first-level-only requires --technical-grid")
    if grid_mode:
        result = evaluate_grid(inputs, first_level_only=args.first_level_only,
                               spot=intervals == {"1h"}, continuation=args.confirmed_breakout)
    else:
        result = evaluate(inputs, step=int(next(iter(intervals))[:-1]), selective=args.selective)
    receipt = {"sources": sources, "result": result}
    output = args.output_dir / f"{digest(receipt)}.json"
    atomic_write_json(output, receipt)
    print(json.dumps({"output": str(output), "mode": result["mode"],
                      "final_evaluated": result["final_evaluated"],
                      "result_sha256": result["result_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
