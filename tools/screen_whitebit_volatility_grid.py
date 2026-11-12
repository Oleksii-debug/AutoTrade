#!/usr/bin/env python3
"""Locked-final WhiteBIT spot volatility/volume breakout grid.

Benchmarks are computed once per partition.  Candidate configurations consume
TRAIN and only eligible candidates consume DEVELOPMENT; one frozen winner may
consume FINAL.  The artifact is retrospective ZERO research with no order or
promotion authority.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from decimal import Decimal
from itertools import product
import json
import os
from pathlib import Path
from uuid import uuid4

from research.autotrade_research.strategies.volatility_expansion_breakout import (
    ExecutionCosts, GateConfiguration, StrategyConfiguration, digest, simulate,
    validate_bars,
)
from tools.screen_whitebit_trend_regime import authenticated_bars


SCHEMA = "whitebit-volatility-grid-locked-final-v1"


def default_grid() -> list[StrategyConfiguration]:
    result = []
    for breakout, expansion, minimum_range, volume_lookback, volume_multiplier, stop in product(
            (24, 72, 168), ("1.25", "1.75"), ("0.003", "0.008"),
            (24, 72), ("1.25", "2"), ("0.03", "0.06")):
        result.append(StrategyConfiguration(breakout_lookback=breakout,
            volatility_lookback=24, range_expansion_multiplier=expansion,
            minimum_range_fraction=minimum_range, volume_lookback=volume_lookback,
            volume_multiplier=volume_multiplier, minimum_mean_quote_volume="100000",
            breakout_buffer_bps="5", stop_loss_fraction=stop, exit_lookback=24,
            minimum_hold_bars=6, maximum_hold_bars=168))
    return result


def _reasons(candidate, cash, hold, gate):
    reasons = []
    if candidate["closed_cycles"] < gate.minimum_closed_cycles:
        reasons.append("MINIMUM_CLOSED_CYCLES_NOT_MET")
    if Decimal(candidate["return_fraction"]) <= Decimal(cash["return_fraction"]):
        reasons.append("DID_NOT_BEAT_CASH_AFTER_COSTS")
    if gate.require_beats_hold and Decimal(candidate["return_fraction"]) <= Decimal(hold["return_fraction"]):
        reasons.append("DID_NOT_BEAT_HOLD_AFTER_COSTS")
    if Decimal(candidate["max_drawdown_fraction"]) > Decimal(gate.maximum_drawdown_fraction):
        reasons.append("MAXIMUM_DRAWDOWN_EXCEEDED")
    return reasons


def locked_final_grid_screen(bars, *, train_count, development_count,
        dataset_sha256, configurations, costs=ExecutionCosts(),
        gate=GateConfiguration(maximum_drawdown_fraction="0.35")):
    validate_bars(bars); costs.validate(); gate.validate()
    if not configurations or len(configurations) > 1000:
        raise ValueError("configuration grid must contain 1..1000 entries")
    if train_count < 4 or development_count < 4 or train_count+development_count > len(bars)-4:
        raise ValueError("TRAIN, DEVELOPMENT and FINAL need at least four bars")
    if len(dataset_sha256) != 64 or any(c not in "0123456789abcdef" for c in dataset_sha256):
        raise ValueError("dataset identity must be lowercase SHA-256")
    unique = {}
    for strategy in configurations:
        strategy.validate()
        key = digest(asdict(strategy))
        if key in unique:
            raise ValueError("configuration grid contains duplicates")
        unique[key] = strategy
    parts = {"TRAIN": bars[:train_count],
        "DEVELOPMENT": bars[train_count:train_count+development_count],
        "FINAL": bars[train_count+development_count:]}
    baseline_strategy = unique[sorted(unique)[0]]
    benchmarks = {}
    for label in ("TRAIN", "DEVELOPMENT"):
        benchmarks[label] = {
            name: simulate(parts[label], candidate=name, costs=costs,
                strategy=baseline_strategy) for name in ("CASH", "BTC_HOLD")}
    screened = []
    for key, strategy in sorted(unique.items()):
        train = simulate(parts["TRAIN"], candidate="VOLATILITY_EXPANSION_BREAKOUT",
            costs=costs, strategy=strategy)
        train_reasons = _reasons(train, benchmarks["TRAIN"]["CASH"],
            benchmarks["TRAIN"]["BTC_HOLD"], gate)
        development = None; development_reasons = []
        if not train_reasons:
            development = simulate(parts["DEVELOPMENT"],
                candidate="VOLATILITY_EXPANSION_BREAKOUT", costs=costs, strategy=strategy)
            development_reasons = _reasons(development,
                benchmarks["DEVELOPMENT"]["CASH"], benchmarks["DEVELOPMENT"]["BTC_HOLD"], gate)
        score = None
        if development is not None and not development_reasons:
            score = str(min(
                Decimal(train["return_fraction"])-max(Decimal(x["return_fraction"])
                    for x in benchmarks["TRAIN"].values()),
                Decimal(development["return_fraction"])-max(Decimal(x["return_fraction"])
                    for x in benchmarks["DEVELOPMENT"].values())))
        screened.append({"configuration_sha256": key, "configuration": asdict(strategy),
            "train": train, "train_rejection_reasons": train_reasons,
            "development": development,
            "development_rejection_reasons": development_reasons,
            "selection_score": score})
    eligible = [row for row in screened if row["selection_score"] is not None]
    selected = max(eligible, key=lambda row: (Decimal(row["selection_score"]),
        row["configuration_sha256"])) if eligible else None
    final = []; final_reasons = ["NO_CONFIGURATION_PASSED_TRAIN_AND_DEVELOPMENT"]
    if selected:
        strategy = StrategyConfiguration(**selected["configuration"])
        final = [simulate(parts["FINAL"], candidate=name, costs=costs, strategy=strategy)
                 for name in ("CASH", "BTC_HOLD", "VOLATILITY_EXPANSION_BREAKOUT")]
        values = {row["candidate"]: row for row in final}
        final_reasons = _reasons(values["VOLATILITY_EXPANSION_BREAKOUT"],
            values["CASH"], values["BTC_HOLD"], gate)
    qualified = bool(selected and not final_reasons)
    result = {"schema": SCHEMA, "evidence_class": "ZERO_RETROSPECTIVE_RESEARCH",
        "dataset_sha256": dataset_sha256, "train_count": train_count,
        "development_count": development_count,
        "final_count": len(bars)-train_count-development_count,
        "costs": asdict(costs), "gate": asdict(gate),
        "grid_sha256": digest([asdict(unique[key]) for key in sorted(unique)]),
        "configuration_count": len(unique), "benchmarks": benchmarks,
        "screened": screened,
        "selected_configuration": None if selected is None else selected["configuration"],
        "selected_configuration_sha256": None if selected is None else selected["configuration_sha256"],
        "final": final, "final_rejection_reasons": final_reasons,
        "final_consumed": bool(final), "prospective_qualification": qualified,
        "selected_candidate": "VOLATILITY_EXPANSION_BREAKOUT" if qualified else "CASH",
        "canonical_qualification": False, "network_orders": False,
        "trading_authority": False, "paid_model_calls": 0}
    result["result_sha256"] = digest(result)
    return result


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
    with tmp.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2)+"\n")
        stream.flush(); os.fsync(stream.fileno())
    os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--train-count", type=int, required=True)
    parser.add_argument("--development-count", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    bars, dataset_sha256, manifest = authenticated_bars(args.manifest)
    market = manifest["market"]
    if not market.endswith("_USDT"):
        raise ValueError("locked volatility grid requires a USDT spot market")
    result = locked_final_grid_screen(bars, train_count=args.train_count,
        development_count=args.development_count, dataset_sha256=dataset_sha256,
        configurations=default_grid())
    target = args.output_dir / f"{result['result_sha256']}.json"
    _atomic(target, {"source": {"venue": "WHITEBIT_SPOT", "market": market,
        "dataset_identity_sha256": dataset_sha256, "manifest": str(args.manifest)},
        "result": result})
    print(json.dumps({"result_path": str(target),
        "configuration_count": result["configuration_count"],
        "selected_configuration_sha256": result["selected_configuration_sha256"],
        "final_consumed": result["final_consumed"],
        "prospective_qualification": result["prospective_qualification"],
        "final_rejection_reasons": result["final_rejection_reasons"],
        "network_orders": False, "paid_model_calls": 0}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

