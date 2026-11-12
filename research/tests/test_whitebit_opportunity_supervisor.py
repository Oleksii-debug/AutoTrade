from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
import unittest

from tools.run_whitebit_opportunity_supervisor import (
    completed_window,
    fast_volatility_grid,
    rank_markets,
)


class OpportunitySupervisorTests(unittest.TestCase):
    def test_fast_volatility_grid_is_bounded_and_spans_full_grid(self):
        grid = fast_volatility_grid()
        self.assertEqual(len(grid), 16)
        self.assertEqual(len({repr(item) for item in grid}), 16)

    def test_completed_window_excludes_open_hour(self):
        start, end = completed_window(datetime(2026, 11, 12, 1, 35, tzinfo=UTC), 800)
        self.assertEqual(end, int(datetime(2026, 11, 12, 0, tzinfo=UTC).timestamp()))
        self.assertEqual((end - start) // 3600 + 1, 800)

    def test_rank_markets_uses_exact_quote_volume_and_filters(self):
        markets = (
            {"market": "AAA_USDT", "quote_asset": "USDT"},
            {"market": "BBB_USDT", "quote_asset": "USDT"},
            {"market": "USDC_USDT", "quote_asset": "USDT"},
            {"market": "CCC_BTC", "quote_asset": "BTC"},
        )
        tickers = (
            SimpleNamespace(market="AAA_USDT", quote_volume="1000001", is_frozen=False),
            SimpleNamespace(market="BBB_USDT", quote_volume="2000000", is_frozen=False),
            SimpleNamespace(market="USDC_USDT", quote_volume="9999999", is_frozen=False),
            SimpleNamespace(market="CCC_BTC", quote_volume="9999999", is_frozen=False),
        )
        ranked = rank_markets(markets, tickers, limit=2,
                              minimum_quote_volume=Decimal("1000000"))
        self.assertEqual([row["market"] for row in ranked], ["BBB_USDT", "AAA_USDT"])
        self.assertEqual(ranked[0]["quote_volume_24h"], "2000000")

    def test_rank_markets_rejects_frozen_and_low_volume(self):
        markets = (
            {"market": "AAA_USDT", "quote_asset": "USDT"},
            {"market": "BBB_USDT", "quote_asset": "USDT"},
        )
        tickers = (
            SimpleNamespace(market="AAA_USDT", quote_volume="999999", is_frozen=False),
            SimpleNamespace(market="BBB_USDT", quote_volume="2000000", is_frozen=True),
        )
        self.assertEqual(rank_markets(markets, tickers, limit=2,
                                     minimum_quote_volume=Decimal("1000000")), [])

    def test_rank_market_limit_is_bounded(self):
        with self.assertRaises(ValueError):
            rank_markets((), (), limit=33, minimum_quote_volume=Decimal("0"))

    def test_rank_markets_ignores_invalid_provider_volume(self):
        markets = ({"market": "AAA_USDT", "quote_asset": "USDT"},)
        tickers = (SimpleNamespace(
            market="AAA_USDT", quote_volume="not-a-number", is_frozen=False),)
        self.assertEqual(rank_markets(markets, tickers, limit=1,
                                     minimum_quote_volume=Decimal("0")), [])

    def test_completed_window_requires_timezone_and_bounds(self):
        with self.assertRaises(ValueError):
            completed_window(datetime(2026, 11, 12), 800)
        with self.assertRaises(ValueError):
            completed_window(datetime.now(UTC), 799)


if __name__ == "__main__":
    unittest.main()

