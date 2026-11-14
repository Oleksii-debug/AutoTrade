from datetime import UTC, datetime
from decimal import Decimal

from research.autotrade_research.strategies.intraday_absorption import Candle
from tools.screen_whitebit_price_reaction import BTC, ETH, SOL, entries_by_clock, select_follower


def test_selects_partial_same_direction_reaction():
    cases = (
        (("0.012", "0.003", "0.003", "0.009"), (ETH, 1), "SIGNAL"),
        (("-0.02", "0.006", "-0.012", "-0.004"), (SOL, -1), "SIGNAL"),
        (("0.012", "0.006", "0.003", "0.008"), None, "NO_BTC_SHOCK"),
        (("0.012", "0.003", "-0.002", "0.008"), None, "FOLLOWERS_DISAGREE"),
        (("0.012", "0.003", "0.006", "0.008"), None, "NO_PARTIAL_REACTION"),
    )
    for inputs, arm, reason in cases:
        assert select_follower(*map(Decimal, inputs)) == (arm, reason)


def make_candle(opened=100, close=100):
    price, close = Decimal(opened), Decimal(close)
    return Candle(price, max(price, close) + 1, min(price, close) - 1, close, Decimal(1))


def test_signal_ignores_the_bar_between_close_and_entry_and_future_prices():
    series = {name: [make_candle() for _ in range(74)] for name in (BTC, ETH, SOL)}
    for market, close in ((BTC, "102"), (ETH, "100.4"), (SOL, "101.5")):
        series[market][68] = make_candle(close=close)
    before, _ = entries_by_clock(series, 0, 74)
    assert before[70]["candidate"][0] == ETH
    assert before[70]["candidate"][1].side == 1
    assert before[70]["candidate"][1].atr == Decimal(2)
    assert before[70]["follower_opposite"][1].side == -1
    assert before[70]["btc_continuation"][0] == BTC
    series[ETH][69] = make_candle(close="1")
    series[ETH][70] = make_candle(close="1")
    series[ETH][71] = make_candle(close="200")
    after, _ = entries_by_clock(series, 0, 74)
    assert before[70] == after[70]
