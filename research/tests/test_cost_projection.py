from __future__ import annotations

from decimal import Decimal
import unittest

from mvp.autotrade_mvp.accounting import (
    JournalTransaction,
    ScopedEconomicBook,
    posting,
    reverse_transaction,
    validate_transaction,
)
from mvp.autotrade_mvp.provider_activity_accounting import EconomicBookCut
from research.autotrade_research.evaluation.cost_projection import (
    REQUIRED_ABLATION_COST_COMPONENTS,
    CanonicalJournalCostCut,
    CostProjectionError,
    project_economic_cut_costs,
    project_provider_journal_costs,
)


def transaction(
    *,
    transaction_id: str,
    cause_event_id: str,
    expense_account: str,
    unit: str,
    amount: str,
) -> JournalTransaction:
    value = Decimal(amount)
    result = JournalTransaction(
        transaction_id=transaction_id,
        cause_event_id=cause_event_id,
        postings=(
            posting(f"CASH:{unit}", unit, -value),
            posting(expense_account, unit, value),
        ),
    )
    validate_transaction(result)
    return result


def cut(*transactions: JournalTransaction, digest: str | None = None) -> EconomicBookCut:
    scoped = ScopedEconomicBook(
        environment="PAPER",
        account_id="acct-1",
        transactions=transactions,
    )
    return EconomicBookCut(
        provider_id="BYBIT",
        account_id="acct-1",
        environment="PAPER",
        transactions=tuple(transactions),
        book_digest=scoped.audit_digest() if digest is None else digest,
        aggregate_version=len(transactions),
    )


