from datetime import UTC, datetime, timedelta
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
import unittest

from research.autotrade_research.integrations.whitebit_public import (
    PublicBook,
    PublicFundingHistory,
    PublicFundingRecord,
)
from tools.run_whitebit_funding_carry_shadow import _vwap, evaluate_pair, historical_replay_gate


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
    def test_multilevel_buy_vwap_from_course_exercise(self):
        snapshot = replace(book("WBT_USDT", "99.90", "100.00"),
                           asks=(("100.00", "2"), ("100.10", "3"), ("100.30", "4")))
        self.assertEqual(_vwap(snapshot, "BUY", Decimal(7)), Decimal("700.90") / 7)
        self.assertEqual(_vwap(snapshot, "BUY", Decimal(5)), Decimal("100.06"))
        with self.assertRaisesRegex(ValueError, "INSUFFICIENT_PUBLIC_BOOK_DEPTH"):
            _vwap(snapshot, "BUY", Decimal(10))

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

    def test_historical_gate_uses_fixed_chronological_partitions(self):
        fixture = self.fixture()
        newest = NOW
        descending = []
        for index in range(800):
            at = newest - timedelta(hours=8 * index)
            descending.append(PublicFundingRecord(
                market="WBT_PERP", funding_time=at, rate_calculated_time=at,
                funding_rate="0.0002", settlement_price="100",
            ))

        class Client:
            def funding_history(self, _market, *, limit, offset):
                rows = tuple(descending[offset:offset + limit])
                return PublicFundingHistory(
                    market="WBT_PERP", received_at=NOW, records=rows,
                    raw=f"history-{offset}".encode(), source_uri=f"history/{offset}",
                )

            def klines(self, _market, *, interval, end, limit, training):
                self.assertions = (interval, training)
                start = end - limit * 3600
                candles = tuple(SimpleNamespace(
                    open_time=datetime.fromtimestamp(start + index * 3600, UTC),
                    close="100",
                ) for index in range(limit))
                return SimpleNamespace(candles=candles, digest=f"sha256:kline-{start}")

        client = Client()
        first = client.funding_history("WBT_PERP", limit=100, offset=0)
        result = historical_replay_gate(
            client=client, future=fixture["future"], perpetual=fixture["perpetual"],
            spot=fixture["spot"], first_history=first, notional=Decimal("1000"),
            horizon_days=30,
        )
        self.assertTrue(result["qualified"])
        self.assertGreaterEqual(result["train"]["window_count"], 4)
        self.assertGreaterEqual(result["evaluation"]["window_count"], 2)
        self.assertTrue(result["horizon_selected_before_evaluation"])
        self.assertFalse(result["overlapping_windows"])


if __name__ == "__main__":
    unittest.main()

