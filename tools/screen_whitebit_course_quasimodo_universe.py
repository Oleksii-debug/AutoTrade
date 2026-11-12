#!/usr/bin/env python3
"""Evaluate one course strategy across a locked WhiteBIT market universe."""
from __future__ import annotations

import argparse
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

from research.autotrade_research.strategies.course_quasimodo import CourseConfiguration, VERSION, simulate
from tools.screen_whitebit_trend_regime import authenticated_bars


SCHEMA = "autotrade-whitebit-private-course-universe-screen-v1"


def _aggregate(values: dict[str, dict]) -> dict:
    total = lambda key: sum((Decimal(row[key]) for row in values.values()), Decimal(0))
    cycles = sum(row["closed_cycles"] for row in values.values())
    wins = sum(row["wins"] for row in values.values())
    return {"market_count": len(values), "closed_cycles": cycles,
        "wins": wins, "losses": cycles - wins,
        "win_rate": str(Decimal(wins) / Decimal(cycles)) if cycles else None,
        "net_pnl": str(total("net_pnl")), "fees": str(total("fees")),
        "funding": str(total("funding")),
        "markets": {market: {"closed_cycles": row["closed_cycles"],
            "net_pnl": row["net_pnl"], "wins": row["wins"], "losses": row["losses"],
            "exit_reasons": row["exit_reasons"], "evidence_sha256": row["evidence_sha256"]}
            for market, row in sorted(values.items())}}


def _reasons(value: dict, minimum_cycles: int, partition: str) -> list[str]:
    reasons = []
    if value["closed_cycles"] < minimum_cycles:
        reasons.append(f"MINIMUM_{partition}_CYCLES_NOT_MET")
    if Decimal(value["net_pnl"]) <= 0:
        reasons.append(f"{partition}_NO_POSITIVE_AFTER_COST_PNL")
    return reasons


def screen_universe(datasets: list[dict], *, train_count: int, development_count: int,
                    configuration: CourseConfiguration,
                    minimum_train_cycles: int = 20,
                    minimum_development_cycles: int = 5,
                    minimum_final_cycles: int = 5) -> dict:
    if not datasets:
        raise ValueError("at least one authenticated market is required")
    markets = [row["market"] for row in datasets]
    if len(set(markets)) != len(markets):
        raise ValueError("duplicate market in universe")
    if any(train_count + development_count > len(row["bars"]) - 500 for row in datasets):
        raise ValueError("every market needs substantial locked FINAL data")
    configuration.validate()
    train_native = {row["market"]: simulate(row["bars"][:train_count], configuration=configuration)
                    for row in datasets}
    train = _aggregate(train_native)
    train_reasons = _reasons(train, minimum_train_cycles, "TRAIN")
    development_native = None; development = None; development_reasons = []
    if not train_reasons:
        development_native = {row["market"]: simulate(
            row["bars"][train_count:train_count + development_count], configuration=configuration)
            for row in datasets}
        development = _aggregate(development_native)
        development_reasons = _reasons(development, minimum_development_cycles, "DEVELOPMENT")
    final_native = None; final = None; final_reasons = ["PRIOR_PARTITIONS_NOT_QUALIFIED"]
    if development is not None and not development_reasons:
        final_native = {row["market"]: simulate(
            row["bars"][train_count + development_count:], configuration=configuration)
            for row in datasets}
        final = _aggregate(final_native)
        final_reasons = _reasons(final, minimum_final_cycles, "FINAL")
    result = {"schema": SCHEMA, "strategy_schema": VERSION,
        "configuration": configuration.__dict__,
        "dataset_identity_sha256": {row["market"]: row["dataset_identity_sha256"]
            for row in sorted(datasets, key=lambda item: item["market"])},
        "train_count_per_market": train_count,
        "development_count_per_market": development_count,
        "final_count_per_market": {row["market"]: len(row["bars"]) - train_count - development_count
            for row in datasets},
        "minimum_train_cycles": minimum_train_cycles,
        "minimum_development_cycles": minimum_development_cycles,
        "minimum_final_cycles": minimum_final_cycles,
        "train": train, "train_rejection_reasons": train_reasons,
        "development": development, "development_rejection_reasons": development_reasons,
        "final": final, "final_rejection_reasons": final_reasons,
        "development_consumed": development is not None,
        "final_consumed": final is not None,
        "prospective_qualification": final is not None and not final_reasons,
        "canonical_qualification": False, "automatic_promotion": False,
        "network_orders": False, "trading_authority": False,
        "paid_model_calls": 0}
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
    result["result_sha256"] = sha256(encoded.encode()).hexdigest()
    return result


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".course-universe-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True, indent=2); stream.write("\n")
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-root", type=Path, required=True)
    parser.add_argument("--train-count", type=int, required=True)
    parser.add_argument("--development-count", type=int, required=True)
    parser.add_argument("--minimum-shift-bars", type=int, default=1)
    parser.add_argument("--maximum-shift-bars", type=int, default=16)
    parser.add_argument("--shift-lookback", type=int, default=12)
    parser.add_argument("--require-top-down", action="store_true")
    parser.add_argument("--minimum-train-cycles", type=int, default=20)
    parser.add_argument("--minimum-development-cycles", type=int, default=5)
    parser.add_argument("--minimum-final-cycles", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    datasets = []
    for path in sorted(args.manifest_root.rglob("*.manifest.json")):
        bars, identity, manifest = authenticated_bars(path)
        datasets.append({"market": manifest["market"], "bars": bars,
            "dataset_identity_sha256": identity, "manifest": str(path)})
    config = CourseConfiguration(
        minimum_shift_confirmation_bars=args.minimum_shift_bars,
        shift_expiry_bars=args.maximum_shift_bars,
        shift_lookback=args.shift_lookback,
        require_true_top_down=args.require_top_down)
    result = screen_universe(datasets, train_count=args.train_count,
        development_count=args.development_count, configuration=config,
        minimum_train_cycles=args.minimum_train_cycles,
        minimum_development_cycles=args.minimum_development_cycles,
        minimum_final_cycles=args.minimum_final_cycles)
    _atomic(args.output, {"source": {"venue": "WHITEBIT_PUBLIC",
        "manifests": {row["market"]: row["manifest"] for row in datasets},
        "private_course_text_embedded": False}, "result": result})
    print(json.dumps({"output": str(args.output),
        "train": result["train"], "train_rejection_reasons": result["train_rejection_reasons"],
        "development_consumed": result["development_consumed"],
        "development": result["development"],
        "development_rejection_reasons": result["development_rejection_reasons"],
        "final_consumed": result["final_consumed"], "final": result["final"],
        "final_rejection_reasons": result["final_rejection_reasons"],
        "prospective_qualification": result["prospective_qualification"],
        "network_orders": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

