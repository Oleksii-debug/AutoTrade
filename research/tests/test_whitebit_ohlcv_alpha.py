from datetime import UTC, datetime, timedelta

import numpy as np
from hashlib import sha256

from tools.screen_whitebit_ohlcv_alpha import build_dataset, screen, simulate_partition


def _bars(count=500):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    result = []
    price = 100.0
    for index in range(count):
        opened = price
        price *= 1.0002 + (0.0008 if index % 17 == 0 else 0)
        high = max(opened, price) * 1.001
        low = min(opened, price) * 0.999
        result.append({"opened_at": start+timedelta(hours=index),
            "closed_at": start+timedelta(hours=index+1),
            "open": str(opened), "high": str(high), "low": str(low),
            "close": str(price), "volume": str(1000 + (index % 17)*20)})
    return result


def test_features_do_not_read_future_bars():
    bars = _bars()
    original = build_dataset(bars, horizon=4)
    changed = [dict(row) for row in bars]
    changed[300].update(open="500", high="501", low="499", close="500", volume="999999")
    mutated = build_dataset(changed, horizon=4)
    row = int(np.flatnonzero(original["decision_bar"] == 250)[0])
    assert np.array_equal(original["x"][row], mutated["x"][row])


def test_partitions_purge_crossing_outcomes():
    dataset = build_dataset(_bars(), horizon=12)
    decisions = dataset["decision_bar"]
    assert decisions[dataset["train"][-1]] + 12 < decisions[dataset["development"][0]]
    assert decisions[dataset["development"][-1]] + 12 < decisions[dataset["final"][0]]


def test_simulator_skips_overlapping_positions_and_charges_costs():
    dataset = build_dataset(_bars(), horizon=4)
    prediction = np.full(len(dataset["y"]), 0.02)
    result = simulate_partition(prediction, dataset, dataset["train"],
        threshold=0.01, round_trip_cost_ratio=0.001, allow_short=False)
    assert 0 < result["trade_count"] < len(dataset["train"])
    assert result["long_trade_count"] == result["trade_count"]
    assert result["short_trade_count"] == 0
    assert float(result["explicit_cost_sum"]) == result["trade_count"] * 0.001


def test_short_requires_explicit_switch():
    dataset = build_dataset(_bars(), horizon=4)
    prediction = np.full(len(dataset["y"]), -0.02)
    disabled = simulate_partition(prediction, dataset, dataset["train"],
        threshold=0.01, round_trip_cost_ratio=0.001, allow_short=False)
    enabled = simulate_partition(prediction, dataset, dataset["train"],
        threshold=0.01, round_trip_cost_ratio=0.001, allow_short=True)
    assert disabled["trade_count"] == 0
    assert enabled["short_trade_count"] > 0


def test_native_model_artifact_is_hash_bound():
    artifacts = {}
    result = screen(_bars(), horizon=4, minimum_trades=1,
        model_artifact_sink=artifacts)
    assert set(artifacts) == {"xgboost.json"}
    assert sha256(artifacts["xgboost.json"]).hexdigest() == result["model"]["native_sha256"]

