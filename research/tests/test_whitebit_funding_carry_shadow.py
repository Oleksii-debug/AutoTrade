from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
import unittest

from research.autotrade_research.integrations.whitebit_public import (
    PublicBook,
    PublicFundingHistory,
    PublicFundingRecord,
)
from tools.run_whitebit_funding_carry_shadow import evaluate_pair


NOW = datetime(2026, 11, 12, 1, 0, tzinfo=UTC)


def book(market: str, bid: str, ask: str) -> PublicBook:
    return PublicBook(
        market=market,
        source_time=NOW,
        received_at=NOW,
        bids=((bid, "100000"),),
        asks=((ask, "100000"),),
        raw=(market + bid + ask).encode(),
        source_uri=f"https://whitebit.com/api/v4/public/orderbook/{market}?limit=100&level=0",
    )


class FundingCarryShadowTests(unittest.TestCase):
    def fixture(self, rate: str = "0.0002") -> dict:
        future = SimpleNamespace(
            market="WBT_PERP", base_asset="WBT", quote_asset="USDT",
            funding_rate=rate, funding_interval_minutes=480,
            mark_price="100.1", index_price="100",
        )
        perpetual = SimpleNamespace(
            market="WBT_PERP", base_asset="WBT", quote_asset="USDT",
            step_size="0.001", minimum_quantity="0.1", minimum_notional="5",
            taker_fee_rate="0.00055",
        )
        spot = SimpleNamespace(
            market="WBT_USDT", base_asset="WBT", quote_asset="USDT",
            step_size="0.001", minimum_quantity="0.01", minimum_notional="1",
            taker_fee_rate="0.001",
        )
        records = tuple(PublicFundingRecord(
            market="WBT_PERP", funding_time=NOW, rate_calculated_time=NOW,
            funding_rate=rate, settlement_price="100",
        ) for _ in range(100))
        history = PublicFundingHistory(
            market="WBT_PERP", received_at=NOW, records=records, raw=b"history",
            source_uri="https://whitebit.com/api/v4/public/funding-history/WBT_PERP?limit=100&offset=0",
        )
        return dict(future=future, perpetual=perpetual, spot=spot, history=history,
                    spot_book=book("WBT_USDT", "99.99", "100.01"),
                    future_book=book("WBT_PERP", "99.99", "100.01"),
                    notional=Decimal("1000"), basis_stress_bps=Decimal("25"))

    def test_long_enough_persistent_funding_can_qualify_for_shadow_only(self):
        result = evaluate_pair(**self.fixture(), horizon_days=30)
        self.assertTrue(result["qualified_for_shadow_observation"])
        self.assertGreater(Decimal(result["stressed_after_cost_quote"]), 0)
        self.assertEqual(result["execution_environment"], "SIMULATION_ONLY")
        self.assertFalse(result["order_authority"])
        self.assertFalse(result["leverage_enabled"])

    def test_short_horizon_and_nonpositive_funding_are_rejected(self):
        short = evaluate_pair(**self.fixture(), horizon_days=1)
        self.assertFalse(short["qualified_for_shadow_observation"])
        negative = evaluate_pair(**self.fixture("-0.0001"), horizon_days=30)
        self.assertFalse(negative["qualified_for_shadow_observation"])
        self.assertEqual(Decimal(negative["projected_funding_quote"]), 0)


if __name__ == "__main__":
    unittest.main()

