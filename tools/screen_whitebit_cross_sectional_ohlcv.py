#!/usr/bin/env python3
"""Locked-final pooled OHLCV ranking model across WhiteBIT spot markets.

One market-agnostic model is trained on every supplied asset.  At each causal
decision it may hold cash or select only the highest predicted market, so
parallel overlapping bets cannot manufacture returns.  TRAIN and DEVELOPMENT
select a fixed confidence threshold; FINAL is read once only after both pass.
"""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import numpy as np
from threadpoolctl import threadpool_limits
from xgboost import XGBRegressor

from tools.screen_whitebit_ohlcv_alpha import FEATURE_NAMES, build_dataset
from tools.screen_whitebit_trend_regime import authenticated_bars


SCHEMA = "whitebit-cross-sectional-ohlcv-locked-final-v1"


def aligned_panel(series: list[tuple[str, list[dict]]], *, horizon: int) -> dict:
    if not 3 <= len(series) <= 50 or len({market for market, _bars in series}) != len(series):
        raise ValueError("three to fifty distinct markets required")
    datasets = [build_dataset(bars, horizon=horizon) for _market, bars in series]
    reference = datasets[0]
    for dataset in datasets[1:]:
        if (not np.array_equal(dataset["decision_bar"], reference["decision_bar"])
                or any(not np.array_equal(dataset[name], reference[name])
                       for name in ("train", "development", "final"))):
            raise ValueError("market datasets are not causally aligned")
    for index in range(len(series)-1):
        left, right = series[index][1], series[index+1][1]
        if len(left) != len(right) or any(
                a["opened_at"] != b["opened_at"] or a["closed_at"] != b["closed_at"]
                for a, b in zip(left, right)):
            raise ValueError("market candle timestamps are not synchronized")
    return {"markets": [market for market, _bars in series],
        "x": np.stack([dataset["x"] for dataset in datasets], axis=1),
        "y": np.stack([dataset["y"] for dataset in datasets], axis=1),
        "decision_bar": reference["decision_bar"],
        "train": reference["train"], "development": reference["development"],
        "final": reference["final"], "horizon": horizon}


def simulate_panel(prediction: np.ndarray, panel: dict, indices: np.ndarray,
        *, threshold: float, round_trip_cost_ratio: float) -> dict:
    if prediction.shape != panel["y"].shape:
        raise ValueError("prediction panel shape differs")
    equity = peak = 1.0
    maximum_drawdown = 0.0
    trades = wins = 0
    gross_total = cost_total = 0.0
    selected = Counter()
    next_decision_bar = -1
    for row in indices:
        decision_bar = int(panel["decision_bar"][row])
        if decision_bar < next_decision_bar:
            continue
        market_index = int(np.argmax(prediction[row]))
        estimate = float(prediction[row, market_index])
        if estimate < threshold:
            continue
        gross = float(panel["y"][row, market_index])
        net = gross-round_trip_cost_ratio
        equity *= max(1.0+net, 1e-12)
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, (peak-equity)/peak)
        trades += 1; wins += int(net > 0)
        gross_total += gross; cost_total += round_trip_cost_ratio
        selected[panel["markets"][market_index]] += 1
        next_decision_bar = decision_bar+panel["horizon"]+1
    return {"trade_count": trades, "winning_trade_count": wins,
        "gross_return_sum": format(gross_total, ".12f"),
        "explicit_cost_sum": format(cost_total, ".12f"),
        "net_return_fraction": format(equity-1.0, ".12f"),
        "maximum_drawdown_fraction": format(maximum_drawdown, ".12f"),
        "win_rate": format(wins/trades if trades else 0.0, ".12f"),
        "market_trade_counts": dict(sorted(selected.items()))}


def _passes(report: dict, *, minimum_trades: int, maximum_drawdown: float) -> bool:
    return (report["trade_count"] >= minimum_trades
            and float(report["net_return_fraction"]) > 0
            and float(report["maximum_drawdown_fraction"]) <= maximum_drawdown)


