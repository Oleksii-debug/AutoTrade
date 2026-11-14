from decimal import Decimal

from research.autotrade_research.strategies.intraday_absorption import (
    Candle, Position, exit_reference, pullback_signal,
)


def candle(opened, high, low, close, volume=100):
    return Candle(*(Decimal(value) for value in (opened, high, low, close, volume)))


def test_pullback_requires_volume_wick_and_reclaim_in_both_directions():
    prior = [candle(100, 101, 99, 100) for _ in range(31)]
    prior[0] = candle(90, 91, 89, 90)
    impulse = candle(100, 100, 95, 96, 300)
    rejection = candle(96, 99, 93, 99)
    signal = pullback_signal([*prior, impulse, rejection])
    assert (signal.side, signal.atr) == (1, Decimal(2))
    assert pullback_signal([*prior, candle(100, 100, 95, 96), rejection]) is None
    assert pullback_signal([*prior, impulse, candle(96, 99, 96, 99)]) is None
    assert pullback_signal([*prior, impulse, candle(96, 100, 93, 100)]) is None

    def mirror(bar):
        return candle(200 - bar.opened, 200 - bar.low, 200 - bar.high, 200 - bar.close, bar.volume)

    mirrored = pullback_signal([mirror(bar) for bar in [*prior, impulse, rejection]])
    assert (mirrored.side, mirrored.atr) == (-1, Decimal(2))


def test_stop_gap_uses_open_and_time_exit_does_not_observe_future_wick():
    position = Position("BTC_PERP", 1, 0, Decimal(1), Decimal(100), Decimal("100.02"),
                        Decimal(96), Decimal(500), Decimal("0.055"))
    assert exit_reference(position, candle(95, 97, 94, 97), True) == (Decimal(95), "STOP_GAP")
    assert exit_reference(position, candle(100, 102, 95, 101), True) == (Decimal(100), "TIME_EXIT")
    assert exit_reference(position, candle(100, 102, 95, 101), False) == (Decimal(96), "STOP_TOUCH")
    assert exit_reference(position, candle(100, 102, 95, 101, 0), False) == (Decimal(101), "")
