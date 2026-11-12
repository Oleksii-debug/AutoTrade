from pathlib import Path
from unittest.mock import patch

from autotrade_research.strategies.trade_flow_screen import (
    FlowCandidate,
    _simulate,
    candidates,
    screen_trade_flow,
)


CANDIDATE = FlowCandidate("MOMENTUM", 1, "0.2", 1)


def _rows(count, *, correct=True, start=0):
    rows = []
    signs = [1 if (index + start) % 2 == 0 else -1 for index in range(count)]
    for index, sign in enumerate(signs):
        previous = signs[index - 1] if index else 1
        move = previous if correct else -previous
        rows.append({
            "bucket_epoch": (index + start) * 5,
            "bucket_seconds": 5,
            "first_price": "100",
            "last_price": "101" if move > 0 else "99",
            "high_price": "101",
            "low_price": "99",
            "aggressive_buy_quote_quantity": "100" if sign > 0 else "0",
            "aggressive_sell_quote_quantity": "0" if sign > 0 else "100",
        })
    return rows


def test_flow_simulation_is_nonoverlapping_and_cost_aware():
    result = _simulate(_rows(40), CANDIDATE, cost=__import__("decimal").Decimal("0.0026"))
    assert result["trade_count"] == 39
    assert float(result["net_return_fraction"]) > 0
    assert result["maximum_drawdown_fraction"] == "0"


def test_price_response_families_are_causal_and_preregistered():
    assert len(candidates()) == 108
    rows = [
        {
            "bucket_epoch": 0, "bucket_seconds": 5,
            "first_price": "100", "last_price": "99",
            "high_price": "100", "low_price": "99",
            "aggressive_buy_quote_quantity": "100",
            "aggressive_sell_quote_quantity": "0",
        },
        {
            "bucket_epoch": 5, "bucket_seconds": 5,
            "first_price": "99", "last_price": "98",
            "high_price": "99", "low_price": "98",
            "aggressive_buy_quote_quantity": "0",
            "aggressive_sell_quote_quantity": "100",
        },
    ]
    absorption = _simulate(
        rows, FlowCandidate("ABSORPTION_REVERSAL", 1, "0.8", 1),
        cost=__import__("decimal").Decimal("0.0026"),
    )
    confirmed = _simulate(
        rows, FlowCandidate("CONFIRMED_MOMENTUM", 1, "0.8", 1),
        cost=__import__("decimal").Decimal("0.0026"),
    )
    assert absorption["trade_count"] == 1
    assert absorption["net_return_fraction"] > "0"
    assert confirmed["trade_count"] == 0


def test_locked_final_is_consumed_only_after_train_and_development(tmp_path):
    dummy = tmp_path / "tape.sqlite3"
    dummy.touch()
    rows = _rows(600)
    with patch("autotrade_research.strategies.trade_flow_screen._read_rows",
               return_value=rows), patch(
               "autotrade_research.strategies.trade_flow_screen.candidates",
               return_value=(CANDIDATE,)):
        passed = screen_trade_flow(dummy, "BTC_USDT", minimum_buckets=600,
                                   minimum_trades=10)
    assert passed["development_passed"] is True
    assert passed["final_consumed"] is True
    assert passed["prospective_qualification"] is True
    assert passed["automatic_promotion"] is False

    mixed = _rows(360) + _rows(120, correct=False, start=360) + _rows(120, start=480)
    with patch("autotrade_research.strategies.trade_flow_screen._read_rows",
               return_value=mixed), patch(
               "autotrade_research.strategies.trade_flow_screen.candidates",
               return_value=(CANDIDATE,)):
        rejected = screen_trade_flow(dummy, "BTC_USDT", minimum_buckets=600,
                                     minimum_trades=10)
    assert rejected["development_passed"] is False
    assert rejected["final_consumed"] is False
    assert rejected["final"] is None


def test_waits_for_contiguous_causal_buckets_without_touching_final(tmp_path):
    dummy = Path(tmp_path / "tape.sqlite3")
    dummy.touch()
    with patch("autotrade_research.strategies.trade_flow_screen._read_rows",
               return_value=_rows(299)):
        result = screen_trade_flow(dummy, "BTC_USDT", minimum_buckets=300)
    assert result["status"] == "WAITING_FOR_CAUSAL_BUCKETS"
    assert result["final_consumed"] is False
    assert result["prospective_qualification"] is False

