from __future__ import annotations

import unittest

from mvp.autotrade_mvp.accounting import AccountingConflict, ScopedEconomicBook
from mvp.autotrade_mvp.fill_accounting import (
    ProjectedFillEvidence,
    book_provider_fill,
    book_provider_fill_correction,
)
from mvp.autotrade_mvp.reconciliation import ProviderFillEvidence


class ProviderFillCorrectionDerivativeIdentityTests(unittest.TestCase):
    def test_original_position_effect_cannot_be_sanitized_by_correction(self):
        original_projected = ProjectedFillEvidence.create(
            fill_id="fill-derivative-marker-r1",
            provider_execution_id="exec-derivative-marker",
            intent_id="intent-derivative-marker",
            client_order_id="client-derivative-marker",
            side="BUY",
            quantity="2",
            price="100",
            position_effect="OPEN",
        )
        original_provider = ProviderFillEvidence.create(
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
            provider_execution_id="exec-derivative-marker",
            client_order_id="client-derivative-marker",
            instrument="ABC",
            side="BUY",
            position_effect="OPEN",
            quantity="2",
            price="100",
            fee_amount="1",
            fee_currency="USD",
            trade_time="2026-01-01T00:00:00Z",
        )
        corrected_projected = ProjectedFillEvidence.create(
            fill_id="fill-derivative-marker-r2",
            provider_execution_id="exec-derivative-marker",
            intent_id="intent-derivative-marker",
            client_order_id="client-derivative-marker",
            side="BUY",
            quantity="2",
            price="101",
            provider_revision="r2",
            correction_of="fill-derivative-marker-r1",
        )
        corrected_provider = ProviderFillEvidence.create(
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
            provider_execution_id="exec-derivative-marker",
            client_order_id="client-derivative-marker",
            instrument="ABC",
            side="BUY",
            quantity="2",
            price="101",
            fee_amount="1",
            fee_currency="USD",
            trade_time="2026-01-01T00:00:01Z",
        )
        book = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
        self.assertTrue(
            book_provider_fill(
                book=book,
                provider_id="provider-a",
                projected_fill=original_projected,
                provider_fill=original_provider,
                expected_instrument="ABC",
                settlement_currency="USD",
            )
        )
        before_transactions = book.transactions
        before_digest = book.audit_digest()

        with self.assertRaisesRegex(
            AccountingConflict,
            "cash-equity correction rejects derivative position identity",
        ):
            book_provider_fill_correction(
                book=book,
                provider_id="provider-a",
                original_projected_fill=original_projected,
                original_provider_fill=original_provider,
                corrected_projected_fill=corrected_projected,
                corrected_provider_fill=corrected_provider,
                expected_instrument="ABC",
                settlement_currency="USD",
                correction_observed_at="2026-01-01T00:00:02Z",
            )

        self.assertEqual(book.transactions, before_transactions)
        self.assertEqual(book.audit_digest(), before_digest)


if __name__ == "__main__":
    unittest.main()
