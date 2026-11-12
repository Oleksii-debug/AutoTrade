#!/usr/bin/env python3
"""Cost-aware causal OHLCV alpha screen over authenticated WhiteBIT history.

The model observes only completed candles, enters at the next candle open and
exits after a fixed horizon.  Threshold selection consumes TRAIN and
DEVELOPMENT only; FINAL remains locked until a candidate is profitable after
explicit costs in both earlier partitions.  This tool has no credential or
order route and cannot promote a model into live trading.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from math import cos, log, pi, sin, sqrt
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import numpy as np
from threadpoolctl import threadpool_limits

from tools.screen_whitebit_trend_regime import authenticated_bars


SCHEMA = "whitebit-ohlcv-alpha-locked-final-v1"
FEATURE_NAMES = (
    "return_1", "return_3", "return_6", "return_12", "return_24", "return_72",
    "range_fraction", "body_fraction", "close_location", "volume_change_1",
    "volume_z_24", "volume_z_72", "volatility_6", "volatility_24",
    "volatility_72", "rsi_14", "hour_sin", "hour_cos",
)
LOOKBACK = 72


def _safe_z(value: float, history: np.ndarray) -> float:
    scale = float(np.std(history))
    return 0.0 if scale < 1e-12 else (value - float(np.mean(history))) / scale


def _numeric_arrays(bars: list[dict]) -> tuple[np.ndarray, ...]:
    opened = np.asarray([float(row["open"]) for row in bars], dtype=np.float64)
    high = np.asarray([float(row["high"]) for row in bars], dtype=np.float64)
    low = np.asarray([float(row["low"]) for row in bars], dtype=np.float64)
    close = np.asarray([float(row["close"]) for row in bars], dtype=np.float64)
    volume = np.asarray([float(row["volume"]) for row in bars], dtype=np.float64)
    if (not all(np.isfinite(value).all() for value in (opened, high, low, close, volume))
            or min(opened.min(), high.min(), low.min(), close.min()) <= 0
            or volume.min() < 0):
        raise ValueError("bars contain invalid numeric values")
    returns = np.diff(np.log(close), prepend=np.log(close[0]))
    return opened, high, low, close, volume, returns, np.log1p(volume)


def _feature_row(bars: list[dict], arrays: tuple[np.ndarray, ...], t: int) -> list[float]:
    if not LOOKBACK <= t < len(bars):
        raise ValueError("feature row lacks causal history")
    opened, high, low, close, _volume, returns, log_volume = arrays
    lagged = [close[t] / close[t-lag] - 1.0 for lag in (1, 3, 6, 12, 24, 72)]
    candle_range = (high[t] - low[t]) / close[t-1]
    body = (close[t] - opened[t]) / opened[t]
    location = (close[t] - low[t]) / max(high[t] - low[t], 1e-12)
    volume_change = log_volume[t] - log_volume[t-1]
    gains = np.maximum(returns[t-13:t+1], 0.0)
    losses = np.maximum(-returns[t-13:t+1], 0.0)
    denominator = float(np.mean(gains) + np.mean(losses))
    rsi = 0.5 if denominator < 1e-12 else float(np.mean(gains) / denominator)
    hour = bars[t]["closed_at"].hour + bars[t]["closed_at"].minute / 60.0
    return lagged + [
        candle_range, body, location, volume_change,
        _safe_z(log_volume[t], log_volume[t-23:t+1]),
        _safe_z(log_volume[t], log_volume[t-71:t+1]),
        float(np.std(returns[t-5:t+1])),
        float(np.std(returns[t-23:t+1])),
        float(np.std(returns[t-71:t+1])),
        rsi, sin(2*pi*hour/24), cos(2*pi*hour/24),
    ]


def latest_feature(bars: list[dict]) -> np.ndarray:
    """Return the current completed-candle feature without requiring a label."""
    if len(bars) < LOOKBACK + 1:
        raise ValueError("latest feature requires at least 73 bars")
    row = np.asarray([_feature_row(bars, _numeric_arrays(bars), len(bars)-1)],
                     dtype=np.float64)
    if row.shape != (1, len(FEATURE_NAMES)) or not np.isfinite(row).all():
        raise ValueError("latest feature is invalid")
    return row


def build_dataset(bars: list[dict], *, horizon: int) -> dict:
    """Build causal close-time features and next-open executable labels."""
    if type(horizon) is not int or not 1 <= horizon <= 24:
        raise ValueError("horizon must be an integer in [1,24]")
    if len(bars) < 400:
        raise ValueError("at least 400 bars are required")
    arrays = _numeric_arrays(bars)
    opened = arrays[0]
    features, labels, decisions = [], [], []
    for t in range(LOOKBACK, len(bars) - horizon - 1):
        entry = opened[t+1]
        exit_price = opened[t+1+horizon]
        features.append(_feature_row(bars, arrays, t))
        labels.append(exit_price / entry - 1.0)
        decisions.append(t)
    x = np.asarray(features, dtype=np.float64)
    y = np.asarray(labels, dtype=np.float64)
    t = np.asarray(decisions, dtype=np.int64)
    if x.shape != (len(y), len(FEATURE_NAMES)) or not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError("feature construction produced invalid values")
    a, b = int(len(y)*0.6), int(len(y)*0.8)
    train = np.arange(0, a-horizon, dtype=np.int64)
    development = np.arange(a, b-horizon, dtype=np.int64)
    final = np.arange(b, len(y), dtype=np.int64)
    if min(len(train), len(development), len(final)) < 40:
        raise ValueError("chronological partitions are too small")
    if not (t[train[-1]] + horizon < t[development[0]] and
            t[development[-1]] + horizon < t[final[0]]):
        raise ValueError("partition outcome intervals overlap")
    return {"x": x, "y": y, "decision_bar": t, "train": train,
            "development": development, "final": final, "horizon": horizon}


def simulate_partition(prediction: np.ndarray, dataset: dict, indices: np.ndarray,
        *, threshold: float, round_trip_cost_ratio: float, allow_short: bool) -> dict:
    if threshold < 0 or not 0 <= round_trip_cost_ratio < 1:
        raise ValueError("invalid threshold or costs")
    equity = peak = 1.0
    maximum_drawdown = 0.0
    trades = wins = longs = shorts = 0
    gross_total = cost_total = 0.0
    next_decision_bar = -1
    for row in indices:
        decision_bar = int(dataset["decision_bar"][row])
        if decision_bar < next_decision_bar:
            continue
        estimate = float(prediction[row])
        direction = 1 if estimate >= threshold else (-1 if allow_short and estimate <= -threshold else 0)
        if not direction:
            continue
        gross = direction * float(dataset["y"][row])
        net = gross - round_trip_cost_ratio
        equity *= max(1.0 + net, 1e-12)
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, (peak-equity)/peak)
        trades += 1
        wins += int(net > 0)
        longs += int(direction > 0)
        shorts += int(direction < 0)
        gross_total += gross
        cost_total += round_trip_cost_ratio
        next_decision_bar = decision_bar + dataset["horizon"] + 1
    return {
        "trade_count": trades, "winning_trade_count": wins,
        "long_trade_count": longs, "short_trade_count": shorts,
        "gross_return_sum": format(gross_total, ".12f"),
        "explicit_cost_sum": format(cost_total, ".12f"),
        "net_return_fraction": format(equity-1.0, ".12f"),
        "maximum_drawdown_fraction": format(maximum_drawdown, ".12f"),
        "win_rate": format(wins/trades if trades else 0.0, ".12f"),
    }


def _passes(report: dict, *, minimum_trades: int, maximum_drawdown: float) -> bool:
    return (report["trade_count"] >= minimum_trades
            and float(report["net_return_fraction"]) > 0
            and float(report["maximum_drawdown_fraction"]) <= maximum_drawdown)


def screen(bars: list[dict], *, horizon: int, round_trip_cost_bps: float = 26.0,
        minimum_trades: int = 8, maximum_drawdown: float = 0.20,
        allow_short: bool = False, seed: int = 17,
        model_artifact_sink: dict[str, bytes] | None = None) -> dict:
    if not 0 <= round_trip_cost_bps <= 1000:
        raise ValueError("round-trip costs must be in [0,1000] bps")
    if type(minimum_trades) is not int or not 1 <= minimum_trades <= 1000:
        raise ValueError("minimum trades must be in [1,1000]")
    dataset = build_dataset(bars, horizon=horizon)
    from xgboost import XGBRegressor
    model = XGBRegressor(n_estimators=192, max_depth=3, learning_rate=0.035,
        subsample=0.8, colsample_bytree=0.85, min_child_weight=8,
        reg_lambda=2.0, objective="reg:squarederror", tree_method="hist",
        n_jobs=1, random_state=seed)
    with threadpool_limits(limits=1):
        model.fit(dataset["x"][dataset["train"]], dataset["y"][dataset["train"]])
        prediction = np.asarray(model.predict(dataset["x"]), dtype=np.float64)
    training_abs = np.abs(prediction[dataset["train"]]) if allow_short else prediction[dataset["train"]]
    quantiles = (0.50, 0.60, 0.70, 0.80, 0.875, 0.925, 0.95, 0.975)
    cost = round_trip_cost_bps / 10000.0
    thresholds = sorted(set(max(cost, float(np.quantile(training_abs, q))) for q in quantiles))
    candidates = []
    for threshold in thresholds:
        train = simulate_partition(prediction, dataset, dataset["train"], threshold=threshold,
            round_trip_cost_ratio=cost, allow_short=allow_short)
        development = simulate_partition(prediction, dataset, dataset["development"],
            threshold=threshold, round_trip_cost_ratio=cost, allow_short=allow_short)
        eligible = (_passes(train, minimum_trades=minimum_trades, maximum_drawdown=maximum_drawdown)
                    and _passes(development, minimum_trades=minimum_trades,
                                maximum_drawdown=maximum_drawdown))
        score = min(float(train["net_return_fraction"]),
                    float(development["net_return_fraction"])) if eligible else None
        candidates.append({"threshold": format(threshold, ".12f"), "train": train,
            "development": development, "eligible": eligible, "selection_score": score})
    eligible = [row for row in candidates if row["eligible"]]
    selected = max(eligible, key=lambda row: (row["selection_score"], row["threshold"])) if eligible else None
    final = None
    final_passed = False
    if selected is not None:
        final = simulate_partition(prediction, dataset, dataset["final"],
            threshold=float(selected["threshold"]), round_trip_cost_ratio=cost,
            allow_short=allow_short)
        final_passed = _passes(final, minimum_trades=minimum_trades,
                               maximum_drawdown=maximum_drawdown)
    booster = model.get_booster()
    native_model = bytes(booster.save_raw(raw_format="json"))
    if model_artifact_sink is not None:
        if type(model_artifact_sink) is not dict or model_artifact_sink:
            raise ValueError("model artifact sink must be an empty dictionary")
        model_artifact_sink["xgboost.json"] = native_model
    importance = booster.get_score(importance_type="gain")
    result = {
        "schema": SCHEMA, "evidence_class": "ZERO_RETROSPECTIVE_RESEARCH",
        "horizon_bars": horizon, "feature_names": list(FEATURE_NAMES),
        "partition_counts": {key: len(dataset[key]) for key in ("train", "development", "final")},
        "purged_partition_boundaries": True, "execution": "SIGNAL_CLOSE_NEXT_OPEN_FIXED_HORIZON",
        "round_trip_cost_bps": format(round_trip_cost_bps, ".6f"),
        "allow_short": allow_short, "minimum_trades": minimum_trades,
        "maximum_drawdown_fraction": format(maximum_drawdown, ".6f"),
        "model": {"implementation": "xgboost.XGBRegressor", "trees": 192,
            "max_depth": 3, "seed": seed,
            "native_sha256": sha256(native_model).hexdigest(),
            "feature_gain": importance},
        "threshold_candidates": candidates,
        "selected_threshold": None if selected is None else selected["threshold"],
        "final": final, "final_consumed": final is not None,
        "prospective_qualification": final_passed,
        "selected_candidate": "OHLCV_XGBOOST" if final_passed else "CASH",
        "automatic_promotion": False, "network_orders": False,
        "trading_authority": False, "paid_model_calls": 0,
    }
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


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        stream.write(value); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--horizon", type=int, default=4)
    parser.add_argument("--round-trip-cost-bps", type=float, default=26.0)
    parser.add_argument("--minimum-trades", type=int, default=8)
    parser.add_argument("--maximum-drawdown", type=float, default=0.20)
    parser.add_argument("--allow-short", action="store_true")
    parser.add_argument("--seed", type=int, default=17)
    args = parser.parse_args(argv)
    started = perf_counter()
    bars, dataset_sha256, manifest = authenticated_bars(args.manifest)
    artifacts: dict[str, bytes] = {}
    result = screen(bars, horizon=args.horizon,
        round_trip_cost_bps=args.round_trip_cost_bps,
        minimum_trades=args.minimum_trades, maximum_drawdown=args.maximum_drawdown,
        allow_short=args.allow_short, seed=args.seed, model_artifact_sink=artifacts)
    result["dataset_identity_sha256"] = dataset_sha256
    result["market"] = manifest["market"]
    result["interval"] = manifest["interval"]
    result.pop("result_sha256", None)
    result["result_sha256"] = sha256(json.dumps(result, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    target = args.output_dir / f"{manifest['market'].lower()}-h{args.horizon}-{result['result_sha256']}.json"
    model_target = target.with_suffix(".xgboost.json")
    if sha256(artifacts["xgboost.json"]).hexdigest() != result["model"]["native_sha256"]:
        raise RuntimeError("model artifact hash changed before persistence")
    _atomic_bytes(model_target, artifacts["xgboost.json"])
    _atomic(target, {"source": {"manifest": str(args.manifest),
        "dataset_identity_sha256": dataset_sha256},
        "model_artifact": {"path": str(model_target),
            "sha256": result["model"]["native_sha256"]}, "result": result})
    print(json.dumps({"result_path": str(target), "market": manifest["market"],
        "horizon_bars": args.horizon, "observations": len(bars),
        "selected_threshold": result["selected_threshold"],
        "final_consumed": result["final_consumed"],
        "prospective_qualification": result["prospective_qualification"],
        "model_artifact_path": str(model_target),
        "elapsed_seconds": round(perf_counter()-started, 3),
        "network_orders": False, "paid_model_calls": 0}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

