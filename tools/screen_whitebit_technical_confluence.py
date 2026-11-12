#!/usr/bin/env python3
"""Locked-final technical-indicator grid on authenticated WhiteBIT candles."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from decimal import Decimal
from itertools import product
import json
import os
from pathlib import Path
import tempfile

from research.autotrade_research.strategies.technical_confluence import (
    CANDIDATES, TechnicalConfiguration, TechnicalCosts, digest, simulate,
)
from research.autotrade_research.strategies.fast_screen import validate_bars
from tools.screen_whitebit_trend_regime import authenticated_bars


SCHEMA = "autotrade-whitebit-technical-confluence-locked-final-v1"
GATE_POLICIES = ("BEAT_ALL_CONTROLS", "POSITIVE_AFTER_COSTS")


def default_grid() -> tuple[TechnicalConfiguration, ...]:
    values = []
    for fast, slow, gap, cooldown in product(
            (8, 12), (32, 48), ("0.0015", "0.003"), (8, 24)):
        values.append(TechnicalConfiguration(
            family="EMA_RSI_TREND", fast_ema=fast, slow_ema=slow,
            stop_atr="1", take_profit_atr="4", maximum_hold_bars=120,
            minimum_ema_gap_fraction=gap, minimum_expected_move_fraction="0.003",
            cooldown_bars=cooldown))
    for period, width, expected in product(
            (20, 40), ("1.5", "2"), ("0.003", "0.006")):
        values.append(TechnicalConfiguration(
            family="BOLLINGER_RSI_REVERSION", fast_ema=8, slow_ema=32,
            rsi_long_min="35", rsi_short_max="65", bollinger_period=period,
            bollinger_width=width, stop_atr="1", take_profit_atr="4",
            minimum_expected_move_fraction=expected, cooldown_bars=8,
            minimum_hold_bars=2, maximum_hold_bars=60))
    return tuple(values)


def intraday_trend_configuration() -> TechnicalConfiguration:
    """One fixed trend control with the same 30–60 minute risk envelope."""
    return TechnicalConfiguration(family="EMA_RSI_TREND", fast_ema=8, slow_ema=32,
        stop_atr="1", take_profit_atr="4", cooldown_bars=4,
        minimum_expected_move_fraction="0.006", minimum_hold_bars=2,
        maximum_hold_bars=4, maximum_entries_per_utc_day=1)


def _reasons(candidate: dict, controls: list[dict], *, stage: str,
             minimum_cycles: int, maximum_drawdown: Decimal,
             gate_policy: str) -> list[str]:
    reasons = []
    if candidate["closed_cycles"] < minimum_cycles:
        reasons.append(f"MINIMUM_{stage}_CYCLES_NOT_MET")
    if gate_policy == "BEAT_ALL_CONTROLS":
        if Decimal(candidate["net_pnl"]) <= max(Decimal(row["net_pnl"]) for row in controls):
            reasons.append(f"{stage}_DID_NOT_BEAT_CASH_LONG_AND_SHORT_CONTROLS")
    elif gate_policy == "POSITIVE_AFTER_COSTS":
        if Decimal(candidate["net_pnl"]) <= 0:
            reasons.append(f"{stage}_NO_POSITIVE_AFTER_COST_PNL")
    else:
        raise ValueError("unsupported technical gate policy")
    if Decimal(candidate["maximum_drawdown_fraction"]) > maximum_drawdown:
        reasons.append(f"{stage}_DRAWDOWN_LIMIT_EXCEEDED")
    return reasons


def locked_final_screen(bars: list[dict], *, train_count: int,
        development_count: int, dataset_sha256: str,
        configurations: tuple[TechnicalConfiguration, ...],
        costs: TechnicalCosts = TechnicalCosts(), minimum_cycles: int = 10,
        maximum_drawdown_fraction: str = "0.20",
        gate_policy: str = "BEAT_ALL_CONTROLS", report_hourly_returns: bool = False,
        protective_stop_intrabar: bool = False) -> dict:
    validate_bars(bars, require_ohlcv=True); costs.validate()
    if not 1 <= len(configurations) <= 128:
        raise ValueError("technical grid must contain 1..128 configurations")
    if train_count < 100 or development_count < 100 or train_count + development_count > len(bars) - 100:
        raise ValueError("TRAIN, DEVELOPMENT and FINAL require at least 100 bars")
    if len(dataset_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in dataset_sha256):
        raise ValueError("dataset identity must be lowercase SHA-256")
    maximum = Decimal(maximum_drawdown_fraction)
    if not Decimal(0) <= maximum <= Decimal(1):
        raise ValueError("maximum drawdown must be in [0,1]")
    if gate_policy not in GATE_POLICIES:
        raise ValueError("unsupported technical gate policy")
    unique = {}
    for config in configurations:
        config.validate(); identity = digest(asdict(config))
        if identity in unique:
            raise ValueError("duplicate technical configuration")
        unique[identity] = config
    parts = {"TRAIN": bars[:train_count],
             "DEVELOPMENT": bars[train_count:train_count + development_count],
             "FINAL": bars[train_count + development_count:]}
    baseline = unique[sorted(unique)[0]]
    controls = {stage: [simulate(parts[stage], candidate=name, costs=costs,
                                 configuration=baseline) for name in CANDIDATES[:-1]]
                for stage in ("TRAIN", "DEVELOPMENT")}
    screened = []
    for identity, config in sorted(unique.items()):
        train = simulate(parts["TRAIN"], candidate=CANDIDATES[-1], costs=costs,
                         configuration=config, report_hourly_returns=report_hourly_returns,
                         protective_stop_intrabar=protective_stop_intrabar)
        train_reasons = _reasons(train, controls["TRAIN"], stage="TRAIN",
            minimum_cycles=minimum_cycles, maximum_drawdown=maximum,
            gate_policy=gate_policy)
        development = None; development_reasons = []; score = None
        if not train_reasons:
            development = simulate(parts["DEVELOPMENT"], candidate=CANDIDATES[-1],
                                   costs=costs, configuration=config,
                                   report_hourly_returns=report_hourly_returns,
                                   protective_stop_intrabar=protective_stop_intrabar)
            development_reasons = _reasons(development, controls["DEVELOPMENT"],
                stage="DEVELOPMENT", minimum_cycles=minimum_cycles,
                maximum_drawdown=maximum, gate_policy=gate_policy)
            if not development_reasons:
                score = str(min(
                    Decimal(train["net_pnl"]) - max(Decimal(x["net_pnl"]) for x in controls["TRAIN"]),
                    Decimal(development["net_pnl"]) - max(Decimal(x["net_pnl"]) for x in controls["DEVELOPMENT"])))
        screened.append({"configuration_sha256": identity,
            "configuration": asdict(config), "train": train,
            "train_rejection_reasons": train_reasons, "development": development,
            "development_rejection_reasons": development_reasons,
            "selection_score_quote": score})
    eligible = [row for row in screened if row["selection_score_quote"] is not None]
    selected = max(eligible, key=lambda row: (
        Decimal(row["selection_score_quote"]), row["configuration_sha256"])) if eligible else None
    final = []; final_reasons = ["NO_CONFIGURATION_PASSED_TRAIN_AND_DEVELOPMENT"]
    if selected:
        config = TechnicalConfiguration(**selected["configuration"])
        final = [simulate(parts["FINAL"], candidate=name, costs=costs,
                          configuration=config,
                          report_hourly_returns=report_hourly_returns and name == CANDIDATES[-1],
                          protective_stop_intrabar=protective_stop_intrabar and name == CANDIDATES[-1])
                 for name in CANDIDATES]
        candidate = final[-1]
        final_reasons = _reasons(candidate, final[:-1], stage="FINAL",
            minimum_cycles=minimum_cycles, maximum_drawdown=maximum,
            gate_policy=gate_policy)
    qualified = bool(selected and not final_reasons)
    result = {"schema": SCHEMA, "dataset_identity_sha256": dataset_sha256,
        "train_count": train_count, "development_count": development_count,
        "final_count": len(parts["FINAL"]), "costs": asdict(costs),
        "round_trip_cost_bps": str(costs.round_trip_bps),
        "minimum_cycles": minimum_cycles,
        "gate_policy": gate_policy,
        "maximum_drawdown_fraction": str(maximum),
        "configuration_count": len(unique), "controls": controls,
        "screened": screened,
        "selected_configuration_sha256": None if selected is None else selected["configuration_sha256"],
        "selected_configuration": None if selected is None else selected["configuration"],
        "final": final, "final_consumed": bool(final),
        "final_rejection_reasons": final_reasons,
        "prospective_qualification": qualified,
        "selected_candidate": CANDIDATES[-1] if qualified else "CASH",
        "course_translation": {
            "source": "CME_TECHNICAL_ANALYSIS_COURSE",
            "indicators": ["EMA", "RSI", "BOLLINGER_BANDS", "ATR"],
            "rules_are_formal_not_discretionary": True},
        "canonical_qualification": False, "network_orders": False,
        "trading_authority": False, "paid_model_calls": 0}
    if protective_stop_intrabar:
        result["execution_model"] = screened[0]["train"]["execution_model"]
    result["result_sha256"] = digest(result)
    return result


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".technical-grid-", dir=path.parent)
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
    parser.add_argument("--gate-policy", choices=GATE_POLICIES,
                        default="BEAT_ALL_CONTROLS")
    parser.add_argument("--intraday-trend", action="store_true",
                        help="replay one fixed 30–60 minute EMA/RSI trend control on M15")
    args = parser.parse_args(argv)
    bars, dataset, manifest = authenticated_bars(args.manifest)
    if args.intraday_trend and manifest["interval"] != "15m":
        parser.error("--intraday-trend requires a 15-minute manifest")
    configurations = (intraday_trend_configuration(),) if args.intraday_trend else default_grid()
    result = locked_final_screen(bars, train_count=args.train_count,
        development_count=args.development_count, dataset_sha256=dataset,
        configurations=configurations, gate_policy=args.gate_policy,
        minimum_cycles=5 if args.intraday_trend else 10,
        report_hourly_returns=args.intraday_trend)
    target = args.output_dir / f"{result['result_sha256']}.json"
    _atomic(target, {"source": {"venue": "WHITEBIT_PUBLIC", "market": manifest["market"],
        "dataset_identity_sha256": dataset, "manifest": str(args.manifest)}, "result": result})
    print(json.dumps({"result_path": str(target),
        "configuration_count": result["configuration_count"],
        "gate_policy": result["gate_policy"],
        "selected_configuration_sha256": result["selected_configuration_sha256"],
        "final_consumed": result["final_consumed"],
        "prospective_qualification": result["prospective_qualification"],
        "final_rejection_reasons": result["final_rejection_reasons"],
        "network_orders": False, "paid_model_calls": 0}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
