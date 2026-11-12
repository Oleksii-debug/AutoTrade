from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from autotrade_research.strategies.cross_market_trade_flow import (
    LeadLagCandidate,
    _aligned_rows,
    _simulate,
    candidates,
    screen_lead_lag,
)


CANDIDATE = LeadLagCandidate("LEADER_MOMENTUM", 1, "0.8", 1)


def _aligned(count: int, *, correct: bool = True, start: int = 0) -> list[dict]:
    rows = []
    signs = [1 if (index + start) % 2 == 0 else -1 for index in range(count)]
    for index, sign in enumerate(signs):
        previous = signs[index - 1] if index else 1
        move = previous if correct else -previous
        rows.append({
            "bucket_epoch": (index + start) * 5,
            "leader_buy_quote": "100" if sign > 0 else "0",
            "leader_sell_quote": "0" if sign > 0 else "100",
            "target_first_price": "100",
            "target_last_price": "101" if move > 0 else "99",
        })
    return rows


def _market_rows(rows: list[dict], *, leader: bool) -> list[dict]:
    return [{
        "bucket_epoch": row["bucket_epoch"],
        "bucket_seconds": 5,
        "first_price": "100" if leader else row["target_first_price"],
        "last_price": "100" if leader else row["target_last_price"],
        "aggressive_buy_quote_quantity": row["leader_buy_quote"] if leader else "0",
        "aggressive_sell_quote_quantity": row["leader_sell_quote"] if leader else "0",
    } for row in rows]


def test_candidate_registry_is_bounded_and_preregistered():
    registry = candidates()
    assert len(registry) == 54
    assert {row.family for row in registry} == {
        "LEADER_MOMENTUM", "LEADER_CONTRARIAN",
    }


def test_simulation_uses_next_bucket_entry_and_nonoverlapping_costs():
    result = _simulate(_aligned(40), CANDIDATE, Decimal("0.0026"))
    assert result["trade_count"] == 39
    assert Decimal(result["net_return_fraction"]) > 0
    assert result["maximum_drawdown_fraction"] == "0"

    no_future_move = _aligned(40)
    for row in no_future_move:
        row["target_last_price"] = row["target_first_price"]
    loss = _simulate(no_future_move, CANDIDATE, Decimal("0.0026"))
    assert Decimal(loss["net_return_fraction"]) < 0


def test_alignment_keeps_only_latest_strict_common_segment():
    base = _aligned(5)
    leader = _market_rows(base, leader=True)
    target = _market_rows(base, leader=False)
    target[3]["bucket_epoch"] += 20
    aligned = _aligned_rows(leader, target)
    assert [row["bucket_epoch"] for row in aligned] == [20]


def test_locked_final_requires_train_and_development(tmp_path):
    tape = Path(tmp_path / "tape.sqlite3")
    tape.touch()
    good = _aligned(300)

    def good_read(_path, market):
        return _market_rows(good, leader=market == "SOL_USDT")

    with patch("autotrade_research.strategies.cross_market_trade_flow._read_rows",
               side_effect=good_read), patch(
               "autotrade_research.strategies.cross_market_trade_flow.candidates",
               return_value=(CANDIDATE,)):
        passed = screen_lead_lag(
            tape, "SOL_USDT", "PEPE_USDT", minimum_buckets=300,
            minimum_trades=10,
        )
    assert passed["development_passed"] is True
    assert passed["final_consumed"] is True
    assert passed["prospective_qualification"] is True
    assert passed["automatic_promotion"] is False
    assert passed["trading_authority"] is False

    mixed = _aligned(180) + _aligned(60, correct=False, start=180) + _aligned(60, start=240)

    def mixed_read(_path, market):
        return _market_rows(mixed, leader=market == "SOL_USDT")

    with patch("autotrade_research.strategies.cross_market_trade_flow._read_rows",
               side_effect=mixed_read), patch(
               "autotrade_research.strategies.cross_market_trade_flow.candidates",
               return_value=(CANDIDATE,)):
        rejected = screen_lead_lag(
            tape, "SOL_USDT", "PEPE_USDT", minimum_buckets=300,
            minimum_trades=10,
        )
    assert rejected["development_passed"] is False
    assert rejected["final_consumed"] is False
    assert rejected["final"] is None


def test_waits_without_consuming_final(tmp_path):
    tape = Path(tmp_path / "tape.sqlite3")
    tape.touch()
    rows = _aligned(179)

    def read(_path, market):
        return _market_rows(rows, leader=market == "SOL_USDT")

    with patch("autotrade_research.strategies.cross_market_trade_flow._read_rows",
               side_effect=read):
        result = screen_lead_lag(
            tape, "SOL_USDT", "PEPE_USDT", minimum_buckets=180,
        )
    assert result["status"] == "WAITING_FOR_ALIGNED_BUCKETS"
    assert result["final_consumed"] is False
    assert result["prospective_qualification"] is False


def test_rejects_same_market_and_too_short_experiment(tmp_path):
    tape = Path(tmp_path / "tape.sqlite3")
    tape.touch()
    with pytest.raises(ValueError, match="must differ"):
        screen_lead_lag(tape, "SOL_USDT", "SOL_USDT")
    with pytest.raises(ValueError, match="at least 180"):
        screen_lead_lag(tape, "SOL_USDT", "PEPE_USDT", minimum_buckets=179)
