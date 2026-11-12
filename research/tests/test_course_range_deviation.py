from datetime import UTC, datetime, timedelta
from decimal import Decimal
import unittest

from research.autotrade_research.strategies.course_range_deviation import (
    RangeDeviationConfiguration, detect_deviation, simulate,
)
from tools.screen_whitebit_course_range_universe import screen_universe


def range_bars(count=120, *, deviation=False):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    result = []
    for index in range(count):
        opened = start + timedelta(minutes=15 * index)
        close = Decimal("100") + Decimal(index % 8 - 4) / Decimal(10)
        result.append({"opened_at": opened, "closed_at": opened + timedelta(minutes=15),
            "open": str(close), "high": str(close + Decimal("0.5")),
            "low": str(close - Decimal("0.5")), "close": str(close), "volume": "1000"})
    if deviation:
        lower = min(Decimal(row["low"]) for row in result[-97:-1])
        row = result[-1]
        row.update({"open": str(lower - Decimal("0.1")),
            "low": str(lower - Decimal("0.5")), "high": str(lower + Decimal("1.2")),
            "close": str(lower + Decimal("0.4"))})
    return result


class CourseRangeDeviationTests(unittest.TestCase):
    def test_detects_causal_reentry_after_external_liquidity_sweep(self):
        config = RangeDeviationConfiguration(range_lookback=96,
            minimum_range_atr="1", maximum_range_atr="20",
            minimum_reaction_atr="0.5")
        setup = detect_deviation(range_bars(deviation=True), config)
        self.assertIsNotNone(setup)
        self.assertEqual(setup["direction"], 1)
        self.assertGreater(Decimal(setup["target"]), Decimal(setup["stop"]))
        self.assertIn("shift_level", setup)
        self.assertGreaterEqual(setup["lower_boundary_touches"], 2)
        self.assertGreaterEqual(setup["upper_boundary_touches"], 2)

    def test_no_sweep_means_no_trade(self):
        result = simulate(range_bars(300), configuration=RangeDeviationConfiguration(
            range_lookback=96, minimum_range_atr="1", maximum_range_atr="20"))
        self.assertEqual(result["closed_cycles"], 0)
        self.assertFalse(result["trading_authority"])
        self.assertFalse(result["automatic_strategy_mutation"])

    def test_directional_market_is_not_a_range(self):
        values = range_bars(120)
        for index, row in enumerate(values):
            price = Decimal(100 + index)
            row.update({"open": str(price), "high": str(price + 1),
                "low": str(price - 1), "close": str(price + Decimal("0.5"))})
        self.assertIsNone(detect_deviation(values, RangeDeviationConfiguration()))

    def test_locked_universe_does_not_read_later_partitions_after_train_failure(self):
        value = screen_universe([{"market": "BTC_PERP", "bars": range_bars(1100),
            "dataset_identity_sha256": "a" * 64}], train_count=300,
            development_count=200, configuration=RangeDeviationConfiguration(
                range_lookback=96, minimum_range_atr="1", maximum_range_atr="20"),
            minimum_train_cycles=3)
        self.assertFalse(value["development_consumed"])
        self.assertFalse(value["final_consumed"])
        self.assertIn("MINIMUM_TRAIN_CYCLES_NOT_MET", value["train_rejection_reasons"])


if __name__ == "__main__":
    unittest.main()

