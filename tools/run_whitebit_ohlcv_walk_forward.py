#!/usr/bin/env python3
"""Non-overlapping regime validation for the WhiteBIT OHLCV alpha family.

Each chronological window independently fits on its own TRAIN partition,
selects a threshold on DEVELOPMENT and unlocks its FINAL only after both pass.
Windows never share candles, so a favorable regime cannot dominate every test.
This is retrospective robustness research with no promotion or order authority.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from tools.screen_whitebit_ohlcv_alpha import screen
from tools.screen_whitebit_trend_regime import authenticated_bars


SCHEMA = "whitebit-ohlcv-nonoverlap-walk-forward-v1"


def window_ranges(total_bars: int, *, window_bars: int, window_count: int) -> list[tuple[int, int]]:
    if (type(total_bars) is not int or type(window_bars) is not int or
            type(window_count) is not int or window_bars < 1000 or
            not 2 <= window_count <= 12 or window_bars*window_count > total_bars):
        raise ValueError("invalid non-overlapping window request")
    start = total_bars-window_bars*window_count
    result = [(start+index*window_bars, start+(index+1)*window_bars)
              for index in range(window_count)]
    if any(left[1] != right[0] for left, right in zip(result, result[1:])):
        raise AssertionError("walk-forward windows are not contiguous")
    return result


def evaluate_windows(bars: list[dict], *, horizon: int, window_bars: int = 4000,
        window_count: int = 4, round_trip_cost_bps: float = 26.0,
        minimum_trades: int = 5, maximum_drawdown: float = 0.20,
        seed: int = 17, screen_fn=screen) -> dict:
    ranges = window_ranges(len(bars), window_bars=window_bars, window_count=window_count)
    windows = []
    for index, (start, end) in enumerate(ranges):
        selected = bars[start:end]
        result = screen_fn(selected, horizon=horizon,
            round_trip_cost_bps=round_trip_cost_bps,
            minimum_trades=minimum_trades, maximum_drawdown=maximum_drawdown,
            allow_short=False, seed=seed)
        windows.append({"index": index, "start_bar": start, "end_bar_exclusive": end,
            "start_opened_at": selected[0]["opened_at"].isoformat(),
            "end_closed_at": selected[-1]["closed_at"].isoformat(),
            "result": result})
    qualified = [row for row in windows if row["result"]["prospective_qualification"]]
    consumed = [row for row in windows if row["result"]["final_consumed"]]
    final_returns = [float(row["result"]["final"]["net_return_fraction"])
                     for row in consumed]
    aggregate = 1.0
    for value in final_returns:
        aggregate *= 1.0+value
    robust = len(qualified) == window_count
    payload = {"schema": SCHEMA, "evidence_class": "ZERO_POSTHOC_ROBUSTNESS_RESEARCH",
        "horizon_bars": horizon, "window_bars": window_bars,
        "window_count": window_count, "windows_non_overlapping": True,
        "round_trip_cost_bps": format(round_trip_cost_bps, ".6f"),
        "minimum_trades_per_partition": minimum_trades,
        "maximum_drawdown_fraction": format(maximum_drawdown, ".6f"),
        "seed": seed, "windows": windows,
        "qualified_window_count": len(qualified),
        "final_consumed_window_count": len(consumed),
        "consumed_final_compound_return_fraction": format(aggregate-1.0, ".12f"),
        "worst_consumed_final_return_fraction": None if not final_returns else format(
            min(final_returns), ".12f"),
        "robustness_gate_passed": robust,
        "automatic_promotion": False, "network_orders": False,
        "trading_authority": False, "paid_model_calls": 0}
    payload["result_sha256"] = sha256(json.dumps(payload, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return payload


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2, allow_nan=False)+"\n")
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--horizon", type=int, default=24)
    parser.add_argument("--window-bars", type=int, default=4000)
    parser.add_argument("--window-count", type=int, default=4)
    parser.add_argument("--round-trip-cost-bps", type=float, default=26.0)
    parser.add_argument("--minimum-trades", type=int, default=5)
    parser.add_argument("--maximum-drawdown", type=float, default=0.20)
    parser.add_argument("--seed", type=int, default=17)
    args = parser.parse_args(argv)
    started = perf_counter()
    bars, dataset_sha256, manifest = authenticated_bars(args.manifest)
    result = evaluate_windows(bars, horizon=args.horizon,
        window_bars=args.window_bars, window_count=args.window_count,
        round_trip_cost_bps=args.round_trip_cost_bps,
        minimum_trades=args.minimum_trades,
        maximum_drawdown=args.maximum_drawdown, seed=args.seed)
    result.update(market=manifest["market"], interval=manifest["interval"],
        dataset_identity_sha256=dataset_sha256)
    result.pop("result_sha256", None)
    result["result_sha256"] = sha256(json.dumps(result, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    target = args.output_dir / f"{manifest['market'].lower()}-h{args.horizon}-{result['result_sha256']}.json"
    _atomic(target, {"source": {"manifest": str(args.manifest),
        "dataset_identity_sha256": dataset_sha256}, "result": result})
    print(json.dumps({"result_path": str(target), "market": manifest["market"],
        "horizon_bars": args.horizon,
        "qualified_window_count": result["qualified_window_count"],
        "window_count": result["window_count"],
        "robustness_gate_passed": result["robustness_gate_passed"],
        "consumed_final_compound_return_fraction": result[
            "consumed_final_compound_return_fraction"],
        "elapsed_seconds": round(perf_counter()-started, 3),
        "network_orders": False, "paid_model_calls": 0}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

