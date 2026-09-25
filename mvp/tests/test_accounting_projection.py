from decimal import Decimal
import unittest

from mvp.autotrade_mvp.accounting import (
    AccountingConflict,
    EconomicBook,
    JournalTransaction,
    book_equity_fill,
    book_external_cash_flow,
    posting,
    project_equity_position,
    reverse_transaction,
)


class EquityPositionProjectionTests(unittest.TestCase):
    def test_fifo_projection_matches_cash_round_trip_reference(self):
        book = EconomicBook()
        book.append(book_external_cash_flow(
            transaction_id="seed",
            cause_event_id="cash-1",
            currency="USD",
            amount="1000",
        ))
        book.append(book_equity_fill(
            transaction_id="buy-1",
            cause_event_id="fill-1",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="100",
            fee="1",
        ))
        book.append(book_equity_fill(
            transaction_id="sell-1",
            cause_event_id="fill-2",
            instrument="ABC",
            settlement_currency="USD",
            side="SELL",
            quantity="1",
            price="110",
            fee="0.50",
        ))

        projection = project_equity_position(
            book,
            instrument="ABC",
            settlement_currency="USD",
            mark_price="105",
        )

        self.assertEqual(projection.quantity, Decimal("1"))
        self.assertEqual(projection.open_cost_basis, Decimal("100"))
        self.assertEqual(projection.realized_pnl, Decimal("10"))
        self.assertEqual(projection.unrealized_pnl, Decimal("5"))
        self.assertEqual(projection.policy_version, "FIFO_GROSS_V1")

    def test_fifo_closes_oldest_lot_first(self):
        book = EconomicBook()
        for transaction in (
            book_equity_fill(
                transaction_id="buy-100",
                cause_event_id="fill-100",
                instrument="ABC",
                settlement_currency="USD",
                side="BUY",
                quantity="1",
                price="100",
            ),
            book_equity_fill(
                transaction_id="buy-120",
                cause_event_id="fill-120",
                instrument="ABC",
                settlement_currency="USD",
                side="BUY",
                quantity="1",
                price="120",
            ),
            book_equity_fill(
                transaction_id="sell-110",
                cause_event_id="fill-110",
                instrument="ABC",
                settlement_currency="USD",
                side="SELL",
                quantity="1",
                price="110",
            ),
        ):
            book.append(transaction)

        projection = project_equity_position(
            book,
            instrument="ABC",
            settlement_currency="USD",
            mark_price="125",
        )
        self.assertEqual(projection.realized_pnl, Decimal("10"))
        self.assertEqual(projection.open_cost_basis, Decimal("120"))
        self.assertEqual(projection.unrealized_pnl, Decimal("5"))
        self.assertEqual(projection.lots[0].transaction_id, "buy-120")

    def test_short_cover_and_flip_are_exact(self):
        book = EconomicBook()
        book.append(book_equity_fill(
            transaction_id="short",
            cause_event_id="fill-short",
            instrument="ABC",
            settlement_currency="USD",
            side="SELL",
            quantity="2",
            price="100",
        ))
        book.append(book_equity_fill(
            transaction_id="cover-and-flip",
            cause_event_id="fill-cover",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="3",
            price="90",
        ))
        projection = project_equity_position(
            book,
            instrument="ABC",
            settlement_currency="USD",
            mark_price="95",
        )
        self.assertEqual(projection.realized_pnl, Decimal("20"))
        self.assertEqual(projection.quantity, Decimal("1"))
        self.assertEqual(projection.open_cost_basis, Decimal("90"))
        self.assertEqual(projection.unrealized_pnl, Decimal("5"))

    def test_fees_remain_separate_from_gross_basis_and_pnl(self):
        book = EconomicBook()
        book.append(book_equity_fill(
            transaction_id="buy",
            cause_event_id="fill-buy",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="1",
            price="100",
            fee="3",
        ))
        book.append(book_equity_fill(
            transaction_id="sell",
            cause_event_id="fill-sell",
            instrument="ABC",
            settlement_currency="USD",
            side="SELL",
            quantity="1",
            price="110",
            fee="2",
        ))
        projection = project_equity_position(
            book,
            instrument="ABC",
            settlement_currency="USD",
            mark_price="110",
        )
        self.assertEqual(projection.quantity, Decimal("0"))
        self.assertEqual(projection.open_cost_basis, Decimal("0"))
        self.assertEqual(projection.realized_pnl, Decimal("10"))
        self.assertEqual(book.fee_expense("USD"), Decimal("5"))

    def test_third_currency_fee_never_changes_gross_projection(self):
        book = EconomicBook()
        book.append(book_equity_fill(
            transaction_id="buy-third-fee",
            cause_event_id="fill-buy-third-fee",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="1",
            price="100",
            fee="2",
            fee_currency="EUR",
        ))
        book.append(book_equity_fill(
            transaction_id="sell-third-fee",
            cause_event_id="fill-sell-third-fee",
            instrument="ABC",
            settlement_currency="USD",
            side="SELL",
            quantity="1",
            price="110",
            fee="-0.50",
            fee_currency="EUR",
        ))
        projection = project_equity_position(
            book,
            instrument="ABC",
            settlement_currency="USD",
            mark_price="110",
        )
        self.assertEqual(projection.realized_pnl, Decimal("10"))
        self.assertEqual(book.fee_expense("EUR"), Decimal("1.50"))

    def test_missing_mark_keeps_unrealized_unknown(self):
        book = EconomicBook()
        book.append(book_equity_fill(
            transaction_id="buy",
            cause_event_id="fill-buy",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="1",
            price="100",
        ))
        projection = project_equity_position(
            book,
            instrument="ABC",
            settlement_currency="USD",
        )
        self.assertIsNone(projection.mark_price)
        self.assertIsNone(projection.unrealized_pnl)

    def test_noncanonical_position_adjustment_fails_closed(self):
        book = EconomicBook()
        book.append(JournalTransaction(
            transaction_id="manual-position",
            cause_event_id="manual-1",
            postings=(
                posting("POSITION:ABC", "ABC", "1"),
                posting("CLEARING:ABC", "ABC", "-1"),
            ),
        ))
        with self.assertRaisesRegex(AccountingConflict, "canonical equity-fill"):
            project_equity_position(
                book,
                instrument="ABC",
                settlement_currency="USD",
            )

    def test_reversed_fill_history_fails_closed_until_effective_time_exists(self):
        book = EconomicBook()
        original = book_equity_fill(
            transaction_id="buy",
            cause_event_id="fill-buy",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="100",
        )
        book.append(original)
        book.append(reverse_transaction(
            original,
            transaction_id="reverse-buy",
            cause_event_id="provider-correction",
        ))
        with self.assertRaisesRegex(AccountingConflict, "effective-time"):
            project_equity_position(
                book,
                instrument="ABC",
                settlement_currency="USD",
            )

    def test_binary_float_mark_fails_closed(self):
        book = EconomicBook()
        with self.assertRaises(TypeError):
            project_equity_position(
                book,
                instrument="ABC",
                settlement_currency="USD",
                mark_price=100.5,
            )


if __name__ == "__main__":
    unittest.main()
