from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tools.weekly_btc_research.forward import FIRST, capture, midnight


def market_data(first, last):
    candles = [
        SimpleNamespace(close=price, open="105", base_volume="10")
        for price in ("100", "101")
    ]
    return SimpleNamespace(
        raw=f"{first} {last}".encode(),
        candles=candles,
        received_at=midnight(last + timedelta(days=1)),
    )


def test_forward_keeps_decisions_and_fills_across_restart(tmp_path):
    day = timedelta(days=1)
    initial = capture(FIRST - day, tmp_path, market_data)
    assert initial["monday_marks"] == []
    first = capture(FIRST + day, tmp_path, market_data)
    marks = first["monday_marks"][0]["liquidation_value"]
    assert Decimal(marks["momentum_base"]) == Decimal(marks["passive_base"]) < 1000
    assert Decimal(marks["cash_stress"]) == Decimal(marks["opposite_base"]) == 1000

    def unavailable(*args):
        pytest.fail("Replay requested previously archived market data")

    assert (
        capture(FIRST + day, tmp_path, unavailable)["monday_marks"]
        == first["monday_marks"]
    )
    next((tmp_path / "sources").iterdir()).write_bytes(b"changed")
    with pytest.raises(ValueError, match="Raw market evidence"):
        capture(FIRST + day, tmp_path, unavailable)


def test_missed_sunday_is_reported_on_monday_and_never_backfilled(tmp_path):
    assert not capture(FIRST, tmp_path, market_data)["complete_decision_chain"]
    result = capture(FIRST + timedelta(days=3), tmp_path, market_data)
    assert not result["complete_decision_chain"]
    assert not result["monday_marks"] and not (tmp_path / "decisions").exists()
