from datetime import UTC, datetime, timedelta
from unittest.mock import patch
import unittest

from research.autotrade_research.strategies.volatility_expansion_breakout import (
    GateConfiguration, StrategyConfiguration,
)
from tools.screen_whitebit_volatility_grid import locked_final_grid_screen


def bars():
    start = datetime(2025, 1, 1, tzinfo=UTC)
    return [dict(opened_at=start+timedelta(hours=i), closed_at=start+timedelta(hours=i+1),
        open=str(100+i), high=str(102+i), low=str(99+i), close=str(101+i), volume="1000")
        for i in range(12)]


def row(candidate, value):
    return {"candidate": candidate, "return_fraction": str(value),
        "max_drawdown_fraction": "0.1", "closed_cycles": 3}


class WhitebitVolatilityGridTests(unittest.TestCase):
    def setUp(self):
        self.good = StrategyConfiguration(breakout_lookback=2, volatility_lookback=2,
            range_expansion_multiplier="1", minimum_range_fraction="0",
            volume_lookback=2, volume_multiplier="1", minimum_mean_quote_volume="0",
            exit_lookback=2, minimum_hold_bars=1, maximum_hold_bars=2)
        self.bad = StrategyConfiguration(**{**self.good.__dict__, "breakout_lookback": 3})
        self.calls = []

    def fake(self, bars, *, candidate, costs, strategy):
        partition = int(bars[0]["open"])
        self.calls.append((partition, candidate, strategy.breakout_lookback))
        if candidate == "CASH": return row(candidate, 0)
        if candidate == "BTC_HOLD": return row(candidate, .04)
        return row(candidate, .2 if strategy.breakout_lookback == 2 else -.1)

    def test_one_winner_alone_consumes_final(self):
        with patch("tools.screen_whitebit_volatility_grid.simulate", side_effect=self.fake):
            result = locked_final_grid_screen(bars(), train_count=4,
                development_count=4, dataset_sha256="a"*64,
                configurations=[self.good, self.bad], gate=GateConfiguration(1, "1", True))
        self.assertTrue(result["prospective_qualification"])
        self.assertEqual(result["selected_configuration"]["breakout_lookback"], 2)
        self.assertEqual(sum(partition == 108 for partition, _, _ in self.calls), 3)
        self.assertEqual({lookback for partition, _, lookback in self.calls if partition == 108}, {2})

    def test_no_winner_keeps_final_untouched(self):
        def losing(bars, *, candidate, costs, strategy):
            self.calls.append((int(bars[0]["open"]), candidate, strategy.breakout_lookback))
            return row(candidate, 0 if candidate == "CASH" else -.1)
        with patch("tools.screen_whitebit_volatility_grid.simulate", side_effect=losing):
            result = locked_final_grid_screen(bars(), train_count=4,
                development_count=4, dataset_sha256="b"*64,
                configurations=[self.good], gate=GateConfiguration(1, "1", True))
        self.assertFalse(result["final_consumed"])
        self.assertFalse(any(partition == 108 for partition, _, _ in self.calls))


if __name__ == "__main__":
    unittest.main()
