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

    def test_matching_clearing_without_trade_cash_fails_closed(self):
        book = EconomicBook()
        book.append(JournalTransaction(
            transaction_id="spoofed-fill",
            cause_event_id="adjustment-1",
            postings=(
                posting("POSITION:ABC", "ABC", "1"),
                posting("CLEARING:ABC", "ABC", "-1"),
                posting("CLEARING:USD", "USD", "100"),
                posting("SUSPENSE:USD", "USD", "-100"),
            ),
        ))
        with self.assertRaisesRegex(
            AccountingConflict,
            "complete canonical equity-fill posting shape",
        ):
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

    def test_effective_time_correction_restates_fifo_after_later_sell(self):
        book = EconomicBook()
        original = book_equity_fill(
            transaction_id="buy-original",
            cause_event_id="fill-buy",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="100",
            economic_effective_at="2026-01-01T10:00:00Z",
            economic_order_key="provider:A:execution:1",
        )
        later_sell = book_equity_fill(
            transaction_id="sell-later",
            cause_event_id="fill-sell",
            instrument="ABC",
            settlement_currency="USD",
            side="SELL",
            quantity="1",
            price="110",
            economic_effective_at="2026-01-02T10:00:00Z",
            economic_order_key="provider:A:execution:2",
        )
        book.append(original)
        book.append(later_sell)
        reversal = reverse_transaction(
            original,
            transaction_id="reverse-buy",
            cause_event_id="provider-correction-reversal",
            observed_at="2026-01-03T10:00:00Z",
        )
        replacement = book_equity_fill(
            transaction_id="buy-r2",
            cause_event_id="provider-correction-replacement",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="101",
            economic_effective_at="2026-01-01T10:00:00Z",
            observed_at="2026-01-03T10:00:00Z",
            economic_order_key="provider:A:execution:1",
            corrects_transaction_id=original.transaction_id,
        )
        self.assertTrue(book.append_batch((reversal, replacement)))

        projection = project_equity_position(
            book,
            instrument="ABC",
            settlement_currency="USD",
            mark_price="105",
        )
        self.assertEqual(projection.quantity, Decimal("1"))
        self.assertEqual(projection.open_cost_basis, Decimal("101"))
        self.assertEqual(projection.realized_pnl, Decimal("9"))
        self.assertEqual(projection.unrealized_pnl, Decimal("4"))
        self.assertEqual(projection.policy_version, "FIFO_GROSS_EFFECTIVE_V2")

        restarted = EconomicBook(book.transactions)
        self.assertEqual(
            project_equity_position(
                restarted,
                instrument="ABC",
                settlement_currency="USD",
                mark_price="105",
            ),
            projection,
        )

    def test_second_correction_preserves_lineage_and_only_latest_fact_projects(self):
        book = EconomicBook()
        original = book_equity_fill(
            transaction_id="buy-original",
            cause_event_id="fill-buy",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="100",
            economic_effective_at="2026-01-01T10:00:00Z",
            economic_order_key="provider:A:execution:1",
        )
        sell = book_equity_fill(
            transaction_id="sell",
            cause_event_id="fill-sell",
            instrument="ABC",
            settlement_currency="USD",
            side="SELL",
            quantity="1",
            price="110",
            economic_effective_at="2026-01-02T10:00:00Z",
            economic_order_key="provider:A:execution:2",
        )
        book.append(original)
        book.append(sell)

        first_reversal = reverse_transaction(
            original,
            transaction_id="reverse-r1",
            cause_event_id="corr-r1-reversal",
            observed_at="2026-01-03T10:00:00Z",
        )
        first_replacement = book_equity_fill(
            transaction_id="buy-r2",
            cause_event_id="corr-r1-replacement",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="101",
            economic_effective_at="2026-01-01T10:00:00Z",
            observed_at="2026-01-03T10:00:00Z",
            economic_order_key="provider:A:execution:1",
            corrects_transaction_id=original.transaction_id,
        )
        book.append_batch((first_reversal, first_replacement))

        second_reversal = reverse_transaction(
            first_replacement,
            transaction_id="reverse-r2",
            cause_event_id="corr-r2-reversal",
            observed_at="2026-01-04T10:00:00Z",
        )
        second_replacement = book_equity_fill(
            transaction_id="buy-r3",
            cause_event_id="corr-r2-replacement",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="102",
            economic_effective_at="2026-01-01T10:00:00Z",
            observed_at="2026-01-04T10:00:00Z",
            economic_order_key="provider:A:execution:1",
            corrects_transaction_id=first_replacement.transaction_id,
        )
        book.append_batch((second_reversal, second_replacement))

        projection = project_equity_position(
            book,
            instrument="ABC",
            settlement_currency="USD",
        )
        self.assertEqual(projection.quantity, Decimal("1"))
        self.assertEqual(projection.open_cost_basis, Decimal("102"))
        self.assertEqual(projection.realized_pnl, Decimal("8"))
        self.assertEqual(
            [
                item.corrects_transaction_id
                for item in book.transactions
                if item.corrects_transaction_id
            ],
            [original.transaction_id, first_replacement.transaction_id],
        )
        self.assertEqual(
            project_equity_position(
                EconomicBook(book.transactions),
                instrument="ABC",
                settlement_currency="USD",
            ),
            projection,
        )

    def test_equal_effective_times_use_immutable_order_key_during_restatement(self):
        book = EconomicBook()
        original_b = book_equity_fill(
            transaction_id="buy-b-original",
            cause_event_id="fill-b",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="1",
            price="200",
            economic_effective_at="2026-01-01T10:00:00Z",
            economic_order_key="provider:A:execution:b",
        )
        buy_a = book_equity_fill(
            transaction_id="buy-a",
            cause_event_id="fill-a",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="1",
            price="100",
            economic_effective_at="2026-01-01T10:00:00Z",
            economic_order_key="provider:A:execution:a",
        )
        sell_c = book_equity_fill(
            transaction_id="sell-c",
            cause_event_id="fill-c",
            instrument="ABC",
            settlement_currency="USD",
            side="SELL",
            quantity="1",
            price="300",
            economic_effective_at="2026-01-01T10:00:00Z",
            economic_order_key="provider:A:execution:c",
        )
        book.append(original_b)
        book.append(buy_a)
        book.append(sell_c)
        reversal = reverse_transaction(
            original_b,
            transaction_id="reverse-b",
            cause_event_id="corr-b-reversal",
            observed_at="2026-01-02T10:00:00Z",
        )
        replacement_b = book_equity_fill(
            transaction_id="buy-b-r2",
            cause_event_id="corr-b-replacement",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="1",
            price="200",
            economic_effective_at="2026-01-01T10:00:00Z",
            economic_order_key="provider:A:execution:b",
            observed_at="2026-01-02T10:00:00Z",
            corrects_transaction_id=original_b.transaction_id,
        )
        book.append_batch((reversal, replacement_b))

        projection = project_equity_position(
            book,
            instrument="ABC",
            settlement_currency="USD",
        )
        self.assertEqual(projection.realized_pnl, Decimal("200"))
        self.assertEqual(projection.open_cost_basis, Decimal("200"))
        self.assertEqual(projection.lots[0].transaction_id, "buy-b-r2")

    def test_duplicate_effective_order_key_fails_closed_during_restatement(self):
        book = EconomicBook()
        original = book_equity_fill(
            transaction_id="buy-1-original",
            cause_event_id="cause-buy-1",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="1",
            price="100",
            economic_effective_at="2026-01-01T10:00:00Z",
            economic_order_key="provider:A:execution:same",
        )
        duplicate = book_equity_fill(
            transaction_id="buy-2",
            cause_event_id="cause-buy-2",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="1",
            price="101",
            economic_effective_at="2026-01-01T10:00:00Z",
            economic_order_key="provider:A:execution:same",
        )
        book.append(original)
        book.append(duplicate)
        reversal = reverse_transaction(
            original,
            transaction_id="reverse-buy-1",
            cause_event_id="corr-buy-1-reversal",
            observed_at="2026-01-02T10:00:00Z",
        )
        replacement = book_equity_fill(
            transaction_id="buy-1-r2",
            cause_event_id="corr-buy-1-replacement",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="1",
            price="100",
            economic_effective_at="2026-01-01T10:00:00Z",
            economic_order_key="provider:A:execution:same",
            observed_at="2026-01-02T10:00:00Z",
            corrects_transaction_id=original.transaction_id,
        )
        book.append_batch((reversal, replacement))

        with self.assertRaisesRegex(AccountingConflict, "ambiguous economic ordering"):
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
