import unittest
from decimal import Decimal

from mvp.autotrade_mvp.valuation import (
    FxQuote,
    InventoryBook,
    SettlementBook,
    TradableMark,
    ValuationUncertain,
    value_position,
)


class ValuationTests(unittest.TestCase):
    def test_cash_round_trip_reference_vector(self):
        book = InventoryBook()
        buy = book.apply_fill(fill_id="buy", side="BUY", quantity="2", price="100", trade_time="2026-09-24T10:00:00Z")
        sell = book.apply_fill(fill_id="sell", side="SELL", quantity="1", price="110", trade_time="2026-09-24T11:00:00Z")
        self.assertEqual(buy.realized_gross_pnl, Decimal("0"))
        self.assertEqual(sell.realized_gross_pnl, Decimal("10"))
        self.assertEqual(book.position_quantity, Decimal("1"))
        self.assertEqual(book.gross_open_basis, Decimal("100"))
        self.assertEqual(book.unrealized_gross_pnl("105"), Decimal("5"))
        # Fees are intentionally outside basis: 10 + 5 - 1.50 = 13.50 net P&L.
        self.assertEqual(book.realized_gross_pnl + book.unrealized_gross_pnl("105") - Decimal("1.5"), Decimal("13.5"))

    def test_short_inventory_realized_and_unrealized_signs(self):
        book = InventoryBook()
        book.apply_fill(fill_id="s1", side="SELL", quantity="2", price="100", trade_time="2026-09-24T10:00:00Z")
        close = book.apply_fill(fill_id="b1", side="BUY", quantity="1", price="90", trade_time="2026-09-24T11:00:00Z")
        self.assertEqual(close.realized_gross_pnl, Decimal("10"))
        self.assertEqual(book.position_quantity, Decimal("-1"))
        self.assertEqual(book.unrealized_gross_pnl("95"), Decimal("5"))

    def test_fill_identity_is_idempotent_but_conflict_is_rejected(self):
        book = InventoryBook()
        first = book.apply_fill(fill_id="x", side="BUY", quantity="1", price="10", trade_time="2026-09-24T10:00:00Z")
        replay = book.apply_fill(fill_id="x", side="BUY", quantity="1", price="10", trade_time="2026-09-24T10:00:00Z")
        self.assertEqual(first, replay)
        with self.assertRaisesRegex(ValueError, "identity conflict"):
            book.apply_fill(fill_id="x", side="BUY", quantity="2", price="10", trade_time="2026-09-24T10:00:00Z")

    def test_split_preserves_total_basis_and_creates_no_pnl(self):
        book = InventoryBook()
        book.apply_fill(fill_id="buy", side="BUY", quantity="10", price="100", trade_time="2026-09-24T10:00:00Z")
        before = book.gross_open_basis
        book.apply_split("2", effective_at="2026-09-25T00:00:00Z")
        self.assertEqual(book.position_quantity, Decimal("20"))
        self.assertEqual(book.gross_open_basis, before)
        self.assertEqual(book.lots[0].unit_basis, Decimal("50"))
        self.assertEqual(book.realized_gross_pnl, Decimal("0"))

    def test_long_uses_bid_and_short_uses_ask_for_liquidation(self):
        long_book = InventoryBook()
        long_book.apply_fill(fill_id="l", side="BUY", quantity="1", price="100", trade_time="2026-09-24T10:00:00Z")
        mark = TradableMark("AAPL@v1", "104", "106", "2026-09-24T10:00:05Z", "quote-1")
        long_value = value_position("AAPL@v1", long_book, mark, now="2026-09-24T10:00:10Z", max_age_seconds=10, stress_haircut="0.1")
        self.assertEqual(long_value.liquidation_mark, Decimal("104"))
        self.assertEqual(long_value.gross_unrealized_pnl, Decimal("4"))
        self.assertEqual(long_value.stressed_liquidation_value, Decimal("93.6"))

        short_book = InventoryBook()
        short_book.apply_fill(fill_id="s", side="SELL", quantity="1", price="100", trade_time="2026-09-24T10:00:00Z")
        short_value = value_position("AAPL@v1", short_book, mark, now="2026-09-24T10:00:10Z", max_age_seconds=10, stress_haircut="0.1")
        self.assertEqual(short_value.liquidation_mark, Decimal("106"))
        self.assertEqual(short_value.gross_unrealized_pnl, Decimal("-6"))
        self.assertEqual(short_value.stressed_liquidation_value, Decimal("-116.6"))

    def test_stale_or_future_mark_fails_closed(self):
        book = InventoryBook()
        book.apply_fill(fill_id="l", side="BUY", quantity="1", price="100", trade_time="2026-09-24T10:00:00Z")
        stale = TradableMark("AAPL@v1", "99", "101", "2026-09-24T09:00:00Z", "quote")
        with self.assertRaisesRegex(ValuationUncertain, "stale"):
            value_position("AAPL@v1", book, stale, now="2026-09-24T10:00:00Z", max_age_seconds=30, stress_haircut="0")
        future = TradableMark("AAPL@v1", "99", "101", "2026-09-24T11:00:00Z", "quote")
        with self.assertRaisesRegex(ValuationUncertain, "future"):
            value_position("AAPL@v1", book, future, now="2026-09-24T10:00:00Z", max_age_seconds=30, stress_haircut="0")

    def test_fx_conversion_uses_bid_for_assets_and_ask_for_liabilities(self):
        fx = FxQuote("EUR", "USD", "1.10", "1.11", "2026-09-24T10:00:00Z", "fx-source")
        self.assertEqual(fx.convert("100", now="2026-09-24T10:00:05Z", max_age_seconds=10), Decimal("110"))
        self.assertEqual(fx.convert("-100", now="2026-09-24T10:00:05Z", max_age_seconds=10), Decimal("-111"))
        with self.assertRaisesRegex(ValuationUncertain, "stale"):
            fx.convert("100", now="2026-09-24T10:10:00Z", max_age_seconds=10)

    def test_settled_and_unsettled_cash_remain_separate(self):
        cash = SettlementBook()
        cash.post("USD", "1000", bucket="SETTLED")
        cash.post("USD", "110", bucket="UNSETTLED")
        cash.settle("USD", "60")
        snap = cash.snapshot()
        self.assertEqual(snap.settled["USD"], Decimal("1060"))
        self.assertEqual(snap.unsettled["USD"], Decimal("50"))
        with self.assertRaisesRegex(ValueError, "cannot settle"):
            cash.settle("USD", "51")


if __name__ == "__main__":
    unittest.main()
