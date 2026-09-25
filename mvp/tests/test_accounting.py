from decimal import Decimal
import unittest

from mvp.autotrade_mvp.accounting import (
    AccountingConflict,
    EconomicBook,
    JournalTransaction,
    Posting,
    book_equity_fill,
    book_external_cash_flow,
    book_fx_exchange,
    posting,
    project_equity_position,
    reverse_transaction,
    transaction_digest,
    validate_transaction,
)
from mvp.autotrade_mvp.economics import cash_round_trip


class AccountingFoundationTests(unittest.TestCase):
    def test_cash_equity_round_trip_matches_independent_oracle(self):
        book = EconomicBook()
        book.append(book_external_cash_flow(
            transaction_id="deposit-1",
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
        oracle = cash_round_trip(
            start_cash="1000",
            buy_quantity="2",
            buy_price="100",
            buy_fee="1",
            sell_quantity="1",
            sell_price="110",
            sell_fee="0.50",
            mark_price="105",
        )
        self.assertEqual(book.cash("USD"), oracle.cash)
        self.assertEqual(book.position("ABC"), oracle.position)
        self.assertEqual(book.fee_expense("USD"), oracle.fees)

    def test_third_currency_fee_and_rebate_are_not_silently_converted(self):
        book = EconomicBook()
        book.append(book_external_cash_flow(
            transaction_id="usd",
            cause_event_id="cash-usd",
            currency="USD",
            amount="1000",
        ))
        book.append(book_external_cash_flow(
            transaction_id="eur",
            cause_event_id="cash-eur",
            currency="EUR",
            amount="10",
        ))
        book.append(book_equity_fill(
            transaction_id="buy",
            cause_event_id="fill-buy",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="1",
            price="100",
            fee="2",
            fee_currency="EUR",
        ))
        book.append(book_equity_fill(
            transaction_id="sell",
            cause_event_id="fill-sell",
            instrument="ABC",
            settlement_currency="USD",
            side="SELL",
            quantity="1",
            price="100",
            fee="-0.50",
            fee_currency="EUR",
        ))
        self.assertEqual(book.cash("USD"), Decimal("1000"))
        self.assertEqual(book.cash("EUR"), Decimal("8.50"))
        self.assertEqual(book.fee_expense("EUR"), Decimal("1.50"))

    def test_fx_exchange_balances_each_currency_separately(self):
        book = EconomicBook()
        book.append(book_external_cash_flow(
            transaction_id="seed",
            cause_event_id="cash",
            currency="USD",
            amount="1000",
        ))
        trade = book_fx_exchange(
            transaction_id="fx-1",
            cause_event_id="fx-fill",
            sold_currency="USD",
            sold_amount="110",
            bought_currency="EUR",
            bought_amount="100",
        )
        book.append(trade)
        self.assertEqual(book.cash("USD"), Decimal("890"))
        self.assertEqual(book.cash("EUR"), Decimal("100"))
        for asset in {"USD", "EUR"}:
            self.assertEqual(
                sum(p.signed_amount for p in trade.postings if p.asset_or_currency == asset),
                Decimal("0"),
            )

    def test_exact_reversal_restores_balances(self):
        book = EconomicBook()
        book.append(book_external_cash_flow(
            transaction_id="seed",
            cause_event_id="cash",
            currency="USD",
            amount="1000",
        ))
        fill = book_equity_fill(
            transaction_id="fill",
            cause_event_id="fill-event",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="100",
            fee="1",
        )
        book.append(fill)
        book.append(reverse_transaction(
            fill,
            transaction_id="fill-reversal",
            cause_event_id="correction",
        ))
        self.assertEqual(book.cash("USD"), Decimal("1000"))
        self.assertEqual(book.position("ABC"), Decimal("0"))
        self.assertEqual(book.fee_expense("USD"), Decimal("0"))

    def test_same_transaction_cannot_be_reversed_twice(self):
        book = EconomicBook()
        original = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit",
            currency="USD",
            amount="100",
        )
        book.append(original)
        book.append(reverse_transaction(
            original,
            transaction_id="reverse-1",
            cause_event_id="correction-1",
        ))
        with self.assertRaises(AccountingConflict):
            book.append(reverse_transaction(
                original,
                transaction_id="reverse-2",
                cause_event_id="correction-2",
            ))

    def test_duplicate_transaction_is_idempotent_but_changed_content_conflicts(self):
        book = EconomicBook()
        original = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit",
            currency="USD",
            amount="100",
        )
        self.assertTrue(book.append(original))
        self.assertFalse(book.append(original))
        changed = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit",
            currency="USD",
            amount="101",
        )
        with self.assertRaises(AccountingConflict):
            book.append(changed)

    def test_same_economic_cause_cannot_be_booked_under_two_transaction_ids(self):
        book = EconomicBook()
        first = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="provider-activity-1",
            currency="USD",
            amount="100",
        )
        duplicate_cause = book_external_cash_flow(
            transaction_id="cash-2",
            cause_event_id="provider-activity-1",
            currency="USD",
            amount="100",
        )
        self.assertTrue(book.append(first))
        with self.assertRaisesRegex(AccountingConflict, "cause_event_id"):
            book.append(duplicate_cause)
        self.assertEqual(book.cash("USD"), Decimal("100"))
        self.assertEqual(len(book.transactions), 1)

    def test_same_fill_cause_cannot_double_position_or_fee(self):
        book = EconomicBook()
        first = book_equity_fill(
            transaction_id="fill-posting-1",
            cause_event_id="provider-execution-abc",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="100",
            fee="1",
        )
        duplicate = book_equity_fill(
            transaction_id="fill-posting-2",
            cause_event_id="provider-execution-abc",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="100",
            fee="1",
        )
        book.append(first)
        with self.assertRaises(AccountingConflict):
            book.append(duplicate)
        self.assertEqual(book.position("ABC"), Decimal("2"))
        self.assertEqual(book.fee_expense("USD"), Decimal("1"))

    def test_reversal_requires_its_own_distinct_cause_event(self):
        book = EconomicBook()
        original = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="provider-cash-event",
            currency="USD",
            amount="100",
        )
        book.append(original)
        reversal = reverse_transaction(
            original,
            transaction_id="cash-reversal",
            cause_event_id="provider-cash-event",
        )
        with self.assertRaisesRegex(AccountingConflict, "cause_event_id"):
            book.append(reversal)
        self.assertEqual(book.cash("USD"), Decimal("100"))

    def test_constructor_detects_duplicate_cause_history(self):
        first = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="same-cause",
            currency="USD",
            amount="100",
        )
        second = book_external_cash_flow(
            transaction_id="cash-2",
            cause_event_id="same-cause",
            currency="USD",
            amount="-100",
        )
        with self.assertRaisesRegex(AccountingConflict, "cause_event_id"):
            EconomicBook([first, second])

    def test_canonical_cause_identity_cannot_be_bypassed_with_whitespace(self):
        book = EconomicBook()
        first = JournalTransaction(
            transaction_id="raw-1",
            cause_event_id=" provider-execution-abc ",
            postings=(
                posting("CASH:USD", "USD", "100"),
                posting("EXTERNAL_EQUITY:USD", "USD", "-100"),
            ),
        )
        second = JournalTransaction(
            transaction_id="raw-2",
            cause_event_id="provider-execution-abc",
            postings=(
                posting("CASH:USD", "USD", "100"),
                posting("EXTERNAL_EQUITY:USD", "USD", "-100"),
            ),
        )
        self.assertTrue(book.append(first))
        with self.assertRaisesRegex(AccountingConflict, "cause_event_id"):
            book.append(second)
        self.assertEqual(book.cash("USD"), Decimal("100"))

    def test_canonical_transaction_identity_cannot_be_split_with_whitespace(self):
        book = EconomicBook()
        first = JournalTransaction(
            transaction_id=" tx-1 ",
            cause_event_id="cause-a",
            postings=(
                posting("CASH:USD", "USD", "100"),
                posting("EXTERNAL_EQUITY:USD", "USD", "-100"),
            ),
        )
        changed = JournalTransaction(
            transaction_id="tx-1",
            cause_event_id="cause-b",
            postings=(
                posting("CASH:USD", "USD", "200"),
                posting("EXTERNAL_EQUITY:USD", "USD", "-200"),
            ),
        )
        self.assertTrue(book.append(first))
        with self.assertRaisesRegex(AccountingConflict, "transaction_id"):
            book.append(changed)
        self.assertEqual(book.cash("USD"), Decimal("100"))

    def test_audit_identity_and_balance_projection_share_canonical_posting_names(self):
        raw = JournalTransaction(
            transaction_id=" tx-whitespace ",
            cause_event_id=" source-event ",
            postings=(
                Posting(" CASH:USD ", " USD ", Decimal("100")),
                Posting(" CLEARING:USD ", " USD ", Decimal("-100")),
            ),
        )
        book = EconomicBook()
        self.assertTrue(book.append(raw))

        self.assertEqual(book.cash("USD"), Decimal("100"))
        stored = book.transactions[0]
        self.assertEqual(stored.transaction_id, "tx-whitespace")
        self.assertEqual(stored.cause_event_id, "source-event")
        self.assertEqual(stored.postings[0].ledger_account, "CASH:USD")
        self.assertEqual(stored.postings[0].asset_or_currency, "USD")
        self.assertEqual(book.audit_digest(), EconomicBook(book.transactions).audit_digest())

    def test_transaction_digest_is_stable_across_equivalent_decimal_scales(self):
        first = JournalTransaction(
            transaction_id="tx",
            cause_event_id="cause",
            postings=(
                posting("CASH:USD", "USD", Decimal("1.0")),
                posting("CLEARING:USD", "USD", Decimal("-1.00")),
            ),
        )
        second = JournalTransaction(
            transaction_id="tx",
            cause_event_id="cause",
            postings=(
                posting("CASH:USD", "USD", Decimal("1.000")),
                posting("CLEARING:USD", "USD", Decimal("-1")),
            ),
        )
        self.assertEqual(transaction_digest(first), transaction_digest(second))

    def test_transaction_digest_changes_with_economic_or_lineage_content(self):
        original = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit",
            currency="USD",
            amount="100",
        )
        changed_amount = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit",
            currency="USD",
            amount="101",
        )
        changed_cause = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit-2",
            currency="USD",
            amount="100",
        )
        self.assertNotEqual(
            transaction_digest(original),
            transaction_digest(changed_amount),
        )
        self.assertNotEqual(
            transaction_digest(original),
            transaction_digest(changed_cause),
        )

    def test_book_audit_digest_is_restart_reproducible_and_order_sensitive(self):
        first_tx = book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit-1",
            currency="USD",
            amount="100",
        )
        second_tx = book_external_cash_flow(
            transaction_id="cash-2",
            cause_event_id="deposit-2",
            currency="EUR",
            amount="50",
        )
        original = EconomicBook((first_tx, second_tx))
        restarted = EconomicBook(original.transactions)
        reordered = EconomicBook((second_tx, first_tx))
        self.assertEqual(original.audit_digest(), restarted.audit_digest())
        self.assertNotEqual(original.audit_digest(), reordered.audit_digest())

    def test_audit_digest_is_not_a_financial_balance_or_authority(self):
        book = EconomicBook()
        book.append(book_external_cash_flow(
            transaction_id="cash-1",
            cause_event_id="deposit",
            currency="USD",
            amount="100",
        ))
        digest = book.audit_digest()
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(book.cash("USD"), Decimal("100"))

    def test_unbalanced_transaction_is_rejected(self):
        transaction = JournalTransaction(
            transaction_id="bad",
            cause_event_id="bad-event",
            postings=(
                posting("CASH:USD", "USD", "10"),
                posting("OTHER:USD", "USD", "-9"),
            ),
        )
        with self.assertRaises(ValueError):
            validate_transaction(transaction)
        with self.assertRaises(ValueError):
            EconomicBook().append(transaction)

    def test_binary_float_inputs_are_rejected(self):
        with self.assertRaises(TypeError):
            book_equity_fill(
                transaction_id="fill",
                cause_event_id="event",
                instrument="ABC",
                settlement_currency="USD",
                side="BUY",
                quantity=1,
                price=100.1,
            )


    def test_atomic_batch_is_all_or_nothing_and_exact_retry_is_idempotent(self):
        book = EconomicBook()
        usd = book_external_cash_flow(
            transaction_id="batch-usd",
            cause_event_id="batch-cash-usd",
            currency="USD",
            amount="10",
        )
        eur = book_external_cash_flow(
            transaction_id="batch-eur",
            cause_event_id="batch-cash-eur",
            currency="EUR",
            amount="5",
        )

        self.assertTrue(book.append_batch((usd, eur)))
        committed_digest = book.audit_digest()
        self.assertFalse(book.append_batch((usd, eur)))
        self.assertEqual(book.audit_digest(), committed_digest)
        self.assertEqual(len(book.transactions), 2)

        gbp = book_external_cash_flow(
            transaction_id="batch-gbp",
            cause_event_id="batch-cash-gbp",
            currency="GBP",
            amount="7",
        )
        conflicting_eur = book_external_cash_flow(
            transaction_id="batch-eur",
            cause_event_id="batch-cash-eur-conflict",
            currency="EUR",
            amount="6",
        )
        before = book.transactions
        before_digest = book.audit_digest()
        with self.assertRaises(AccountingConflict):
            book.append_batch((gbp, conflicting_eur))
        self.assertEqual(book.transactions, before)
        self.assertEqual(book.audit_digest(), before_digest)
        self.assertEqual(book.cash("GBP"), Decimal("0"))


    def test_corrected_fill_restates_fifo_at_original_economic_time(self):
        book = EconomicBook()
        original = book_equity_fill(
            transaction_id="buy-original",
            cause_event_id="fill-buy-original",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="100",
            economic_effective_at="2026-01-01T10:00:00Z",
            economic_order_key="fill-1",
        )
        later_sale = book_equity_fill(
            transaction_id="sell-later",
            cause_event_id="fill-sell-later",
            instrument="ABC",
            settlement_currency="USD",
            side="SELL",
            quantity="1",
            price="110",
            economic_effective_at="2026-01-01T11:00:00Z",
            economic_order_key="fill-2",
        )
        book.append(original)
        book.append(later_sale)
        replacement = book_equity_fill(
            transaction_id="buy-corrected",
            cause_event_id="fill-buy-corrected",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="101",
            economic_effective_at=original.economic_effective_at,
            economic_order_key=original.economic_order_key,
        )
        self.assertTrue(
            book.append_batch(
                (
                    reverse_transaction(
                        original,
                        transaction_id="buy-original-reversal",
                        cause_event_id="fill-buy-correction-reversal",
                    ),
                    replacement,
                )
            )
        )

        projected = project_equity_position(
            book,
            instrument="ABC",
            settlement_currency="USD",
        )
        self.assertEqual(projected.quantity, Decimal("1"))
        self.assertEqual(projected.open_cost_basis, Decimal("101"))
        self.assertEqual(projected.realized_pnl, Decimal("9"))
        self.assertEqual(projected.lots[0].transaction_id, "buy-corrected")

        restarted = EconomicBook(book.transactions)
        self.assertEqual(
            project_equity_position(
                restarted,
                instrument="ABC",
                settlement_currency="USD",
            ),
            projected,
        )

    def test_corrected_fifo_fails_closed_when_active_fill_lacks_order_evidence(self):
        book = EconomicBook()
        original = book_equity_fill(
            transaction_id="buy-original",
            cause_event_id="fill-buy-original",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="100",
            economic_effective_at="2026-01-01T10:00:00Z",
            economic_order_key="fill-1",
        )
        book.append(original)
        book.append(
            book_equity_fill(
                transaction_id="sell-without-order-proof",
                cause_event_id="fill-sell-unordered",
                instrument="ABC",
                settlement_currency="USD",
                side="SELL",
                quantity="1",
                price="110",
            )
        )
        replacement = book_equity_fill(
            transaction_id="buy-corrected",
            cause_event_id="fill-buy-corrected",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="101",
            economic_effective_at=original.economic_effective_at,
            economic_order_key=original.economic_order_key,
        )
        book.append_batch(
            (
                reverse_transaction(
                    original,
                    transaction_id="buy-original-reversal",
                    cause_event_id="fill-buy-correction-reversal",
                ),
                replacement,
            )
        )
        with self.assertRaisesRegex(
            AccountingConflict,
            "economic effective-time",
        ):
            project_equity_position(
                book,
                instrument="ABC",
                settlement_currency="USD",
            )




if __name__ == "__main__":
    unittest.main()
