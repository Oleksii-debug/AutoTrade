#!/usr/bin/env python3
"""Locked-final grid for costed long/short trend regimes on WhiteBIT history."""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from hashlib import sha256
import argparse
import json
import os
from pathlib import Path
import tempfile

from research.autotrade_research.strategies.fast_screen import Costs, digest, validate_bars
from research.autotrade_research.strategies.trend_regime import (
    CANDIDATES,
    TrendRegime,
    simulate,
)
from tools.screen_whitebit_trend_regime import authenticated_bars


SCHEMA = "autotrade-whitebit-trend-locked-final-grid-v1"


def default_grid() -> tuple[TrendRegime, ...]:
    common = dict(allow_short=True, short_carry_bps_per_day="2")
    return (
        TrendRegime(fast_lookback=24, slow_lookback=168,
                    entry_threshold="0.004", slow_threshold="0.01",
                    exit_threshold="0.004", trailing_stop_fraction="0.03",
                    min_hold_bars=6, max_hold_bars=336,
                    short_entry_threshold="0.015", short_slow_threshold="0.03", **common),
        TrendRegime(fast_lookback=72, slow_lookback=336,
                    entry_threshold="0.01", slow_threshold="0.02",
                    exit_threshold="0.008", trailing_stop_fraction="0.05",
                    min_hold_bars=12, max_hold_bars=504,
                    short_entry_threshold="0.02", short_slow_threshold="0.04", **common),
        TrendRegime(fast_lookback=168, slow_lookback=720,
                    entry_threshold="0.03", slow_threshold="0.05",
                    exit_threshold="0.01", trailing_stop_fraction="0.07",
                    min_hold_bars=24, max_hold_bars=1000,
                    short_entry_threshold="0.02", short_slow_threshold="0.04", **common),
        TrendRegime(fast_lookback=24, slow_lookback=336,
                    entry_threshold="0.008", slow_threshold="0.02",
                    exit_threshold="0.006", trailing_stop_fraction="0.04",
                    min_hold_bars=6, max_hold_bars=336,
                    short_entry_threshold="0.012", short_slow_threshold="0.025", **common),
        TrendRegime(fast_lookback=72, slow_lookback=720,
                    entry_threshold="0.015", slow_threshold="0.04",
                    exit_threshold="0.008", trailing_stop_fraction="0.06",
                    min_hold_bars=24, max_hold_bars=720,
                    short_entry_threshold="0.018", short_slow_threshold="0.04", **common),
        TrendRegime(fast_lookback=168, slow_lookback=336,
                    entry_threshold="0.02", slow_threshold="0.03",
                    exit_threshold="0.01", trailing_stop_fraction="0.05",
                    min_hold_bars=12, max_hold_bars=504,
                    short_entry_threshold="0.02", short_slow_threshold="0.035", **common),
    )


def _reasons(candidate: dict, controls: list[dict], *, min_cycles: int,
             max_drawdown: Decimal, stage: str) -> list[str]:
    reasons = []
    if candidate["closed_cycles"] < min_cycles:
        reasons.append(f"MINIMUM_{stage}_CYCLES_NOT_MET")
    if Decimal(candidate["net_pnl"]) <= max(Decimal(row["net_pnl"]) for row in controls):
        reasons.append(f"{stage}_DID_NOT_BEAT_CASH_AND_HOLD")
    if Decimal(candidate["max_drawdown_fraction"]) > max_drawdown:
        reasons.append(f"{stage}_DRAWDOWN_LIMIT_EXCEEDED")
    return reasons


