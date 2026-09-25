from decimal import Decimal
import unittest

from mvp.autotrade_mvp.accounting import AccountingConflict, ScopedEconomicBook
from mvp.autotrade_mvp.fill_accounting import (
    ProjectedFillEvidence,
    book_provider_fill,
    build_provider_fill_transaction,
)
from mvp.autotrade_mvp.reconciliation import ProviderFillEvidence


def matched_fill(*, price="100", fee="1", revision=None):
    projected = ProjectedFillEvidence.create(
        fill_id="fill-1",
        provider_execution_id="exec-1",
        intent_id="intent-1",
        side="BUY",
        quantity="2",
        price=price,
        provider_revision=revision,
    )
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
    return projected, provider


class FillAccountingTests(unittest.TestCase):
    def test_only_matched_fill_evidence_books_economics(self):
        observed, provider = matched_fill()
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
        observed, provider = matched_fill()
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
        observed, provider = matched_fill()
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

    def test_acknowledgement_like_object_cannot_be_booked_as_fill(self):
        class Acknowledgement:
            provider_order_id = "order-1"

        _, provider = matched_fill()
        book = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
        with self.assertRaisesRegex(TypeError, "not an acknowledgement"):
            build_provider_fill_transaction(
                book=book,
                provider_id="provider-a",
                projected_fill=Acknowledgement(),
                provider_fill=provider,
                expected_instrument="ABC",
                settlement_currency="USD",
            )
        self.assertEqual(book.transactions, ())

    def test_independent_evidence_must_match_execution_quantity_price_and_instrument(self):
        observed, provider = matched_fill()
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
        _, provider = matched_fill()
        corrected = ProjectedFillEvidence.create(
            fill_id="fill-1-r2",
            provider_execution_id="exec-1",
            intent_id="intent-1",
            side="BUY",
            quantity="2",
            price="101",
            provider_revision="r2",
            correction_of="fill-1",
        )
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
        observed, provider = matched_fill()
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

    def test_projection_evidence_normalizes_identity_and_rejects_binary_float(self):
        observed = ProjectedFillEvidence.create(
            fill_id=" fill-1 ",
            provider_execution_id=" exec-1 ",
            intent_id=" intent-1 ",
            side=" buy ",
            quantity="2.00",
            price="100.0",
        )
        self.assertEqual(observed.fill_id, "fill-1")
        self.assertEqual(observed.provider_execution_id, "exec-1")
        self.assertEqual(observed.intent_id, "intent-1")
        self.assertEqual(observed.side, "BUY")
        self.assertEqual(observed.quantity, Decimal("2.00"))
        with self.assertRaises(TypeError):
            ProjectedFillEvidence.create(
                fill_id="f",
                provider_execution_id="e",
                intent_id="i",
                side="BUY",
                quantity=1.0,
                price="100",
            )


if __name__ == "__main__":
    unittest.main()
