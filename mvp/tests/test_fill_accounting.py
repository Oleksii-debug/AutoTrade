from decimal import Decimal
import unittest

from mvp.autotrade_mvp.accounting import AccountingConflict, ScopedEconomicBook
from mvp.autotrade_mvp.fill_accounting import (
    book_provider_fill,
    build_provider_fill_transaction,
)
from mvp.autotrade_mvp.orders import OrderProjection
from mvp.autotrade_mvp.reconciliation import ProviderFillEvidence


def matched_fill(*, price="100", fee="1", revision=None):
    projection = OrderProjection()
    projection.register_intent(intent_id="intent-1", side="BUY", quantity="2")
    projection.observe_fill(
        fill_id="fill-1",
        provider_execution_id="exec-1",
        intent_id="intent-1",
        side="BUY",
        quantity="2",
        price=price,
        provider_revision=revision,
    )
    observed = projection.effective_fills()[0]
    provider = ProviderFillEvidence.create(
        provider_execution_id="exec-1",
        client_order_id="client-1",
        instrument="ABC",
        quantity="2",
        price=price,
        fee_amount=fee,
        fee_currency="USD",
        trade_time="2026-01-01T00:00:00Z",
    )
    return projection, observed, provider


class FillAccountingTests(unittest.TestCase):
    def test_only_matched_fill_evidence_books_economics(self):
        _, observed, provider = matched_fill()
        book = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
        self.assertTrue(book_provider_fill(
            book=book,
            provider_id="provider-a",
            projected_fill=observed,
            provider_fill=provider,
            expected_instrument="ABC",
            settlement_currency="USD",
        ))
        self.assertEqual(book.position("ABC"), Decimal("2"))
        self.assertEqual(book.cash("USD"), Decimal("-201"))
        self.assertEqual(book.fee_expense("USD"), Decimal("1"))

    def test_same_provider_execution_is_idempotent(self):
        _, observed, provider = matched_fill()
        book = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
        args = dict(
            book=book,
            provider_id="provider-a",
            projected_fill=observed,
            provider_fill=provider,
            expected_instrument="ABC",
            settlement_currency="USD",
        )
        self.assertTrue(book_provider_fill(**args))
        self.assertFalse(book_provider_fill(**args))
        self.assertEqual(book.position("ABC"), Decimal("2"))

    def test_conflicting_observation_for_same_execution_fails_closed(self):
        _, observed, provider = matched_fill()
        book = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
        self.assertTrue(book_provider_fill(
            book=book,
            provider_id="provider-a",
            projected_fill=observed,
            provider_fill=provider,
            expected_instrument="ABC",
            settlement_currency="USD",
        ))
        changed_provider = ProviderFillEvidence.create(
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            quantity="2",
            price="101",
            fee_amount="1",
            fee_currency="USD",
            trade_time="2026-01-01T00:00:00Z",
        )
        with self.assertRaisesRegex(AccountingConflict, "price"):
            book_provider_fill(
                book=book,
                provider_id="provider-a",
                projected_fill=observed,
                provider_fill=changed_provider,
                expected_instrument="ABC",
                settlement_currency="USD",
            )
        self.assertEqual(book.position("ABC"), Decimal("2"))

    def test_acknowledgement_snapshot_cannot_be_booked_as_fill(self):
        projection = OrderProjection()
        projection.register_intent(intent_id="intent-1", side="BUY", quantity="2")
        projection.acknowledge("intent-1", provider_order_id="order-1")
        ack = projection.snapshot("intent-1")
        _, _, provider = matched_fill()
        book = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
        with self.assertRaisesRegex(TypeError, "not an acknowledgement"):
            build_provider_fill_transaction(
                book=book,
                provider_id="provider-a",
                projected_fill=ack,
                provider_fill=provider,
                expected_instrument="ABC",
                settlement_currency="USD",
            )
        self.assertEqual(book.transactions, ())

    def test_independent_evidence_must_match_execution_quantity_price_and_instrument(self):
        _, observed, provider = matched_fill()
        book = ScopedEconomicBook(environment="PAPER", account_id="acct-1")

        wrong_execution = ProviderFillEvidence.create(
            provider_execution_id="exec-other",
            client_order_id="client-1",
            instrument="ABC",
            quantity="2",
            price="100",
            fee_amount="1",
            fee_currency="USD",
            trade_time="2026-01-01T00:00:00Z",
        )
        with self.assertRaisesRegex(AccountingConflict, "execution identity"):
            build_provider_fill_transaction(
                book=book,
                provider_id="provider-a",
                projected_fill=observed,
                provider_fill=wrong_execution,
                expected_instrument="ABC",
                settlement_currency="USD",
            )

        wrong_quantity = ProviderFillEvidence.create(
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            quantity="3",
            price="100",
            fee_amount="1",
            fee_currency="USD",
            trade_time="2026-01-01T00:00:00Z",
        )
        with self.assertRaisesRegex(AccountingConflict, "quantity"):
            build_provider_fill_transaction(
                book=book,
                provider_id="provider-a",
                projected_fill=observed,
                provider_fill=wrong_quantity,
                expected_instrument="ABC",
                settlement_currency="USD",
            )

        with self.assertRaisesRegex(AccountingConflict, "instrument"):
            build_provider_fill_transaction(
                book=book,
                provider_id="provider-a",
                projected_fill=observed,
                provider_fill=provider,
                expected_instrument="XYZ",
                settlement_currency="USD",
            )

    def test_corrected_fill_is_blocked_until_atomic_correction_evidence_exists(self):
        projection, original, provider = matched_fill()
        projection.correct_fill(
            original.fill_id,
            correction_fill_id="fill-1-r2",
            provider_execution_id="exec-1",
            quantity="2",
            price="101",
            provider_revision="r2",
        )
        corrected = projection.effective_fills()[0]
        corrected_provider = ProviderFillEvidence.create(
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            quantity="2",
            price="101",
            fee_amount="1",
            fee_currency="USD",
            trade_time="2026-01-01T00:00:00Z",
        )
        with self.assertRaisesRegex(AccountingConflict, "atomic reversal"):
            build_provider_fill_transaction(
                book=ScopedEconomicBook(environment="PAPER", account_id="acct-1"),
                provider_id="provider-a",
                projected_fill=corrected,
                provider_fill=corrected_provider,
                expected_instrument="ABC",
                settlement_currency="USD",
            )

    def test_scope_is_part_of_audit_identity(self):
        _, observed, provider = matched_fill()
        paper = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
        live = ScopedEconomicBook(environment="LIVE", account_id="acct-1")
        other_account = ScopedEconomicBook(environment="PAPER", account_id="acct-2")
        for book in (paper, live, other_account):
            book_provider_fill(
                book=book,
                provider_id="provider-a",
                projected_fill=observed,
                provider_fill=provider,
                expected_instrument="ABC",
                settlement_currency="USD",
            )
        self.assertNotEqual(paper.audit_digest(), live.audit_digest())
        self.assertNotEqual(paper.audit_digest(), other_account.audit_digest())


if __name__ == "__main__":
    unittest.main()