def locked_final_grid_screen(bars: list[dict], *, train_count: int,
        development_count: int, dataset_sha256: str,
        configurations: tuple[TrendRegime, ...], costs: Costs = Costs(),
        min_cycles: int = 3, max_drawdown_fraction: str = "0.25") -> dict:
    validate_bars(bars, require_ohlcv=True); costs.validate()
    if not 1 <= len(configurations) <= 64:
        raise ValueError("trend grid must contain 1..64 configurations")
    if (train_count < 4 or development_count < 4
            or train_count + development_count > len(bars) - 4):
        raise ValueError("TRAIN, DEVELOPMENT and FINAL require at least four bars")
    if len(dataset_sha256) != 64 or any(c not in "0123456789abcdef" for c in dataset_sha256):
        raise ValueError("dataset identity must be lowercase SHA-256")
    maximum = Decimal(max_drawdown_fraction)
    if not Decimal(0) <= maximum <= Decimal(1):
        raise ValueError("maximum drawdown must be 0..1")
    unique = {}
    for config in configurations:
        config.validate()
        identity = digest(asdict(config))
        if identity in unique:
            raise ValueError("duplicate trend configuration")
        unique[identity] = config
    parts = {
        "TRAIN": bars[:train_count],
        "DEVELOPMENT": bars[train_count:train_count + development_count],
        "FINAL": bars[train_count + development_count:],
    }
    baseline = unique[sorted(unique)[0]]
    controls = {
        stage: [simulate(parts[stage], candidate=name, costs=costs, configuration=baseline)
                for name in ("CASH", "HOLD")]
        for stage in ("TRAIN", "DEVELOPMENT")
    }
    screened = []
    for identity, config in sorted(unique.items()):
        train = simulate(parts["TRAIN"], candidate=CANDIDATES[-1],
                         costs=costs, configuration=config)
        train_reasons = _reasons(train, controls["TRAIN"], min_cycles=min_cycles,
                                 max_drawdown=maximum, stage="TRAIN")
        development = None; development_reasons = []; score = None
        if not train_reasons:
            development = simulate(parts["DEVELOPMENT"], candidate=CANDIDATES[-1],
                                   costs=costs, configuration=config)
            development_reasons = _reasons(
                development, controls["DEVELOPMENT"], min_cycles=min_cycles,
                max_drawdown=maximum, stage="DEVELOPMENT")
            if not development_reasons:
                train_edge = Decimal(train["net_pnl"]) - max(
                    Decimal(row["net_pnl"]) for row in controls["TRAIN"])
                dev_edge = Decimal(development["net_pnl"]) - max(
                    Decimal(row["net_pnl"]) for row in controls["DEVELOPMENT"])
                score = str(min(train_edge, dev_edge))
        screened.append({
            "configuration_sha256": identity, "configuration": asdict(config),
            "train": train, "train_rejection_reasons": train_reasons,
            "development": development,
            "development_rejection_reasons": development_reasons,
            "selection_score_quote": score,
        })
    eligible = [row for row in screened if row["selection_score_quote"] is not None]
    selected = max(eligible, key=lambda row: (
        Decimal(row["selection_score_quote"]), row["configuration_sha256"])) if eligible else None
    final = []; final_reasons = ["NO_CONFIGURATION_PASSED_TRAIN_AND_DEVELOPMENT"]
    if selected is not None:
        config = TrendRegime(**selected["configuration"])
        final = [simulate(parts["FINAL"], candidate=name, costs=costs, configuration=config)
                 for name in CANDIDATES]
        candidate = next(row for row in final if row["candidate"] == CANDIDATES[-1])
        final_reasons = _reasons(
            candidate, [row for row in final if row["candidate"] != CANDIDATES[-1]],
            min_cycles=min_cycles, max_drawdown=maximum, stage="FINAL")
    qualified = selected is not None and not final_reasons
    result = {
        "schema": SCHEMA, "dataset_identity_sha256": dataset_sha256,
        "train_count": train_count, "development_count": development_count,
        "final_count": len(parts["FINAL"]), "costs": asdict(costs),
        "min_cycles": min_cycles, "max_drawdown_fraction": str(maximum),
        "configuration_count": len(unique), "screened": screened,
        "selected_configuration_sha256": None if selected is None else selected["configuration_sha256"],
        "selected_configuration": None if selected is None else selected["configuration"],
        "final": final, "final_consumed": bool(final),
        "final_rejection_reasons": final_reasons,
        "prospective_qualification": qualified,
        "selected_candidate": CANDIDATES[-1] if qualified else "CASH",
        "canonical_qualification": False, "network_orders": False,
        "trading_authority": False, "paid_model_calls": 0,
    }
    result["result_sha256"] = digest(result)
    return result


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".trend-grid-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
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
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    bars, dataset, manifest = authenticated_bars(args.manifest)
    result = locked_final_grid_screen(
        bars, train_count=args.train_count, development_count=args.development_count,
        dataset_sha256=dataset, configurations=default_grid())
    target = args.output_dir / f"{result['result_sha256']}.json"
    _atomic(target, {"source": {"venue": "WHITEBIT_SPOT", "market": manifest["market"],
        "dataset_identity_sha256": dataset, "manifest": str(args.manifest)}, "result": result})
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

