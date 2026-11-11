from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from research.autotrade_research.strategies.fast_screen import Costs
from research.autotrade_research.strategies.trend_regime import TrendRegime
from tools.run_whitebit_trend_shadow import _new_state, advance, main


def seed():
    closes = ("100", "101", "102")
    start = datetime(2024, 12, 31, 21, tzinfo=UTC)
    return [dict(opened_at=start+timedelta(hours=index),
                 closed_at=start+timedelta(hours=index+1), close=close)
            for index, close in enumerate(closes)]


class WhitebitTrendShadowTests(unittest.TestCase):
    def setUp(self):
        self.costs = Costs(initial_cash="1000", fee_bps="10", spread_bps="10",
                           slippage_bps="5", capital_fraction="1")
        self.strategy = TrendRegime(fast_lookback=2, slow_lookback=2,
            entry_threshold="0", slow_threshold="0", exit_threshold="0.5",
            trailing_stop_fraction="0.5", min_hold_bars=1, max_hold_bars=10)
        self.state = _new_state(seed(), [], self.costs, self.strategy, "a"*64)

    def test_completed_hour_decides_then_executes_at_next_bbo(self):
        first = datetime(2025, 1, 1, 0, 15, tzinfo=UTC)
        snapshot = {"update_id": 1}
        self.assertIsNone(advance(self.state, snapshot, Decimal("102.9"),
                                  Decimal("103.1"), first,
                                  self.costs, self.strategy))
        event = advance(self.state, {"update_id": 2}, Decimal("103.9"),
                        Decimal("104.1"), datetime(2025, 1, 1, 1, 0, tzinfo=UTC),
                        self.costs, self.strategy)
        self.assertEqual(event["action"], "BUY")
        self.assertGreater(Decimal(self.state["quantity"]), 0)
        self.assertEqual(self.state["completed_live_bars"], 1)
        self.assertFalse(self.state["network_orders"])
        self.assertFalse(self.state["trading_authority"])

    def test_seed_live_gap_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "continuity gap"):
            advance(self.state, {"update_id": 1}, Decimal("100"), Decimal("101"),
                    datetime(2025, 1, 1, 2, tzinfo=UTC), self.costs, self.strategy)

    def test_fresh_install_waits_when_research_artifacts_are_absent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = main(["--qualification-result", str(root/"missing-result.json"),
                "--manifest", str(root/"missing-seed.json"),
                "--market-snapshot", str(root/"missing-market.json"),
                "--state-dir", str(root/"state"), "--once"])
            self.assertEqual(result, 0)
            checkpoint = json.loads((root/"state"/"checkpoint.json").read_text())
            self.assertEqual(checkpoint["status"], "WAITING_FOR_QUALIFIED_ARTIFACTS")
            self.assertFalse(checkpoint["network_orders"])


if __name__ == "__main__":
    unittest.main()
