from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from research.autotrade_research.integrations.whitebit_public import (
    PublicBook, PublicInstrument, PublicTicker, PublicTrade, PublicTradeBatch,
)
from research.autotrade_research.strategies.maker_universe import evaluate_market, rank_candidates
from tools.screen_whitebit_maker_universe import main, screen


NOW = datetime(2026, 11, 12, 6, tzinfo=timezone.utc)


def instrument(market="ABC_USDT"):
    return PublicInstrument(market, market.removesuffix("_USDT"), "USDT", "spot",
                            "0.001", "0.001", "0.01", "5", "0.01", "0.01", True)


def ticker(market="ABC_USDT", volume="500000", moment=NOW):
    return PublicTicker(market, moment, "10", volume, "50000", "1", False, 1, 2,
                        b"ticker", "https://example/ticker")


def book(market="ABC_USDT", bid="9.98", ask="10.02", quantity="100", moment=NOW):
    return PublicBook(market, moment, moment, ((bid, quantity),), ((ask, quantity),),
                      b"book-" + market.encode(), "https://example/book")


def batch(market="ABC_USDT", buy="80", sell="80", *, age=10, moment=NOW):
    rows = (
        PublicTrade(market, 2, "10", "8", buy, moment - timedelta(seconds=age), "buy", False),
        PublicTrade(market, 1, "10", "8", sell, moment - timedelta(seconds=age), "sell", False),
        PublicTrade(market, 0, "10", "1", "10", moment - timedelta(seconds=age), "buy", False),
    )
    return PublicTradeBatch(market, moment, rows, b"trades-" + market.encode(), "https://example/trades")


class FakeClient:
    def __init__(self, markets=("ABC_USDT", "PEPE_USDT"), moment=None):
        self.markets = markets
        self.moment = moment or datetime.now(timezone.utc)

    def instruments(self):
        return tuple(instrument(market) for market in self.markets)

    def market_activities(self):
        return tuple(ticker(market, str(900000 - index * 100000), self.moment)
                     for index, market in enumerate(self.markets))

    def instrument_orderbook(self, market):
        return book(market, moment=self.moment)

    def recent_trades(self, market):
        return batch(market, moment=self.moment)


class MakerUniverseTests(unittest.TestCase):
    def test_accepts_only_after_cost_depth_activity_and_neutral_flow(self):
        result = evaluate_market(instrument(), ticker(), book(), batch(), now=NOW)
        self.assertTrue(result["candidate"])
        self.assertEqual(result["net_spread_after_cost_reserve_bps"], "15.000")
        self.assertEqual(result["rejection_reasons"], [])
        self.assertFalse(result["network_orders"])
        self.assertTrue(result["research_only"])

    def test_rejects_displayed_spread_without_depth(self):
        result = evaluate_market(instrument(), ticker(), book(quantity="1"), batch(), now=NOW)
        self.assertFalse(result["candidate"])
        self.assertIn("INSUFFICIENT_TOP_DEPTH", result["rejection_reasons"])

    def test_rejects_stale_or_directional_activity(self):
        stale = batch(age=120)
        result = evaluate_market(instrument(), ticker(), book(), stale, now=NOW)
        self.assertIn("INSUFFICIENT_RECENT_ACTIVITY", result["rejection_reasons"])
        directional = batch(buy="200", sell="1")
        result = evaluate_market(instrument(), ticker(), book(), directional, now=NOW)
        self.assertIn("DIRECTIONAL_RECENT_FLOW", result["rejection_reasons"])

    def test_rejects_spread_that_does_not_clear_fees_and_reserve(self):
        result = evaluate_market(instrument(), ticker(), book(bid="9.99", ask="10.01"), batch(), now=NOW)
        self.assertIn("NO_SPREAD_AFTER_COST_RESERVE", result["rejection_reasons"])

    def test_rank_uses_net_edge_then_activity(self):
        first = evaluate_market(instrument("AAA_USDT"), ticker("AAA_USDT"),
                                book("AAA_USDT", "9.98", "10.02"), batch("AAA_USDT"), now=NOW)
        second = evaluate_market(instrument("BBB_USDT"), ticker("BBB_USDT"),
                                 book("BBB_USDT", "9.97", "10.03"), batch("BBB_USDT"), now=NOW)
        self.assertEqual(rank_candidates([first, second], limit=1)[0]["market"], "BBB_USDT")

    def test_screen_excludes_prior_failed_market_and_is_inert(self):
        report = screen(client=FakeClient(moment=NOW), now=NOW, prefilter_limit=2, candidate_limit=1)
        self.assertEqual(report["evaluated_count"], 1)
        self.assertEqual(report["candidates"][0]["market"], "ABC_USDT")
        self.assertEqual(report["excluded_prior_failed_markets"], ["ENA_USDT", "PEPE_USDT"])
        self.assertFalse(report["automatic_promotion"])
        self.assertFalse(report["trading_authority"])
        self.assertEqual(report["paid_model_calls"], 0)

    def test_cli_persists_checkpoint(self):
        import tools.screen_whitebit_maker_universe as module
        original = module.WhiteBitPublicClient
        with tempfile.TemporaryDirectory() as temporary:
            try:
                module.WhiteBitPublicClient = FakeClient
                self.assertEqual(main(["--state-dir", temporary, "--prefilter-limit", "2",
                                       "--candidate-limit", "2"]), 0)
            finally:
                module.WhiteBitPublicClient = original
            saved = json.loads((Path(temporary) / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["schema"], "autotrade-whitebit-maker-universe-screen-v1")
            self.assertFalse(saved["network_orders"])


if __name__ == "__main__":
    unittest.main()
