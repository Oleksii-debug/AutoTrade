from pathlib import Path
from unittest.mock import patch

from autotrade_research.strategies.medium_horizon_trade_flow import (
    candidates,
    screen_medium_horizon_flow,
)


def _rows(count: int, *, correct: bool = True) -> list[dict]:
    rows = []
    for index in range(count):
        signal = 1 if (index // 40) % 2 == 0 else -1
        delayed = 1 if index < 36 else (1 if ((index - 36) // 40) % 2 == 0 else -1)
        move = delayed if correct else -delayed
        rows.append({
            "bucket_epoch": index * 5,
            "bucket_seconds": 5,
            "first_price": "100",
            "last_price": "102" if move > 0 else "98",
            "high_price": "102",
            "low_price": "98",
            "aggressive_buy_quote_quantity": "100" if signal > 0 else "0",
            "aggressive_sell_quote_quantity": "0" if signal > 0 else "100",
        })
    return rows


def test_registry_is_bounded_and_medium_horizon_only():
    registry = candidates()
    assert len(registry) == 96
    assert {item.lookback for item in registry} == {12, 36, 60}
    assert {item.threshold for item in registry} == {"0.5", "0.8"}
    assert {item.horizon for item in registry} == {36, 60, 120, 180}


def test_waits_for_900_causal_buckets_without_final(tmp_path):
    database = Path(tmp_path / "tape.sqlite3")
    database.touch()
    with patch(
        "autotrade_research.strategies.medium_horizon_trade_flow._read_rows",
        return_value=_rows(899),
    ):
        result = screen_medium_horizon_flow(database, "SOL_USDT")
    assert result["status"] == "WAITING_FOR_CAUSAL_BUCKETS"
    assert result["analysis_bucket_count"] == 0
    assert result["final_consumed"] is False
    assert result["network_orders"] is False


def test_rejection_keeps_final_locked_and_explains_best_train(tmp_path):
    database = Path(tmp_path / "tape.sqlite3")
    database.touch()
    flat = _rows(1200)
    for row in flat:
        row.update(first_price="100", last_price="100", high_price="100",
                   low_price="100")
    with patch(
        "autotrade_research.strategies.medium_horizon_trade_flow._read_rows",
        return_value=flat,
    ):
        result = screen_medium_horizon_flow(database, "SOL_USDT")
    assert result["status"] == "REJECTED"
    assert result["candidate_count"] == 96
    assert result["best_train_observed"] is not None
    assert result["final_consumed"] is False
    assert result["prospective_qualification"] is False
    assert result["automatic_promotion"] is False


def test_development_failure_is_explained_without_consuming_final(tmp_path):
    database = Path(tmp_path / "tape.sqlite3")
    database.touch()
    with patch(
        "autotrade_research.strategies.medium_horizon_trade_flow._read_rows",
        return_value=_rows(1000),
    ):
        result = screen_medium_horizon_flow(database, "SOL_USDT")
    if result["selected_train"] is not None and not result["development_passed"]:
        assert result["development_failure_reasons"]
        assert result["development_required_return_fraction"] is not None
        assert result["final_consumed"] is False


def test_uses_more_than_minimum_when_contiguous_history_is_available(tmp_path):
    database = Path(tmp_path / "tape.sqlite3")
    database.touch()
    with patch(
        "autotrade_research.strategies.medium_horizon_trade_flow._read_rows",
        return_value=_rows(1400),
    ):
        result = screen_medium_horizon_flow(database, "SOL_USDT")
    assert result["analysis_bucket_count"] == 1400
    assert result["partition_counts"] == {
        "TRAIN": 840, "DEVELOPMENT": 280, "FINAL": 280,
    }


def test_caps_analysis_history_before_chronological_split(tmp_path):
    database = Path(tmp_path / "tape.sqlite3")
    database.touch()
    with patch(
        "autotrade_research.strategies.medium_horizon_trade_flow._read_rows",
        return_value=_rows(5000),
    ):
        result = screen_medium_horizon_flow(database, "SOL_USDT")
    assert result["analysis_bucket_count"] == 4320
    assert sum(result["partition_counts"].values()) == 4320


def test_invalid_economics_and_bounds_fail_closed(tmp_path):
    database = Path(tmp_path / "tape.sqlite3")
    database.touch()
    for kwargs in (
        {"minimum_buckets": 899},
        {"maximum_buckets": 899},
        {"maximum_buckets": 17281},
        {"round_trip_cost_bps": "nan"},
        {"minimum_trades": 0},
    ):
        try:
            screen_medium_horizon_flow(database, "SOL_USDT", **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {kwargs}")
