from datetime import UTC, datetime, timedelta
from unittest.mock import patch
import unittest

from research.autotrade_research.strategies.cross_market_relative_strength import (
    GateConfiguration, StrategyConfiguration,
)
from tools.screen_whitebit_cross_market import locked_final_grid_screen


def bars(base):
    start = datetime(2025, 1, 1, tzinfo=UTC)
    return [dict(opened_at=start+timedelta(hours=i),
        closed_at=start+timedelta(hours=i+1), open=str(base+i), close=str(base+i+1))
        for i in range(12)]


def result(candidate, value, cycles=3):
    return {"candidate": candidate, "return_fraction": str(value),
        "net_pnl": str(value*1000), "max_drawdown_fraction": "0.1",
        "closed_cycles": cycles}


class LockedWhitebitCrossMarketTests(unittest.TestCase):
    def setUp(self):
        self.good = StrategyConfiguration(2, 2, "1", 2, 3, 3)
        self.bad = StrategyConfiguration(3, 2, "1", 2, 3, 3)
        self.btc, self.eth = bars(100), bars(50)
        self.calls = []

    def fake(self, btc, eth, *, candidate, costs, strategy):
        partition = int(btc[0]["open"])
        self.calls.append((partition, candidate, strategy.ratio_lookback))
        benchmark = {"CASH": 0, "BTC_HOLD": .04, "BTC_MOMENTUM": .03}.get(candidate)
        if benchmark is not None:
            return result(candidate, benchmark)
        value = .20 if strategy.ratio_lookback == 2 else -.10
        return result(candidate, value)

    def test_grid_selects_on_train_development_then_touches_final_once(self):
        with patch("tools.screen_whitebit_cross_market.simulate", side_effect=self.fake):
            screened = locked_final_grid_screen(self.btc, self.eth,
                train_count=4, development_count=4, dataset_sha256="a"*64,
                configurations=[self.good, self.bad],
                gate=GateConfiguration(1, "1"))
        self.assertTrue(screened["prospective_qualification"])
        self.assertEqual(screened["selected_configuration"]["ratio_lookback"], 2)
        self.assertEqual(sum(partition == 108 for partition, _, _ in self.calls), 4)
        self.assertEqual({lookback for partition, _, lookback in self.calls if partition == 108}, {2})

    def test_no_development_winner_keeps_final_untouched(self):
        def losing(btc, eth, *, candidate, costs, strategy):
            self.calls.append((int(btc[0]["open"]), candidate, strategy.ratio_lookback))
            return result(candidate, 0 if candidate == "CASH" else -.1)
        with patch("tools.screen_whitebit_cross_market.simulate", side_effect=losing):
            screened = locked_final_grid_screen(self.btc, self.eth,
                train_count=4, development_count=4, dataset_sha256="b"*64,
                configurations=[self.good], gate=GateConfiguration(1, "1"))
        self.assertFalse(screened["final_consumed"])
        self.assertFalse(any(partition == 108 for partition, _, _ in self.calls))


if __name__ == "__main__":
    unittest.main()