class JournalCostProjectionTests(unittest.TestCase):
    def test_generic_fee_funding_and_financing_accounts_remain_unresolved(self):
        fee = transaction(
            transaction_id="fee-1",
            cause_event_id="fill-1",
            expense_account="FEE_EXPENSE:USD",
            unit="USD",
            amount="1.25",
        )
        funding = transaction(
            transaction_id="funding-1",
            cause_event_id="funding-event-1",
            expense_account="FUNDING_PNL:USD",
            unit="USD",
            amount="2.5",
        )
        financing = transaction(
            transaction_id="financing-1",
            cause_event_id="financing-event-1",
            expense_account="FINANCING_EXPENSE:USD",
            unit="USD",
            amount="3",
        )

        result = project_economic_cut_costs(cut(fee, funding, financing))

        self.assertEqual(result.components, ())
        self.assertEqual(
            result.ambiguous_accounts,
            (
                "FEE_EXPENSE:USD",
                "FINANCING_EXPENSE:USD",
                "FUNDING_PNL:USD",
            ),
        )
        for component in ("commission", "funding", "financing", "borrow"):
            self.assertIn(component, result.missing_components)
        self.assertFalse(result.complete)
        self.assertTrue(result.evidence_digest.startswith("sha256:"))

    def test_fee_expense_cannot_self_classify_as_commission(self):
        fee = transaction(
            transaction_id="fee-1",
            cause_event_id="fill-1",
            expense_account="FEE_EXPENSE:USD",
            unit="USD",
            amount="1",
        )

        result = project_economic_cut_costs(cut(fee))

        self.assertEqual(result.components, ())
        self.assertEqual(result.ambiguous_accounts, ("FEE_EXPENSE:USD",))
        self.assertIn("commission", result.missing_components)

    def test_signed_funding_pnl_cannot_self_project_as_cost(self):
        debit = transaction(
            transaction_id="funding-debit",
            cause_event_id="funding-debit-event",
            expense_account="FUNDING_PNL:USD",
            unit="USD",
            amount="2",
        )
        credit = transaction(
            transaction_id="funding-credit",
            cause_event_id="funding-credit-event",
            expense_account="FUNDING_PNL:USD",
            unit="USD",
            amount="-5",
        )

        debit_result = project_economic_cut_costs(cut(debit))
        credit_result = project_economic_cut_costs(cut(credit))

        for result in (debit_result, credit_result):
            self.assertEqual(result.components, ())
            self.assertEqual(result.ambiguous_accounts, ("FUNDING_PNL:USD",))
            self.assertIn("funding", result.missing_components)

    def test_missing_components_are_not_silently_projected_as_zero(self):
        result = project_economic_cut_costs(cut())

        self.assertEqual(result.components, ())
        self.assertEqual(result.missing_components, REQUIRED_ABLATION_COST_COMPONENTS)
        self.assertFalse(result.complete)

    def test_reversal_does_not_upgrade_unclassified_fee_to_zero_commission(self):
        fee = transaction(
            transaction_id="fee-original",
            cause_event_id="fill-original",
            expense_account="FEE_EXPENSE:USD",
            unit="USD",
            amount="1",
        )
        reversal = reverse_transaction(
            fee,
            transaction_id="fee-reversal",
            cause_event_id="fill-bust",
        )

        result = project_economic_cut_costs(cut(fee, reversal))

        self.assertEqual(result.components, ())
        self.assertEqual(result.ambiguous_accounts, ("FEE_EXPENSE:USD",))
        self.assertIn("commission", result.missing_components)
        self.assertFalse(result.complete)

    def test_native_units_do_not_create_implicit_commission_classification(self):
        usd = transaction(
            transaction_id="fee-usd",
            cause_event_id="fill-usd",
            expense_account="FEE_EXPENSE:USD",
            unit="USD",
            amount="1",
        )
        usdt = transaction(
            transaction_id="fee-usdt",
            cause_event_id="fill-usdt",
            expense_account="FEE_EXPENSE:USDT",
            unit="USDT",
            amount="2",
        )

        result = project_economic_cut_costs(cut(usd, usdt))

        self.assertEqual(result.components, ())
        self.assertEqual(
            result.ambiguous_accounts,
            ("FEE_EXPENSE:USD", "FEE_EXPENSE:USDT"),
        )
        self.assertIn("commission", result.missing_components)
        self.assertFalse(result.complete)

    def test_cut_digest_is_recomputed_instead_of_trusted(self):
        fee = transaction(
            transaction_id="fee-1",
            cause_event_id="fill-1",
            expense_account="FEE_EXPENSE:USD",
            unit="USD",
            amount="1",
        )
        with self.assertRaisesRegex(CostProjectionError, "digest does not match"):
            project_economic_cut_costs(
                cut(fee, digest="sha256:" + ("0" * 64))
            )

    def test_financing_account_cannot_be_relabelled_as_funding_or_borrow(self):
        financing = transaction(
            transaction_id="financing-1",
            cause_event_id="provider-financing-1",
            expense_account="FINANCING_EXPENSE:USD",
            unit="USD",
            amount="4",
        )

        result = project_economic_cut_costs(cut(financing))

        self.assertEqual(result.components, ())
        self.assertEqual(result.ambiguous_accounts, ("FINANCING_EXPENSE:USD",))
        self.assertIn("financing", result.missing_components)
        self.assertIn("funding", result.missing_components)
        self.assertIn("borrow", result.missing_components)

    def test_descriptive_cut_rejects_subclasses(self):
        class ForgedCut(EconomicBookCut):
            pass

        canonical = cut()
        forged = ForgedCut(
            provider_id=canonical.provider_id,
            account_id=canonical.account_id,
            environment=canonical.environment,
            transactions=canonical.transactions,
            book_digest=canonical.book_digest,
            aggregate_version=canonical.aggregate_version,
        )
        with self.assertRaises(TypeError):
            project_economic_cut_costs(forged)

    def test_authority_projection_rejects_non_durable_book(self):
        scoped = ScopedEconomicBook(
            environment="PAPER",
            account_id="acct-1",
        )
        with self.assertRaises(TypeError):
            project_provider_journal_costs(scoped)  # type: ignore[arg-type]

    def test_forged_complete_dataclass_cannot_hide_missing_coverage(self):
        canonical = project_economic_cut_costs(cut())
        with self.assertRaisesRegex(CostProjectionError, "missing component coverage"):
            CanonicalJournalCostCut(
                provider_id=canonical.provider_id,
                account_id=canonical.account_id,
                environment=canonical.environment,
                economic_book_digest=canonical.economic_book_digest,
                aggregate_version=canonical.aggregate_version,
                components=canonical.components,
                ambiguous_accounts=canonical.ambiguous_accounts,
                missing_components=(),
                evidence_digest=canonical.evidence_digest,
            )


if __name__ == "__main__":
    unittest.main()
