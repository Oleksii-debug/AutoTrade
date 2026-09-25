from decimal import Decimal
import unittest

from mvp.autotrade_mvp.accounting import (
    AccountingConflict,
    ScopedEconomicBook,
    book_equity_fill,
)
from mvp.autotrade_mvp.fill_accounting import (
    ProjectedFillEvidence,
    book_provider_fill,
    book_provider_fill_correction,
    build_provider_fill_correction_transactions,
    build_provider_fill_transaction,
)
from mvp.autotrade_mvp.reconciliation import ProviderFillEvidence


def matched_fill(*, price="100", fee="1", revision=None):
    projected = ProjectedFillEvidence.create(
        fill_id="fill-1",
        provider_execution_id="exec-1",
        intent_id="intent-1",
        client_order_id="client-1",
        side="BUY",
        quantity="2",
        price=price,
        provider_revision=revision,
    )
    provider = ProviderFillEvidence.create(
        provider_id="SIMULATED",
        account_id="paper-1",
        environment="PAPER",
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
            provider_id="SIMULATED",
            account_id="paper-1",
            environment="PAPER",
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
            provider_id="SIMULATED",
            account_id="paper-1",
            environment="PAPER",
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

        wrong_client = ProviderFillEvidence.create(
            provider_id="SIMULATED",
            account_id="paper-1",
            environment="PAPER",
            provider_execution_id="exec-1",
            client_order_id="client-other",
            instrument="ABC",
            quantity="2",
            price="100",
            fee_amount="1",
            fee_currency="USD",
            trade_time="2026-01-01T00:00:00Z",
        )
        with self.assertRaisesRegex(AccountingConflict, "client order identity"):
            build_provider_fill_transaction(
                book=book,
                provider_id="provider-a",
                projected_fill=observed,
                provider_fill=wrong_client,
                expected_instrument="ABC",
                settlement_currency="USD",
            )

        wrong_quantity = ProviderFillEvidence.create(
            provider_id="SIMULATED",
            account_id="paper-1",
            environment="PAPER",
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
            client_order_id="client-1",
            side="BUY",
            quantity="2",
            price="101",
            provider_revision="r2",
            correction_of="fill-1",
        )
        corrected_provider = ProviderFillEvidence.create(
            provider_id="SIMULATED",
            account_id="paper-1",
            environment="PAPER",
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
            client_order_id=" client-1 ",
            side=" buy ",
            quantity="2.00",
            price="100.0",
        )
        self.assertEqual(observed.fill_id, "fill-1")
        self.assertEqual(observed.provider_execution_id, "exec-1")
        self.assertEqual(observed.intent_id, "intent-1")
        self.assertEqual(observed.client_order_id, "client-1")
        self.assertEqual(observed.side, "BUY")
        self.assertEqual(observed.quantity, Decimal("2.00"))
        with self.assertRaises(TypeError):
            ProjectedFillEvidence.create(
                fill_id="f",
                provider_execution_id="e",
                intent_id="i",
                client_order_id="c",
                side="BUY",
                quantity=1.0,
                price="100",
            )


    def test_provider_identity_case_cannot_double_book_same_execution(self):
        observed, provider = matched_fill()
        book = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
        common = dict(
            book=book,
            projected_fill=observed,
            provider_fill=provider,
            expected_instrument="ABC",
            settlement_currency="USD",
        )
        self.assertTrue(book_provider_fill(provider_id="provider-a", **common))
        self.assertFalse(book_provider_fill(provider_id=" PROVIDER-A ", **common))
        self.assertEqual(book.position("ABC"), Decimal("2"))
        self.assertEqual(book.cash("USD"), Decimal("-201"))
        self.assertEqual(len(book.transactions), 1)


    def test_corrected_fill_atomically_reverses_and_replaces_original_economics(self):
        original_projected, original_provider = matched_fill()
        corrected_projected = ProjectedFillEvidence.create(
            fill_id="fill-1-r2",
            provider_execution_id="exec-1",
            intent_id="intent-1",
            client_order_id="client-1",
            side="BUY",
            quantity="2",
            price="101",
            provider_revision="r2",
            correction_of="fill-1",
        )
        corrected_provider = ProviderFillEvidence.create(
            provider_id="SIMULATED",
            account_id="paper-1",
            environment="PAPER",
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            quantity="2",
            price="101",
            fee_amount="1",
            fee_currency="USD",
            trade_time="2026-01-01T00:00:01Z",
        )
        book = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
        self.assertTrue(book_provider_fill(
            book=book,
            provider_id="provider-a",
            projected_fill=original_projected,
            provider_fill=original_provider,
            expected_instrument="ABC",
            settlement_currency="USD",
        ))

        args = dict(
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
        self.assertTrue(book_provider_fill_correction(**args))
        self.assertEqual(book.position("ABC"), Decimal("2"))
        self.assertEqual(book.cash("USD"), Decimal("-203"))
        self.assertEqual(book.fee_expense("USD"), Decimal("1"))
        self.assertEqual(len(book.transactions), 3)
        self.assertEqual(
            book.transactions[1].reverses_transaction_id,
            book.transactions[0].transaction_id,
        )

        before_transactions = book.transactions
        before_digest = book.audit_digest()
        conflicting_revision = ProjectedFillEvidence.create(
            fill_id="fill-1-r2",
            provider_execution_id="exec-1",
            intent_id="intent-1",
            client_order_id="client-1",
            side="BUY",
            quantity="2",
            price="101",
            provider_revision="r3",
            correction_of="fill-1",
        )
        conflicting_args = {
            **args,
            "corrected_projected_fill": conflicting_revision,
        }
        with self.assertRaisesRegex(
            AccountingConflict,
            "immutable correction evidence",
        ):
            book_provider_fill_correction(**conflicting_args)
        self.assertEqual(book.transactions, before_transactions)
        self.assertEqual(book.audit_digest(), before_digest)

        self.assertFalse(book_provider_fill_correction(**args))
        self.assertEqual(len(book.transactions), 3)
        self.assertEqual(book.cash("USD"), Decimal("-203"))

    def test_corrected_fill_requires_provider_revision_before_any_mutation(self):
        original_projected, original_provider = matched_fill()
        corrected_projected = ProjectedFillEvidence.create(
            fill_id="fill-1-r2",
            provider_execution_id="exec-1",
            intent_id="intent-1",
            client_order_id="client-1",
            side="BUY",
            quantity="2",
            price="101",
            provider_revision=None,
            correction_of="fill-1",
        )
        corrected_provider = ProviderFillEvidence.create(
            provider_id="SIMULATED",
            account_id="paper-1",
            environment="PAPER",
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            quantity="2",
            price="101",
            fee_amount="1",
            fee_currency="USD",
            trade_time="2026-01-01T00:00:01Z",
        )
        book = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
        self.assertTrue(book_provider_fill(
            book=book,
            provider_id="provider-a",
            projected_fill=original_projected,
            provider_fill=original_provider,
            expected_instrument="ABC",
            settlement_currency="USD",
        ))
        before_transactions = book.transactions
        before_digest = book.audit_digest()

        with self.assertRaisesRegex(
            AccountingConflict,
            "provider_revision",
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
        self.assertEqual(len(book.transactions), 1)

    def test_correction_before_original_booking_fails_without_mutation(self):
        original_projected, original_provider = matched_fill()
        corrected_projected = ProjectedFillEvidence.create(
            fill_id="fill-1-r2",
            provider_execution_id="exec-1",
            intent_id="intent-1",
            client_order_id="client-1",
            side="BUY",
            quantity="2",
            price="101",
            provider_revision="r2",
            correction_of="fill-1",
        )
        corrected_provider = ProviderFillEvidence.create(
            provider_id="SIMULATED",
            account_id="paper-1",
            environment="PAPER",
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            quantity="2",
            price="101",
            fee_amount="1",
            fee_currency="USD",
            trade_time="2026-01-01T00:00:01Z",
        )
        book = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
        before = book.audit_digest()
        with self.assertRaisesRegex(AccountingConflict, "original provider fill"):
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
        self.assertEqual(book.audit_digest(), before)
        self.assertEqual(book.transactions, ())

    def test_correction_identity_mismatches_fail_closed(self):
        original_projected, original_provider = matched_fill()
        base = dict(
            fill_id="fill-1-r2",
            provider_execution_id="exec-1",
            intent_id="intent-1",
            client_order_id="client-1",
            side="BUY",
            quantity="2",
            price="101",
            provider_revision="r2",
            correction_of="fill-1",
        )
        corrected_provider = ProviderFillEvidence.create(
            provider_id="SIMULATED",
            account_id="paper-1",
            environment="PAPER",
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            quantity="2",
            price="101",
            fee_amount="1",
            fee_currency="USD",
            trade_time="2026-01-01T00:00:01Z",
        )
        cases = (
            ("correction_of", {**base, "correction_of": "other-fill"}, corrected_provider),
            ("intent", {**base, "intent_id": "other-intent"}, corrected_provider),
            (
                "client_order_id",
                {**base, "client_order_id": "other-client"},
                corrected_provider,
            ),
            (
                "instrument",
                base,
                ProviderFillEvidence.create(
                    provider_id="SIMULATED",
                    account_id="paper-1",
                    environment="PAPER",
                    provider_execution_id="exec-1",
                    client_order_id="client-1",
                    instrument="XYZ",
                    quantity="2",
                    price="101",
                    fee_amount="1",
                    fee_currency="USD",
                    trade_time="2026-01-01T00:00:01Z",
                ),
            ),
        )
        for label, projected_args, provider_evidence in cases:
            with self.subTest(label=label):
                book = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
                self.assertTrue(book_provider_fill(
                    book=book,
                    provider_id="provider-a",
                    projected_fill=original_projected,
                    provider_fill=original_provider,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                ))
                before = book.audit_digest()
                with self.assertRaises(AccountingConflict):
                    book_provider_fill_correction(
                        book=book,
                        provider_id="provider-a",
                        original_projected_fill=original_projected,
                        original_provider_fill=original_provider,
                        corrected_projected_fill=ProjectedFillEvidence.create(**projected_args),
                        corrected_provider_fill=provider_evidence,
                        expected_instrument="ABC",
                        settlement_currency="USD",
                        correction_observed_at="2026-01-01T00:00:02Z",
                    )
                self.assertEqual(book.audit_digest(), before)
                self.assertEqual(len(book.transactions), 1)

    def test_second_correction_targets_latest_active_fact_and_restarts(self):
        original_projected, original_provider = matched_fill()
        first_projected = ProjectedFillEvidence.create(
            fill_id="fill-1-r2",
            provider_execution_id="exec-1",
            intent_id="intent-1",
            client_order_id="client-1",
            side="BUY",
            quantity="2",
            price="101",
            provider_revision="r2",
            correction_of="fill-1",
        )
        first_provider = ProviderFillEvidence.create(
            provider_id="SIMULATED",
            account_id="paper-1",
            environment="PAPER",
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            quantity="2",
            price="101",
            fee_amount="1",
            fee_currency="USD",
            trade_time="2026-01-01T00:00:01Z",
        )
        second_projected = ProjectedFillEvidence.create(
            fill_id="fill-1-r3",
            provider_execution_id="exec-1",
            intent_id="intent-1",
            client_order_id="client-1",
            side="BUY",
            quantity="2",
            price="102",
            provider_revision="r3",
            correction_of="fill-1-r2",
        )
        second_provider = ProviderFillEvidence.create(
            provider_id="SIMULATED",
            account_id="paper-1",
            environment="PAPER",
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            quantity="2",
            price="102",
            fee_amount="1",
            fee_currency="USD",
            trade_time="2026-01-01T00:00:02Z",
        )
        book = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
        self.assertTrue(book_provider_fill(
            book=book,
            provider_id="provider-a",
            projected_fill=original_projected,
            provider_fill=original_provider,
            expected_instrument="ABC",
            settlement_currency="USD",
            observed_at="2026-01-01T00:00:00.500000Z",
        ))
        self.assertTrue(book_provider_fill_correction(
            book=book,
            provider_id="provider-a",
            original_projected_fill=original_projected,
            original_provider_fill=original_provider,
            corrected_projected_fill=first_projected,
            corrected_provider_fill=first_provider,
            expected_instrument="ABC",
            settlement_currency="USD",
            correction_observed_at="2026-01-01T00:00:03Z",
        ))
        first_replacement = book.transactions[-1]
        self.assertTrue(book_provider_fill_correction(
            book=book,
            provider_id="provider-a",
            original_projected_fill=first_projected,
            original_provider_fill=first_provider,
            corrected_projected_fill=second_projected,
            corrected_provider_fill=second_provider,
            expected_instrument="ABC",
            settlement_currency="USD",
            correction_observed_at="2026-01-01T00:00:04Z",
        ))
        self.assertEqual(
            book.transactions[-1].corrects_transaction_id,
            first_replacement.transaction_id,
        )
        self.assertEqual(book.position("ABC"), Decimal("2"))
        self.assertEqual(book.cash("USD"), Decimal("-205"))
        self.assertEqual(book.fee_expense("USD"), Decimal("1"))
        self.assertEqual(len(book.transactions), 5)
        restarted = ScopedEconomicBook(
            environment="PAPER",
            account_id="acct-1",
            transactions=book.transactions,
        )
        self.assertEqual(restarted.audit_digest(), book.audit_digest())

    def test_replacement_conflict_cannot_publish_candidate_reversal(self):
        original_projected, original_provider = matched_fill()
        corrected_projected = ProjectedFillEvidence.create(
            fill_id="fill-1-r2",
            provider_execution_id="exec-1",
            intent_id="intent-1",
            client_order_id="client-1",
            side="BUY",
            quantity="2",
            price="101",
            provider_revision="r2",
            correction_of="fill-1",
        )
        corrected_provider = ProviderFillEvidence.create(
            provider_id="SIMULATED",
            account_id="paper-1",
            environment="PAPER",
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            quantity="2",
            price="101",
            fee_amount="1",
            fee_currency="USD",
            trade_time="2026-01-01T00:00:01Z",
        )
        book = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
        self.assertTrue(book_provider_fill(
            book=book,
            provider_id="provider-a",
            projected_fill=original_projected,
            provider_fill=original_provider,
            expected_instrument="ABC",
            settlement_currency="USD",
        ))
        _reversal, replacement = build_provider_fill_correction_transactions(
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
        conflict = book_equity_fill(
            transaction_id=replacement.transaction_id,
            cause_event_id="injected-conflicting-replacement",
            instrument="ABC",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="999",
            fee="1",
            fee_currency="USD",
        )
        self.assertTrue(book.append(conflict))
        before_transactions = book.transactions
        before_digest = book.audit_digest()

        with self.assertRaises(AccountingConflict):
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
        self.assertFalse(
            any(
                transaction.reverses_transaction_id == book.transactions[0].transaction_id
                for transaction in book.transactions
            )
        )



if __name__ == "__main__":
    unittest.main()