def screen_panel(series: list[tuple[str, list[dict]]], *, horizon: int,
        round_trip_cost_bps: float = 26.0, minimum_trades: int = 8,
        maximum_drawdown: float = 0.20, seed: int = 29,
        target_mode: str = "CROSS_SECTIONAL_EXCESS") -> dict:
    if target_mode not in ("ABSOLUTE_RETURN", "CROSS_SECTIONAL_EXCESS"):
        raise ValueError("unknown panel target mode")
    panel = aligned_panel(series, horizon=horizon)
    model = XGBRegressor(n_estimators=192, max_depth=3, learning_rate=0.035,
        subsample=0.8, colsample_bytree=0.85, min_child_weight=16,
        reg_lambda=3.0, objective="reg:squarederror", tree_method="hist",
        n_jobs=1, random_state=seed)
    train_x = panel["x"][panel["train"]].reshape(-1, len(FEATURE_NAMES))
    targets = panel["y"]
    if target_mode == "CROSS_SECTIONAL_EXCESS":
        targets = targets-np.median(targets, axis=1, keepdims=True)
    train_y = targets[panel["train"]].reshape(-1)
    with threadpool_limits(limits=1):
        model.fit(train_x, train_y)
        prediction = model.predict(panel["x"].reshape(-1, len(FEATURE_NAMES))).reshape(
            panel["y"].shape)
    top_train = np.max(prediction[panel["train"]], axis=1)
    cost = round_trip_cost_bps/10000.0
    prediction_floor = cost if target_mode == "ABSOLUTE_RETURN" else 0.0
    thresholds = sorted(set(max(prediction_floor, float(np.quantile(top_train, q)))
        for q in (0.50, 0.60, 0.70, 0.80, 0.875, 0.925, 0.95, 0.975)))
    candidates = []
    for threshold in thresholds:
        train = simulate_panel(prediction, panel, panel["train"], threshold=threshold,
            round_trip_cost_ratio=cost)
        development = simulate_panel(prediction, panel, panel["development"],
            threshold=threshold, round_trip_cost_ratio=cost)
        eligible = (_passes(train, minimum_trades=minimum_trades,
            maximum_drawdown=maximum_drawdown) and _passes(development,
            minimum_trades=minimum_trades, maximum_drawdown=maximum_drawdown))
        score = min(float(train["net_return_fraction"]),
            float(development["net_return_fraction"])) if eligible else None
        candidates.append({"threshold": format(threshold, ".12f"),
            "train": train, "development": development,
            "eligible": eligible, "selection_score": score})
    eligible = [row for row in candidates if row["eligible"]]
    selected = max(eligible, key=lambda row: (row["selection_score"], row["threshold"])) \
        if eligible else None
    final = None
    if selected:
        final = simulate_panel(prediction, panel, panel["final"],
            threshold=float(selected["threshold"]), round_trip_cost_ratio=cost)
    qualified = bool(final and _passes(final, minimum_trades=minimum_trades,
        maximum_drawdown=maximum_drawdown))
    native = bytes(model.get_booster().save_raw(raw_format="json"))
    result = {"schema": SCHEMA, "evidence_class": "ZERO_RETROSPECTIVE_RESEARCH",
        "markets": panel["markets"], "market_count": len(panel["markets"]),
        "horizon_bars": horizon, "feature_names": list(FEATURE_NAMES),
        "target_mode": target_mode,
        "partition_counts": {name: len(panel[name]) for name in
            ("train", "development", "final")},
        "round_trip_cost_bps": format(round_trip_cost_bps, ".6f"),
        "minimum_trades": minimum_trades,
        "maximum_drawdown_fraction": format(maximum_drawdown, ".6f"),
        "model": {"implementation": "xgboost.XGBRegressor", "trees": 192,
            "max_depth": 3, "seed": seed,
            "native_sha256": sha256(native).hexdigest()},
        "threshold_candidates": candidates,
        "selected_threshold": None if selected is None else selected["threshold"],
        "final": final, "final_consumed": final is not None,
        "prospective_qualification": qualified,
        "selected_candidate": "POOLED_TOP_ONE" if qualified else "CASH",
        "automatic_promotion": False, "network_orders": False,
        "trading_authority": False, "paid_model_calls": 0}
    result["result_sha256"] = sha256(json.dumps(result, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return result


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2, allow_nan=False)+"\n")
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--horizon", type=int, default=24)
    parser.add_argument("--round-trip-cost-bps", type=float, default=26.0)
    parser.add_argument("--minimum-trades", type=int, default=8)
    parser.add_argument("--maximum-drawdown", type=float, default=0.20)
    parser.add_argument("--seed", type=int, default=29)
    parser.add_argument("--target-mode", choices=("ABSOLUTE_RETURN", "CROSS_SECTIONAL_EXCESS"),
        default="CROSS_SECTIONAL_EXCESS")
    args = parser.parse_args(argv)
    started = perf_counter()
    series, sources = [], []
    for manifest_path in args.manifest:
        bars, digest, manifest = authenticated_bars(manifest_path)
        series.append((manifest["market"], bars))
        sources.append({"market": manifest["market"], "manifest": str(manifest_path),
            "dataset_identity_sha256": digest})
    result = screen_panel(series, horizon=args.horizon,
        round_trip_cost_bps=args.round_trip_cost_bps,
        minimum_trades=args.minimum_trades,
        maximum_drawdown=args.maximum_drawdown, seed=args.seed,
        target_mode=args.target_mode)
    source_hash = sha256(json.dumps(sources, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    target = args.output_dir/f"h{args.horizon}-{source_hash}-{result['result_sha256']}.json"
    _atomic(target, {"sources": sources, "result": result})
    print(json.dumps({"result_path": str(target), "market_count": len(series),
        "horizon_bars": args.horizon,
        "target_mode": result["target_mode"],
        "selected_threshold": result["selected_threshold"],
        "final_consumed": result["final_consumed"],
        "prospective_qualification": result["prospective_qualification"],
        "elapsed_seconds": round(perf_counter()-started, 3),
        "network_orders": False, "paid_model_calls": 0}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

