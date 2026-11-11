#!/usr/bin/env python3
"""Select one BTC/ETH relative-strength candidate on authenticated WhiteBIT history.

The configuration grid is exhausted on TRAIN and DEVELOPMENT only.  Exactly
one frozen configuration may then consume FINAL.  This is retrospective ZERO
research and cannot send an order or promote a live champion.
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

from research.autotrade_research.strategies.cross_market_relative_strength import (
    CANDIDATES,
    ExecutionCosts,
    GateConfiguration,
    StrategyConfiguration,
    decimal,
    digest,
    simulate,
    validate_aligned_bars,
)
from tools.screen_whitebit_trend_regime import authenticated_bars


SCHEMA = "whitebit-cross-market-locked-final-v1"


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
    encoded = json.dumps(value, sort_keys=True, indent=2) + "\n"
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def default_grid() -> list[StrategyConfiguration]:
    return [StrategyConfiguration(*values) for values in product(
        (24, 48, 96), (24, 48), ("0.02", "0.03"),
        (24, 48), (3, 6), (12, 24, 48),
    )]


def _by_name(rows: list[dict]) -> dict[str, dict]:
    return {row["candidate"]: row for row in rows}


def _reasons(rows: list[dict], gate: GateConfiguration) -> list[str]:
    values = _by_name(rows)
    candidate = values["CROSS_RELATIVE_STRENGTH"]
    reasons: list[str] = []
    if candidate["closed_cycles"] < gate.minimum_closed_cycles:
        reasons.append("MINIMUM_CLOSED_CYCLES_NOT_MET")
    if Decimal(candidate["max_drawdown_fraction"]) > decimal(gate.maximum_drawdown_fraction):
        reasons.append("MAXIMUM_DRAWDOWN_EXCEEDED")
    candidate_return = Decimal(candidate["return_fraction"])
    for benchmark in ("CASH", "BTC_HOLD", "BTC_MOMENTUM"):
        if candidate_return <= Decimal(values[benchmark]["return_fraction"]):
            reasons.append(f"DID_NOT_BEAT_{benchmark}_AFTER_COSTS")
    return reasons


def _evaluate(bars, strategy, costs) -> list[dict]:
    btc, eth = bars
    return [simulate(btc, eth, candidate=name, costs=costs, strategy=strategy)
            for name in CANDIDATES]


def locked_final_grid_screen(
    btc_bars: list[dict], eth_bars: list[dict], *, train_count: int,
    development_count: int, dataset_sha256: str,
    configurations: list[StrategyConfiguration],
    costs: ExecutionCosts = ExecutionCosts(),
    gate: GateConfiguration = GateConfiguration(),
) -> dict:
    validate_aligned_bars(btc_bars, eth_bars)
    costs.validate(); gate.validate()
    if not configurations or len(configurations) > 1000:
        raise ValueError("configuration grid must contain 1..1000 entries")
    if train_count < 4 or development_count < 4 or train_count + development_count > len(btc_bars)-4:
        raise ValueError("TRAIN, DEVELOPMENT and FINAL need at least four bars")
    if len(dataset_sha256) != 64 or any(c not in "0123456789abcdef" for c in dataset_sha256):
        raise ValueError("dataset identity must be lowercase SHA-256")
    unique: dict[str, StrategyConfiguration] = {}
    for strategy in configurations:
        strategy.validate()
        identity = digest(asdict(strategy))
        if identity in unique:
            raise ValueError("configuration grid contains duplicates")
        unique[identity] = strategy
    partitions = {
        "TRAIN": (btc_bars[:train_count], eth_bars[:train_count]),
        "DEVELOPMENT": (
            btc_bars[train_count:train_count+development_count],
            eth_bars[train_count:train_count+development_count],
        ),
        "FINAL": (btc_bars[train_count+development_count:],
                  eth_bars[train_count+development_count:]),
    }
    screened = []
    for identity, strategy in sorted(unique.items()):
        train = _evaluate(partitions["TRAIN"], strategy, costs)
        train_reasons = _reasons(train, gate)
        development = [] if train_reasons else _evaluate(partitions["DEVELOPMENT"], strategy, costs)
        development_reasons = [] if train_reasons else _reasons(development, gate)
        score = None
        if not train_reasons and not development_reasons:
            excesses = []
            for rows in (train, development):
                values = _by_name(rows)
                benchmark = max(Decimal(values[name]["return_fraction"])
                                for name in ("CASH", "BTC_HOLD", "BTC_MOMENTUM"))
                excesses.append(Decimal(values["CROSS_RELATIVE_STRENGTH"]["return_fraction"])-benchmark)
            score = str(min(excesses))
        screened.append({"configuration_sha256": identity,
            "configuration": asdict(strategy), "train": train,
            "train_rejection_reasons": train_reasons,
            "development": development,
            "development_rejection_reasons": development_reasons,
            "selection_score": score})
    eligible = [row for row in screened if row["selection_score"] is not None]
    selected = max(eligible, key=lambda row: (Decimal(row["selection_score"]),
        row["configuration_sha256"])) if eligible else None
    final = []
    final_reasons = ["NO_CONFIGURATION_PASSED_TRAIN_AND_DEVELOPMENT"]
    if selected:
        strategy = StrategyConfiguration(**selected["configuration"])
        final = _evaluate(partitions["FINAL"], strategy, costs)
        final_reasons = _reasons(final, gate)
    qualified = bool(selected and not final_reasons)
    result = {"schema": SCHEMA, "evidence_class": "ZERO_RETROSPECTIVE_RESEARCH",
        "dataset_sha256": dataset_sha256, "train_count": train_count,
        "development_count": development_count,
        "final_count": len(btc_bars)-train_count-development_count,
        "costs": asdict(costs), "gate": asdict(gate),
        "grid_sha256": digest([asdict(unique[key]) for key in sorted(unique)]),
        "configuration_count": len(unique), "screened": screened,
        "selected_configuration": None if selected is None else selected["configuration"],
        "selected_configuration_sha256": None if selected is None else selected["configuration_sha256"],
        "final": final, "final_rejection_reasons": final_reasons,
        "final_consumed": bool(final), "prospective_qualification": qualified,
        "selected_candidate": "CROSS_RELATIVE_STRENGTH" if qualified else "CASH",
        "canonical_qualification": False, "network_orders": False,
        "trading_authority": False, "paid_model_calls": 0}
    result["result_sha256"] = digest(result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--btc-manifest", type=Path, required=True)
    parser.add_argument("--eth-manifest", type=Path, required=True)
    parser.add_argument("--train-count", type=int, required=True)
    parser.add_argument("--development-count", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--minimum-closed-cycles", type=int, default=8)
    parser.add_argument("--maximum-drawdown-fraction", default="0.35")
    args = parser.parse_args(argv)
    btc, btc_digest, btc_manifest = authenticated_bars(args.btc_manifest)
    eth, eth_digest, eth_manifest = authenticated_bars(args.eth_manifest)
    if btc_manifest["market"] != "BTC_USDT" or eth_manifest["market"] != "ETH_USDT":
        raise ValueError("WhiteBIT manifests must be BTC_USDT and ETH_USDT")
    source = {"venue": "WHITEBIT_SPOT", "interval": "1h",
        "btc_dataset_sha256": btc_digest, "eth_dataset_sha256": eth_digest,
        "btc_manifest": str(args.btc_manifest), "eth_manifest": str(args.eth_manifest)}
    result = locked_final_grid_screen(btc, eth,
        train_count=args.train_count, development_count=args.development_count,
        dataset_sha256=digest(source), configurations=default_grid(),
        gate=GateConfiguration(args.minimum_closed_cycles, args.maximum_drawdown_fraction))
    artifact = {"source": source, "result": result}
    target = args.output_dir / f"{result['result_sha256']}.json"
    _atomic_json(target, artifact)
    print(json.dumps({"result_path": str(target), "phase": (
        "FINAL_PASS" if result["prospective_qualification"] else
        "FINAL_FAIL" if result["final_consumed"] else "REJECTED_BEFORE_FINAL"),
        "configuration_count": result["configuration_count"],
        "selected_configuration_sha256": result["selected_configuration_sha256"],
        "final_rejection_reasons": result["final_rejection_reasons"],
        "network_orders": False, "paid_model_calls": 0}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
