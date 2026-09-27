from decimal import Decimal
from tempfile import TemporaryDirectory
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
    book_unexpected_provider_fill,
    build_unexpected_provider_fill_transaction,
)
from mvp.autotrade_mvp.reconciliation import ProviderFillEvidence, ReconciliationResult
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook


def matched_fill(
    *,
    price="100",
    fee="1",
    revision=None,
    provider_id="PROVIDER-A",
    account_id="acct-1",
    environment="PAPER",
    provider_side="BUY",
    projected_position_side=None,
    provider_position_side=None,
    projected_position_effect=None,
    provider_position_effect=None,
):
    projected = ProjectedFillEvidence.create(
        fill_id="fill-1",
        provider_execution_id="exec-1",
        intent_id="intent-1",
        client_order_id="client-1",
        side="BUY",
        quantity="2",
        price=price,
        position_side=projected_position_side,
        position_effect=projected_position_effect,
        provider_revision=revision,
    )
    provider = ProviderFillEvidence.create(
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
        provider_execution_id="exec-1",
        client_order_id="client-1",
        instrument="ABC",
        side=provider_side,
        position_side=provider_position_side,
        position_effect=provider_position_effect,
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

    def test_provider_direction_must_be_evidenced_and_match_projection(self):
        observed, wrong_side = matched_fill(provider_side="SELL")
        book = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
        with self.assertRaisesRegex(AccountingConflict, "side does not match"):
            build_provider_fill_transaction(
                book=book,
                provider_id="provider-a",
                projected_fill=observed,
                provider_fill=wrong_side,
                expected_instrument="ABC",
                settlement_currency="USD",
            )
        self.assertEqual(book.transactions, ())

        _, evidenced = matched_fill()
        missing_side = ProviderFillEvidence.create(
            provider_id=evidenced.provider_id,
            account_id=evidenced.account_id,
            environment=evidenced.environment,
            provider_execution_id=evidenced.provider_execution_id,
            client_order_id=evidenced.client_order_id,
            instrument=evidenced.instrument,
            quantity=evidenced.quantity,
            price=evidenced.price,
            fee_amount=evidenced.fee_amount,
            fee_currency=evidenced.fee_currency,
            trade_time=evidenced.trade_time,
        )
        with self.assertRaisesRegex(AccountingConflict, "not independently evidenced"):
            build_provider_fill_transaction(
                book=book,
                provider_id="provider-a",
                projected_fill=observed,
                provider_fill=missing_side,
                expected_instrument="ABC",
                settlement_currency="USD",
            )
        self.assertEqual(book.transactions, ())

    def test_hedge_leg_fill_fails_closed_before_generic_economic_booking(self):
        for position_side, side in (("LONG", "BUY"), ("SHORT", "SELL")):
            with self.subTest(position_side=position_side, side=side):
                projected, provider = matched_fill(
                    provider_side=side,
                    projected_position_side=position_side,
                    provider_position_side=position_side,
                    projected_position_effect="OPEN",
                    provider_position_effect="OPEN",
                )
                if side == "SELL":
                    projected = ProjectedFillEvidence.create(
                        fill_id=projected.fill_id,
                        provider_execution_id=projected.provider_execution_id,
                        intent_id=projected.intent_id,
                        client_order_id=projected.client_order_id,
                        side="SELL",
                        quantity=projected.quantity,
                        price=projected.price,
                        position_side=projected.position_side,
                        position_effect=projected.position_effect,
                    )
                book = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
                before_digest = book.audit_digest()
                with self.assertRaisesRegex(
                    AccountingConflict,
                    "leg-aware economic accounting",
                ):
                    book_provider_fill(
                        book=book,
                        provider_id="provider-a",
                        projected_fill=projected,
                        provider_fill=provider,
                        expected_instrument="ABC",
                        settlement_currency="USD",
                    )
                self.assertEqual(book.transactions, ())
                self.assertEqual(book.audit_digest(), before_digest)

        projected, provider = matched_fill(
            projected_position_side="BOTH",
            provider_position_side="BOTH",
        )
        book = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
        self.assertTrue(
            book_provider_fill(
                book=book,
                provider_id="provider-a",
                projected_fill=projected,
                provider_fill=provider,
                expected_instrument="ABC",
                settlement_currency="USD",
            )
        )
        self.assertEqual(book.position("ABC"), Decimal("2"))

    def test_provider_position_identity_must_match_projection_before_booking(self):
        cases = (
            {
                "projected_position_side": "LONG",
                "provider_position_side": "SHORT",
                "projected_position_effect": "OPEN",
                "provider_position_effect": "OPEN",
                "message": "position side does not match projection",
            },
            {
                "projected_position_side": "LONG",
                "provider_position_side": "LONG",
                "projected_position_effect": "OPEN",
                "provider_position_effect": "REDUCE",
                "message": "position effect does not match projection",
            },
            {
                "projected_position_side": None,
                "provider_position_side": "LONG",
                "projected_position_effect": None,
                "provider_position_effect": "OPEN",
                "message": "projection is missing provider-required position side",
            },
            {
                "projected_position_side": "BOTH",
                "provider_position_side": "BOTH",
                "projected_position_effect": "REDUCE",
                "provider_position_effect": None,
                "message": "position effect is not independently evidenced",
            },
        )
        for case in cases:
            with self.subTest(case=case):
                projected, provider = matched_fill(
                    projected_position_side=case["projected_position_side"],
                    provider_position_side=case["provider_position_side"],
                    projected_position_effect=case["projected_position_effect"],
                    provider_position_effect=case["provider_position_effect"],
                )
                book = ScopedEconomicBook(
                    environment="PAPER",
                    account_id="acct-1",
                )
                before_digest = book.audit_digest()
                with self.assertRaisesRegex(
                    AccountingConflict,
                    case["message"],
                ):
                    book_provider_fill(
                        book=book,
                        provider_id="provider-a",
                        projected_fill=projected,
                        provider_fill=provider,
                        expected_instrument="ABC",
                        settlement_currency="USD",
                    )
                self.assertEqual(book.transactions, ())
                self.assertEqual(book.audit_digest(), before_digest)

    def test_hedge_leg_requires_provider_evidenced_position_effect(self):
        projected, provider = matched_fill(
            projected_position_side="LONG",
            provider_position_side="LONG",
        )
        book = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
        with self.assertRaisesRegex(
            AccountingConflict,
            "position effect is not independently evidenced",
        ):
            book_provider_fill(
                book=book,
                provider_id="provider-a",
                projected_fill=projected,
                provider_fill=provider,
                expected_instrument="ABC",
                settlement_currency="USD",
            )
        self.assertEqual(book.transactions, ())

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
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            side="BUY",
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
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
            provider_execution_id="exec-other",
            client_order_id="client-1",
            instrument="ABC",
            side="BUY",
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
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
            provider_execution_id="exec-1",
            client_order_id="client-other",
            instrument="ABC",
            side="BUY",
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
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            side="BUY",
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

    def test_provider_fill_scope_mismatch_fails_before_mutation(self):
        observed, _ = matched_fill()
        cases = (
            ("provider", "PROVIDER-B", "acct-1", "PAPER"),
            ("account", "PROVIDER-A", "acct-2", "PAPER"),
            ("environment", "PROVIDER-A", "acct-1", "LIVE"),
        )
        for label, provider_id, account_id, environment in cases:
            with self.subTest(label=label):
                provider = ProviderFillEvidence.create(
                    provider_id=provider_id,
                    account_id=account_id,
                    environment=environment,
                    provider_execution_id="exec-1",
                    client_order_id="client-1",
                    instrument="ABC",
                    side="BUY",
                    quantity="2",
                    price="100",
                    fee_amount="1",
                    fee_currency="USD",
                    trade_time="2026-01-01T00:00:00Z",
                )
                book = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
                before_transactions = book.transactions
                before_digest = book.audit_digest()
                with self.assertRaisesRegex(AccountingConflict, "scope"):
                    book_provider_fill(
                        book=book,
                        provider_id="provider-a",
                        projected_fill=observed,
                        provider_fill=provider,
                        expected_instrument="ABC",
                        settlement_currency="USD",
                    )
                self.assertEqual(book.transactions, before_transactions)
                self.assertEqual(book.audit_digest(), before_digest)

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
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            side="BUY",
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
        observed, _ = matched_fill()
        paper = ScopedEconomicBook(environment="PAPER", account_id="acct-1")
        live = ScopedEconomicBook(environment="LIVE", account_id="acct-1")
        other_account = ScopedEconomicBook(environment="PAPER", account_id="acct-2")
        for book in (paper, live, other_account):
            _, provider = matched_fill(
                account_id=book.account_id,
                environment=book.environment,
            )
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
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            side="BUY",
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

    def test_correction_scope_mismatch_fails_before_mutation(self):
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

        def provider_evidence(
            *,
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
            price="101",
            trade_time="2026-01-01T00:00:01Z",
        ):
            return ProviderFillEvidence.create(
                provider_id=provider_id,
                account_id=account_id,
                environment=environment,
                provider_execution_id="exec-1",
                client_order_id="client-1",
                instrument="ABC",
                side="BUY",
                quantity="2",
                price=price,
                fee_amount="1",
                fee_currency="USD",
                trade_time=trade_time,
            )

        corrected_provider = provider_evidence()
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

        cases = (
            (
                "original-provider",
                provider_evidence(provider_id="PROVIDER-B", price="100", trade_time="2026-01-01T00:00:00Z"),
                corrected_provider,
            ),
            (
                "original-account",
                provider_evidence(account_id="acct-2", price="100", trade_time="2026-01-01T00:00:00Z"),
                corrected_provider,
            ),
            (
                "original-environment",
                provider_evidence(environment="LIVE", price="100", trade_time="2026-01-01T00:00:00Z"),
                corrected_provider,
            ),
            (
                "corrected-provider",
                original_provider,
                provider_evidence(provider_id="PROVIDER-B"),
            ),
            (
                "corrected-account",
                original_provider,
                provider_evidence(account_id="acct-2"),
            ),
            (
                "corrected-environment",
                original_provider,
                provider_evidence(environment="LIVE"),
            ),
        )
        for label, supplied_original, supplied_corrected in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(AccountingConflict, "scope"):
                    book_provider_fill_correction(
                        book=book,
                        provider_id="provider-a",
                        original_projected_fill=original_projected,
                        original_provider_fill=supplied_original,
                        corrected_projected_fill=corrected_projected,
                        corrected_provider_fill=supplied_corrected,
                        expected_instrument="ABC",
                        settlement_currency="USD",
                        correction_observed_at="2026-01-01T00:00:02Z",
                    )
                self.assertEqual(book.transactions, before_transactions)
                self.assertEqual(book.audit_digest(), before_digest)

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
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            side="BUY",
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
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            side="BUY",
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
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            side="BUY",
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
                    provider_id="PROVIDER-A",
                    account_id="acct-1",
                    environment="PAPER",
                    provider_execution_id="exec-1",
                    client_order_id="client-1",
                    instrument="XYZ",
                    side="BUY",
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
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            side="BUY",
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
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            side="BUY",
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
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
            provider_execution_id="exec-1",
            client_order_id="client-1",
            instrument="ABC",
            side="BUY",
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



    def _unexpected_result(self, execution_id="external-exec-1"):
        return ReconciliationResult(
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
            provider_environment="PAPER",
            complete=False,
            matched_execution_ids=(),
            unexpected_execution_ids=(execution_id,),
            missing_local_execution_ids=(),
            matched_working_client_order_ids=(),
            unexpected_working_provider_order_ids=(),
            missing_local_working_client_order_ids=(),
            snapshot_consistent=True,
            provider_cash={},
            provider_positions={},
            snapshot_mode="ATOMIC",
            snapshot_query_started_at="2026-01-01T00:00:00Z",
            snapshot_query_completed_at="2026-01-01T00:00:01Z",
            cash_differences={},
            position_differences={},
            submission_resolutions=(),
            blocking_resources=("EXECUTION:" + execution_id,),
            reasons=("unexpected provider execution",),
        )

    def _record_unexpected_checkpoint(self, store, execution_id="external-exec-1"):
        event = record_reconciliation_checkpoint(
            store,
            reconciliation_id="unexpected-" + execution_id,
            result=self._unexpected_result(execution_id=execution_id),
            observed_at="2026-01-01T00:00:02Z",
            host_id="host-1",
            owner_epoch="epoch-1",
        )
        return event["event_id"]

    def _unexpected_fill(self, *, side="BUY", position_side=None, execution_id="external-exec-1"):
        return ProviderFillEvidence.create(
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
            provider_execution_id=execution_id,
            client_order_id=None,
            instrument="ABC",
            side=side,
            position_side=position_side,
            quantity="2",
            price="100",
            fee_amount="1",
            fee_currency="USD",
            trade_time="2026-01-01T00:00:00Z",
            evidence_refs=("sha256:provider-read",),
        )

    def test_unexpected_provider_buy_and_sell_book_signed_economics_exactly_once(self):
        cases = (
            ("BUY", Decimal("2"), Decimal("-201")),
            ("SELL", Decimal("-2"), Decimal("199")),
        )
        for side, expected_position, expected_cash in cases:
            with self.subTest(side=side), TemporaryDirectory() as directory:
                store = JournalStore(directory + "/journal.sqlite3")
                checkpoint_event_id = self._record_unexpected_checkpoint(store)
                book = DurableProviderEconomicBook(
                    store,
                    provider_id="PROVIDER-A",
                    account_id="acct-1",
                    environment="PAPER",
                )
                fill = self._unexpected_fill(side=side)
                self.assertTrue(
                    book_unexpected_provider_fill(
                        store=store,
                        checkpoint_event_id=checkpoint_event_id,
                        book=book,
                        provider_fill=fill,
                        expected_instrument="ABC",
                        settlement_currency="USD",
                    )
                )
                self.assertFalse(
                    book_unexpected_provider_fill(
                        store=store,
                        checkpoint_event_id=checkpoint_event_id,
                        book=book,
                        provider_fill=fill,
                        expected_instrument="ABC",
                        settlement_currency="USD",
                    )
                )
                self.assertEqual(book.position("ABC"), expected_position)
                self.assertEqual(book.cash("USD"), expected_cash)
                self.assertEqual(book.fee_expense("USD"), Decimal("1"))

    def test_unexpected_provider_fill_is_durable_across_restart(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(directory + "/journal.sqlite3")
            checkpoint_event_id = self._record_unexpected_checkpoint(store)
            fill = self._unexpected_fill(side="BUY")
            book = DurableProviderEconomicBook(
                store,
                provider_id="PROVIDER-A",
                account_id="acct-1",
                environment="PAPER",
            )
            self.assertTrue(
                book_unexpected_provider_fill(
                    store=store,
                    checkpoint_event_id=checkpoint_event_id,
                    book=book,
                    provider_fill=fill,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                )
            )
            before = book.audit_digest()

            restarted_store = JournalStore(directory + "/journal.sqlite3")
            restarted = DurableProviderEconomicBook(
                restarted_store,
                provider_id="PROVIDER-A",
                account_id="acct-1",
                environment="PAPER",
            )
            self.assertEqual(restarted.audit_digest(), before)
            self.assertEqual(restarted.position("ABC"), Decimal("2"))
            self.assertEqual(restarted.cash("USD"), Decimal("-201"))
            self.assertFalse(
                book_unexpected_provider_fill(
                    store=restarted_store,
                    checkpoint_event_id=checkpoint_event_id,
                    book=restarted,
                    provider_fill=fill,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                )
            )
            self.assertEqual(restarted.audit_digest(), before)

    def test_unexpected_fill_requires_current_durable_checkpoint_and_provider_direction(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(directory + "/journal.sqlite3")
            fill = self._unexpected_fill()
            book = DurableProviderEconomicBook(
                store,
                provider_id="PROVIDER-A",
                account_id="acct-1",
                environment="PAPER",
            )
            with self.assertRaisesRegex(ValueError, "no reconciliation checkpoint"):
                build_unexpected_provider_fill_transaction(
                    store=store,
                    checkpoint_event_id="caller-authored-not-authority",
                    book=book,
                    provider_fill=fill,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                )
            self.assertEqual(book.transactions, ())

            not_unexpected = self._record_unexpected_checkpoint(
                store,
                execution_id="different-exec",
            )
            with self.assertRaisesRegex(AccountingConflict, "not proven unexpected"):
                build_unexpected_provider_fill_transaction(
                    store=store,
                    checkpoint_event_id=not_unexpected,
                    book=book,
                    provider_fill=fill,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                )

            current = self._record_unexpected_checkpoint(
                store,
                execution_id=fill.provider_execution_id,
            )
            with self.assertRaisesRegex(ValueError, "superseded"):
                build_unexpected_provider_fill_transaction(
                    store=store,
                    checkpoint_event_id=not_unexpected,
                    book=book,
                    provider_fill=fill,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                )

            missing_side = ProviderFillEvidence.create(
                provider_id=fill.provider_id,
                account_id=fill.account_id,
                environment=fill.environment,
                provider_execution_id=fill.provider_execution_id,
                client_order_id=None,
                instrument=fill.instrument,
                quantity=fill.quantity,
                price=fill.price,
                fee_amount=fill.fee_amount,
                fee_currency=fill.fee_currency,
                trade_time=fill.trade_time,
            )
            with self.assertRaisesRegex(AccountingConflict, "not independently evidenced"):
                build_unexpected_provider_fill_transaction(
                    store=store,
                    checkpoint_event_id=current,
                    book=book,
                    provider_fill=missing_side,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                )
            self.assertEqual(book.transactions, ())

    def test_unexpected_hedge_leg_fails_closed_and_changed_direction_conflicts(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(directory + "/journal.sqlite3")
            checkpoint_event_id = self._record_unexpected_checkpoint(store)
            book = DurableProviderEconomicBook(
                store,
                provider_id="PROVIDER-A",
                account_id="acct-1",
                environment="PAPER",
            )
            with self.assertRaisesRegex(AccountingConflict, "leg-aware"):
                book_unexpected_provider_fill(
                    store=store,
                    checkpoint_event_id=checkpoint_event_id,
                    book=book,
                    provider_fill=self._unexpected_fill(
                        side="BUY",
                        position_side="LONG",
                    ),
                    expected_instrument="ABC",
                    settlement_currency="USD",
                )
            self.assertEqual(book.transactions, ())

            buy = self._unexpected_fill(side="BUY")
            self.assertTrue(
                book_unexpected_provider_fill(
                    store=store,
                    checkpoint_event_id=checkpoint_event_id,
                    book=book,
                    provider_fill=buy,
                    expected_instrument="ABC",
                    settlement_currency="USD",
                )
            )
            before = book.audit_digest()
            with self.assertRaises(AccountingConflict):
                book_unexpected_provider_fill(
                    store=store,
                    checkpoint_event_id=checkpoint_event_id,
                    book=book,
                    provider_fill=self._unexpected_fill(side="SELL"),
                    expected_instrument="ABC",
                    settlement_currency="USD",
                )
            self.assertEqual(book.audit_digest(), before)
            self.assertEqual(len(book.transactions), 1)





if __name__ == "__main__":
    unittest.main()
