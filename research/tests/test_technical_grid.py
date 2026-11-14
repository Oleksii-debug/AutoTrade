from decimal import Decimal as D

import pytest

from research.autotrade_research.strategies.technical_grid import (
    Candle, Plan, advance, size_plan,
)


def candle(opened, high, low, close, volume="1"):
    return Candle(*(D(value) for value in (opened, high, low, close, volume)))


@pytest.mark.parametrize("side", (1, -1))
def test_stop_precedes_profit_in_ambiguous_bar(side):
    if side == 1:
        plan = Plan(side, (D(99), D(97)), D(95), D(104), 48)
        values = candle(100, 105, 94, 103)
    else:
        plan = Plan(side, (D(101), D(103)), D(105), D(96), 48)
        values = candle(100, 106, 95, 97)
    ladder = size_plan(plan, D(500), 0, D("0.0002"), D("0.0003"))
    reason, price = advance(ladder, values, index=10, adverse=D("0.0002"), funding_per_day=D("0.0003"))
    assert (reason, price) == ("STOP_TOUCH", plan.stop)
    assert ladder.filled_levels == 2


def test_target_waits_two_hours_after_last_add_and_gap_exits_at_open():
    ladder = size_plan(Plan(1, (D(99), D(97)), D(95), D(105), 48),
                       D(500), 0, D("0.0002"), D("0.0003"))
    costs = dict(adverse=D("0.0002"), funding_per_day=D("0.0003"))
    assert advance(ladder, candle(100, 104, 96, 99), index=2, **costs)[0] is None
    assert advance(ladder, candle(100, 106, 100, 105), index=9, **costs)[0] is None
    assert advance(ladder, candle(100, 106, 100, 105), index=10, **costs)[0] is None
    assert advance(ladder, candle(100, 106, 100, 105), index=11, **costs) == ("TARGET", D(105))
    assert advance(ladder, candle(90, 106, 89, 101), index=10, **costs) == ("STOP_GAP", D(90))


def test_quote_touch_zero_volume_and_expiring_entries_do_not_fill():
    ladder = size_plan(Plan(1, (D(99), D(97)), D(95), D(105), 48),
                       D(500), 0, D("0.0002"), D("0.0003"))
    costs = dict(adverse=D("0.0002"), funding_per_day=D("0.0003"))
    for bar, index in ((candle(100, 101, 99, 100), 1),
                       (candle(100, 101, 96, 100, "0"), 2),
                       (candle(100, 101, 96, 100), 40)):
        assert advance(ladder, bar, index=index, **costs)[0] is None
        assert ladder.filled_levels == 0
    assert advance(ladder, candle(100, 101, 96, 100), index=48, **costs)[0] == "TIME_EXIT"


def test_hourly_target_eligible_after_two_full_hours_and_spot_fees():
    plan = Plan(1, (D(99),), D(95), D(105), 12)
    ladder = size_plan(plan, D(500), 0, D("0.0002"), D(0), step=60, fee_rate=D("0.001"))
    costs = dict(adverse=D("0.0002"), funding_per_day=D(0))
    assert advance(ladder, candle(100, 101, 98, 99), index=1, **costs)[0] is None
    assert ladder.fees == ladder.basis * D("0.001")
    assert ladder.funding == 0
    assert advance(ladder, candle(101, 106, 100, 105), index=3, **costs)[0] is None
    assert advance(ladder, candle(101, 106, 100, 105), index=4, **costs)[0] == "TARGET"
