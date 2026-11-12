from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autotrade_research.strategies.flow_triple_barrier import (
    FlowBarrierCandidate,
    _resolve_trade,
    _simulate,
    candidates,
    screen_flow_triple_barrier,
)


CANDIDATE = FlowBarrierCandidate("FLOW_MOMENTUM", 1, "0.5", "40", "20", 12)


def _row(index: int, *, flow: int = 0, first: str = "100", last: str = "100",
         high: str = "100.1", low: str = "99.9") -> dict:
    return {
        "bucket_epoch": index * 5, "bucket_seconds": 5,
        "first_price": first, "last_price": last,
        "high_price": high, "low_price": low,
        "aggressive_buy_quote_quantity": "100" if flow > 0 else "0",
        "aggressive_sell_quote_quantity": "100" if flow < 0 else "0",
    }


def _winning_rows(count: int, *, correct: bool = True) -> list[dict]:
    rows = []
    pending = 0
    for index in range(count):
        flow = 1 if index % 16 == 0 else 0
        high = low = last = "100"
        if pending:
            if correct:
                high, low, last = "100.5", "99.9", "100.4"
            else:
                high, low, last = "100.1", "99.7", "99.8"
        rows.append(_row(index, flow=flow, high=high, low=low, last=last))
        pending = flow
    return rows


def test_registry_is_bounded_and_preregistered():
    registry = candidates()
    assert len(registry) == 96
    assert {row.family for row in registry} == {
        "FLOW_MOMENTUM", "ABSORPTION_REVERSAL",
    }
    assert {row.maximum_holding_buckets for row in registry} == {12, 36, 60}


def test_intrabar_both_hit_uses_adverse_barrier_first():
    rows = [_row(0, first="100", last="100", high="101", low="99")]
    price, index, outcome, both = _resolve_trade(rows, 0, 1, CANDIDATE)
    assert price == Decimal("99.8")
    assert index == 0
    assert outcome == "STOP_LOSS"
    assert both is True

    price, index, outcome, both = _resolve_trade(rows, 0, -1, CANDIDATE)
    assert price == Decimal("100.2")
    assert index == 0
    assert outcome == "STOP_LOSS"
    assert both is True


def test_simulation_enters_next_open_and_charges_full_round_trip():
    free = _simulate(_winning_rows(200), CANDIDATE, Decimal(0))
    charged = _simulate(_winning_rows(200), CANDIDATE, Decimal("0.0026"))
    assert free["trade_count"] > 3
    assert Decimal(free["net_return_fraction"]) > Decimal(charged["net_return_fraction"])
    assert charged["outcome_counts"]["TAKE_PROFIT"] == charged["trade_count"]


def test_locked_final_requires_development_pass(tmp_path):
    tape = Path(tmp_path / "tape.sqlite3")
    tape.touch()
    with patch(
        "autotrade_research.strategies.flow_triple_barrier._read_rows",
        return_value=_winning_rows(600),
    ), patch(
        "autotrade_research.strategies.flow_triple_barrier.candidates",
        return_value=(CANDIDATE,),
    ):
        passed = screen_flow_triple_barrier(tape, "SOL_USDT", minimum_buckets=600)
    assert passed["development_passed"] is True
    assert passed["final_consumed"] is True
    assert passed["prospective_qualification"] is True
    assert passed["conservative_intrabar_order"] == "ADVERSE_FIRST"

    rows = _winning_rows(360) + _winning_rows(120, correct=False) + _winning_rows(120)
    with patch(
        "autotrade_research.strategies.flow_triple_barrier._read_rows",
        return_value=rows,
    ), patch(
        "autotrade_research.strategies.flow_triple_barrier.candidates",
        return_value=(CANDIDATE,),
    ):
        rejected = screen_flow_triple_barrier(tape, "SOL_USDT", minimum_buckets=600)
    assert rejected["development_passed"] is False
    assert rejected["final_consumed"] is False
    assert rejected["final"] is None


def test_waiting_state_never_consumes_final(tmp_path):
    tape = Path(tmp_path / "tape.sqlite3")
    tape.touch()
    with patch(
        "autotrade_research.strategies.flow_triple_barrier._read_rows",
        return_value=_winning_rows(299),
    ):
        result = screen_flow_triple_barrier(tape, "BTC_USDT")
    assert result["status"] == "WAITING_FOR_CAUSAL_BUCKETS"
    assert result["final_consumed"] is False
    assert result["prospective_qualification"] is False
