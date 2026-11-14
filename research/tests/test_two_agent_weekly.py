from datetime import UTC, datetime
from decimal import Decimal
import pytest

from research.autotrade_research.strategies.two_agent_weekly import (
    Candidate, Market, _aligned_week_cuts, replay, weekly_summary,
)


def test_signal_does_not_use_immediately_preceding_close_or_entry_price():
    candidate = Candidate("SWING_MOMENTUM", 30, 30, 15)
    market = Market("BTC_PERP", (), tuple(map(Decimal, (100, 100, 100, 110, 999))), frozenset())
    assert candidate.signal(market, 5, 15) == Decimal("0.1")


def test_replay_tracks_funding_and_zero_volume_without_future_admission():
    start = datetime(2026, 11, 9, tzinfo=UTC)
    candidate = Candidate("SWING_MOMENTUM", 15, 15, 15)
    prices = tuple(map(Decimal, (100, 103, 103, 104, 106, 107, 108)))
    market = Market("BTC_PERP", prices, prices, frozenset({3, 4}))
    result = replay((market,), start, 15, candidate, 0, 6, adverse=Decimal("0.0002"),
                    funding_rate=Decimal("0.0003"), keep_trades=True)
    assert result["sides"] == {"LONG": 1, "SHORT": 0}
    assert result["entry_or_exit_legs_on_zero_volume_bars"] == 2
    assert Decimal(result["fees"]) > 0
    assert Decimal(result["modeled_funding_reserve"]) > 0
    assert result["episodes"][0]["signal_closed_at"] == "2026-11-09T00:30:00+00:00"
    assert result["episodes"][0]["entry_at"] == "2026-11-09T00:45:00+00:00"
    assert result["episodes"][0]["exit_at"] == "2026-11-09T01:00:00+00:00"


def test_week_splits_align_to_monday_and_count_flat_periods():
    monday = datetime(2026, 11, 9, tzinfo=UTC)
    assert _aligned_week_cuts(monday, 20 * 672 + 1, 15) == (0, 12 * 672, 16 * 672, 20 * 672)
    with pytest.raises(ValueError, match="20 complete"):
        _aligned_week_cuts(monday, 20 * 672, 15)
    actual = weekly_summary([("a", "500"), ("b", "515"), ("c", "515"), ("d", "540.75")])
    assert actual["complete_weeks"] == 3
    assert actual["positive_weeks"] == 2
    assert actual["target_hits"] == {"0.03": 2, "0.05": 1}


def test_selective_requires_cross_market_confirmation_and_reduces_notional():
    start = datetime(2026, 11, 9, tzinfo=UTC)
    candidate = Candidate("SWING_MOMENTUM", 15, 15, 15)
    prices = tuple(map(Decimal, (100, 103, 103, 104, 106, 107, 108)))
    first = Market("BTC_PERP", prices, prices, frozenset())
    second = Market("ETH_PERP", prices, prices, frozenset())
    common = dict(adverse=Decimal("0.0002"), funding_rate=Decimal("0.0003"))
    single = replay((first,), start, 15, candidate, 0, 6, selective=True, **common)
    assert single["trades"] == 0
    selected = replay((first, second), start, 15, candidate, 0, 6, selective=True, **common)
    control = replay((first, second), start, 15, candidate, 0, 6, **common)
    assert selected["trades"] == 1
    assert Decimal(selected["fees"]) == Decimal(control["fees"]) / 4
