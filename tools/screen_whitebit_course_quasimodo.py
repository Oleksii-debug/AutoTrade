#!/usr/bin/env python3
"""Run frozen private-course Quasimodo research on authenticated WhiteBIT history."""
from __future__ import annotations

import argparse
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

from research.autotrade_research.strategies.course_quasimodo import (
    CourseConfiguration, VERSION, simulate,
)
from tools.screen_whitebit_trend_regime import authenticated_bars


SCHEMA = "autotrade-whitebit-private-course-quasimodo-screen-v1"


def screen(bars: list[dict], *, train_count: int, development_count: int,
           dataset_sha256: str, minimum_cycles: int = 10,
           configuration: CourseConfiguration | None = None) -> dict:
    if train_count < 1000 or development_count < 500 or train_count + development_count > len(bars) - 500:
        raise ValueError("course screen requires substantial TRAIN, DEVELOPMENT and FINAL")
    config = configuration or CourseConfiguration()
    parts = {"TRAIN": bars[:train_count],
             "DEVELOPMENT": bars[train_count:train_count + development_count],
             "FINAL": bars[train_count + development_count:]}
    train = simulate(parts["TRAIN"], configuration=config)
    train_reasons = []
    if train["closed_cycles"] < minimum_cycles:
        train_reasons.append("MINIMUM_TRAIN_CYCLES_NOT_MET")
    if Decimal(train["net_pnl"]) <= 0:
        train_reasons.append("TRAIN_NO_POSITIVE_AFTER_COST_PNL")
    development = None; development_reasons = []
    if not train_reasons:
        development = simulate(parts["DEVELOPMENT"], configuration=config)
        if development["closed_cycles"] < minimum_cycles:
            development_reasons.append("MINIMUM_DEVELOPMENT_CYCLES_NOT_MET")
        if Decimal(development["net_pnl"]) <= 0:
            development_reasons.append("DEVELOPMENT_NO_POSITIVE_AFTER_COST_PNL")
    final = None; final_reasons = ["PRIOR_PARTITIONS_NOT_QUALIFIED"]
    if development is not None and not development_reasons:
        final = simulate(parts["FINAL"], configuration=config)
        final_reasons = []
        if final["closed_cycles"] < minimum_cycles:
            final_reasons.append("MINIMUM_FINAL_CYCLES_NOT_MET")
        if Decimal(final["net_pnl"]) <= 0:
            final_reasons.append("FINAL_NO_POSITIVE_AFTER_COST_PNL")
    result = {"schema": SCHEMA, "strategy_schema": VERSION,
        "dataset_identity_sha256": dataset_sha256,
        "train_count": train_count, "development_count": development_count,
        "final_count": len(parts["FINAL"]), "minimum_cycles_per_partition": minimum_cycles,
        "train": train, "train_rejection_reasons": train_reasons,
        "development": development, "development_rejection_reasons": development_reasons,
        "final": final, "final_rejection_reasons": final_reasons,
        "final_consumed": final is not None,
        "prospective_qualification": final is not None and not final_reasons,
        "frozen_family_policy": "NO_PARAMETER_CHANGE_BEFORE_100_CLOSED_CYCLES_OR_PREDECLARED_FUTILITY",
        "canonical_qualification": False, "network_orders": False,
        "trading_authority": False, "paid_model_calls": 0}
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
    result["result_sha256"] = sha256(encoded.encode()).hexdigest()
    return result


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".course-quasimodo-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True, indent=2); stream.write("\n")
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--train-count", type=int, required=True)
    parser.add_argument("--development-count", type=int, required=True)
    parser.add_argument("--minimum-cycles", type=int, default=10)
    parser.add_argument("--minimum-shift-bars", type=int, default=1)
    parser.add_argument("--maximum-shift-bars", type=int, default=16)
    parser.add_argument("--shift-lookback", type=int, default=12)
    parser.add_argument("--require-top-down", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    bars, dataset, manifest = authenticated_bars(args.manifest)
    configuration = CourseConfiguration(
        minimum_shift_confirmation_bars=args.minimum_shift_bars,
        shift_expiry_bars=args.maximum_shift_bars,
        shift_lookback=args.shift_lookback,
        require_true_top_down=args.require_top_down)
    result = screen(bars, train_count=args.train_count,
        development_count=args.development_count, dataset_sha256=dataset,
        minimum_cycles=args.minimum_cycles, configuration=configuration)
    target = args.output_dir / f"{result['result_sha256']}.json"
    _atomic(target, {"source": {"venue": "WHITEBIT_PUBLIC", "market": manifest["market"],
        "manifest": str(args.manifest), "private_course_text_embedded": False},
        "result": result})
    print(json.dumps({"result_path": str(target), "market": manifest["market"],
        "train_cycles": result["train"]["closed_cycles"],
        "train_net_pnl": result["train"]["net_pnl"],
        "development_consumed": result["development"] is not None,
        "final_consumed": result["final_consumed"],
        "prospective_qualification": result["prospective_qualification"],
        "rejection_reasons": (result["final_rejection_reasons"] if result["final_consumed"]
                              else result["development_rejection_reasons"]
                              if result["development"] is not None else result["train_rejection_reasons"]),
        "network_orders": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

