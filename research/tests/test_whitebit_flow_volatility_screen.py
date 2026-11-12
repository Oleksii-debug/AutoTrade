from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autotrade_research.strategies.flow_volatility_screen import (
    FlowVolatilityCandidate,
    _simulate,
    candidates,
    screen_flow_volatility,
)


CANDIDATE = FlowVolatilityCandidate(
    "EXPANSION", "MOMENTUM", 6, "0.2", "1.5", "1.25", 3,
)


def _rows(count: int, *, correct: bool = True) -> list[dict]:
    rows = []
    for index in range(count):
        signal = 1 if index % 16 == 6 else -1 if index % 16 == 14 else 0
        active_signal = 0
        for distance in (1, 2, 3):
            prior = (index - distance) % 16
            if prior == 6:
                active_signal = 1
            elif prior == 14:
                active_signal = -1
        move = active_signal if correct else -active_signal
        first = Decimal("100")
        last = first + Decimal(move)
        expansion = signal != 0
        rows.append({
            "bucket_epoch": index * 5,
            "bucket_seconds": 5,
            "first_price": format(first, "f"),
            "last_price": format(last, "f"),
            "high_price": "102" if expansion else "100.2",
            "low_price": "98" if expansion else "99.8",
            "aggressive_buy_quote_quantity": "300" if signal > 0 else "0" if signal < 0 else "50",
            "aggressive_sell_quote_quantity": "300" if signal < 0 else "0" if signal > 0 else "50",
        })
    return rows


def test_registry_is_bounded_and_preregistered():
    registry = candidates()
    assert len(registry) == 128
    assert {row.regime for row in registry} == {"EXPANSION", "CONTRACTION"}
    assert {row.side for row in registry} == {"MOMENTUM", "CONTRARIAN"}


def test_simulation_uses_completed_signal_then_next_open_and_costs():
    free = _simulate(_rows(160), CANDIDATE, Decimal(0))
    charged = _simulate(_rows(160), CANDIDATE, Decimal("0.0026"))
    assert free["trade_count"] > 3
    assert Decimal(free["net_return_fraction"]) > Decimal(charged["net_return_fraction"])
    assert charged["maximum_drawdown_fraction"] == "0"


def test_locked_final_requires_independent_development(tmp_path):
    tape = Path(tmp_path / "tape.sqlite3")
    tape.touch()
    passed_rows = _rows(600)
    with patch(
        "autotrade_research.strategies.flow_volatility_screen._read_rows",
        return_value=passed_rows,
    ), patch(
        "autotrade_research.strategies.flow_volatility_screen.candidates",
        return_value=(CANDIDATE,),
    ):
        passed = screen_flow_volatility(tape, "SOL_USDT", minimum_buckets=600)
    assert passed["development_passed"] is True
    assert passed["final_consumed"] is True
    assert passed["prospective_qualification"] is True
    assert passed["automatic_promotion"] is False

    failed_rows = _rows(360) + _rows(120, correct=False) + _rows(120)
    with patch(
        "autotrade_research.strategies.flow_volatility_screen._read_rows",
        return_value=failed_rows,
    ), patch(
        "autotrade_research.strategies.flow_volatility_screen.candidates",
        return_value=(CANDIDATE,),
    ):
        failed = screen_flow_volatility(tape, "SOL_USDT", minimum_buckets=600)
    assert failed["development_passed"] is False
    assert failed["final_consumed"] is False
    assert failed["final"] is None


def test_waiting_state_never_consumes_final(tmp_path):
    tape = Path(tmp_path / "tape.sqlite3")
    tape.touch()
    with patch(
        "autotrade_research.strategies.flow_volatility_screen._read_rows",
        return_value=_rows(299),
    ):
        result = screen_flow_volatility(tape, "BTC_USDT")
    assert result["status"] == "WAITING_FOR_CAUSAL_BUCKETS"
    assert result["final_consumed"] is False
    assert result["prospective_qualification"] is False
