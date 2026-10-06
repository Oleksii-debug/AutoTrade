from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.ablation_provider_fee_expense_evidence import (
    ResolvedAblationProviderFeeExpenseEvidence,
    resolve_ablation_provider_fee_expense_evidence,
    reverify_ablation_provider_fee_expense_evidence,
)
from mvp.autotrade_mvp.accounting import (
    AccountingConflict,
    book_equity_fill,
    reverse_transaction,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook


class AblationProviderFeeExpenseEvidenceTests(unittest.TestCase):
    def _owner(self, root: Path) -> DurableProviderEconomicBook:
        return DurableProviderEconomicBook(
            JournalStore(root / "journal.sqlite3"),
            provider_id="ALPACA",
            account_id="fee-account",
            environment="PAPER",
        )

    def _fill(
        self,
        transaction_id: str,
        *,
        fee: str = "2",
        fee_currency: str = "USD",
        price: str = "100",
        observed_at: str = "2026-09-24T18:01:00Z",
        corrects_transaction_id: str | None = None,
    ):
        return book_equity_fill(
            transaction_id=transaction_id,
            cause_event_id=f"cause-{transaction_id}",
            instrument="AAPL",
            settlement_currency="USD",
            side="BUY",
            quantity="1",
            price=price,
            fee=fee,
            fee_currency=fee_currency,
            economic_effective_at="2026-09-24T18:00:00Z",
            economic_order_key="AAPL-fill-1",
            observed_at=observed_at,
            corrects_transaction_id=corrects_transaction_id,
        )

    def test_exact_provider_fee_is_numeric_but_not_self_classified_as_commission(self):
        with TemporaryDirectory() as directory:
            owner = self._owner(Path(directory))
            owner.append(self._fill("fill-1", fee="2.5"))
            cut = owner.resolve_historical_cut(1)

            evidence = resolve_ablation_provider_fee_expense_evidence(
                owner,
                cut,
                expected_visibility_journal_sequence=cut.visibility_journal_sequence,
            )

            self.assertIsInstance(evidence, ResolvedAblationProviderFeeExpenseEvidence)
            self.assertEqual(evidence.provider_environment, cut.provider_environment)
            self.assertEqual(evidence.fee_expense_by_unit, (("USD", evidence.fee_expense_by_unit[0][1]),))
            self.assertEqual(str(evidence.fee_expense_by_unit[0][1]), "2.5")
            self.assertEqual(len(evidence.contributing_transactions), 1)
            self.assertFalse(evidence.terminal_cost_composite)
            self.assertFalse(evidence.commission_classified)
            self.assertEqual(
                evidence.classification_blocker,
                "registered_commission_classification_unavailable",
            )
            evidence.verify_integrity()

    def test_bybit_fee_evidence_binds_exact_provider_environment(self):
        with TemporaryDirectory() as directory:
            owner = DurableProviderEconomicBook(
                JournalStore(Path(directory) / "journal.sqlite3"),
                provider_id="BYBIT",
                account_id="fee-account",
                environment="PAPER",
                provider_environment="TESTNET",
            )
            owner.append(self._fill("fill-bybit", fee="1.25"))
            cut = owner.resolve_historical_cut(1)

            evidence = resolve_ablation_provider_fee_expense_evidence(
                owner,
                cut,
                expected_visibility_journal_sequence=cut.visibility_journal_sequence,
            )

            self.assertEqual(cut.provider_environment, "TESTNET")
            self.assertEqual(evidence.provider_environment, "TESTNET")
            self.assertEqual(str(evidence.fee_expense_by_unit[0][1]), "1.25")
            evidence.verify_integrity()

    def test_multi_currency_fee_expense_remains_separate_without_fx(self):
        with TemporaryDirectory() as directory:
            owner = self._owner(Path(directory))
            owner.append(self._fill("fill-usd", fee="2", fee_currency="USD"))
            owner.append(
                self._fill(
                    "fill-eur",
                    fee="3",
                    fee_currency="EUR",
                    observed_at="2026-09-24T18:02:00Z",
                )
            )
            cut = owner.resolve_historical_cut(2)

            evidence = resolve_ablation_provider_fee_expense_evidence(
                owner,
                cut,
                expected_visibility_journal_sequence=cut.visibility_journal_sequence,
            )

            self.assertEqual(
                tuple((unit, str(amount)) for unit, amount in evidence.fee_expense_by_unit),
                (("EUR", "3"), ("USD", "2")),
            )
            self.assertEqual(len(evidence.contributing_transactions), 2)

    def test_zero_fee_does_not_invent_a_fee_unit_or_contributor(self):
        with TemporaryDirectory() as directory:
            owner = self._owner(Path(directory))
            owner.append(self._fill("fill-zero", fee="0", fee_currency="USD"))
            cut = owner.resolve_historical_cut(1)

            evidence = resolve_ablation_provider_fee_expense_evidence(
                owner,
                cut,
                expected_visibility_journal_sequence=cut.visibility_journal_sequence,
            )

            self.assertEqual(evidence.fee_expense_by_unit, ())
            self.assertEqual(evidence.contributing_transactions, ())

    def test_later_append_cannot_rewrite_frozen_fee_cut(self):
        with TemporaryDirectory() as directory:
            owner = self._owner(Path(directory))
            owner.append(self._fill("fill-1", fee="2"))
            first_cut = owner.resolve_historical_cut(1)
            first = resolve_ablation_provider_fee_expense_evidence(
                owner,
                first_cut,
                expected_visibility_journal_sequence=first_cut.visibility_journal_sequence,
            )

            owner.append(
                self._fill(
                    "fill-2",
                    fee="9",
                    observed_at="2026-09-24T18:02:00Z",
                )
            )
            replayed = reverify_ablation_provider_fee_expense_evidence(
                owner,
                first_cut,
                first,
                expected_visibility_journal_sequence=first_cut.visibility_journal_sequence,
            )

            self.assertEqual(replayed, first)
            self.assertEqual(str(replayed.fee_expense_by_unit[0][1]), "2")

    def test_pre_cut_correction_is_net_economic_truth(self):
        with TemporaryDirectory() as directory:
            owner = self._owner(Path(directory))
            original = self._fill("fill-original", fee="5")
            owner.append(original)
            reversal = reverse_transaction(
                original,
                transaction_id="fill-original-reversal",
                cause_event_id="cause-fill-original-reversal",
                observed_at="2026-09-24T18:03:00Z",
            )
            owner.append(reversal)
            replacement = self._fill(
                "fill-replacement",
                fee="1.5",
                price="101",
                observed_at="2026-09-24T18:03:00Z",
                corrects_transaction_id="fill-original",
            )
            owner.append(replacement)
            cut = owner.resolve_historical_cut(3)

            evidence = resolve_ablation_provider_fee_expense_evidence(
                owner,
                cut,
                expected_visibility_journal_sequence=cut.visibility_journal_sequence,
            )

            self.assertEqual(
                tuple((unit, str(amount)) for unit, amount in evidence.fee_expense_by_unit),
                (("USD", "1.5"),),
            )
            self.assertEqual(len(evidence.contributing_transactions), 3)

    def test_evidence_field_tamper_fails_before_owner_replay(self):
        with TemporaryDirectory() as directory:
            owner = self._owner(Path(directory))
            owner.append(self._fill("fill-1", fee="2"))
            cut = owner.resolve_historical_cut(1)
            evidence = resolve_ablation_provider_fee_expense_evidence(
                owner,
                cut,
                expected_visibility_journal_sequence=cut.visibility_journal_sequence,
            )
            object.__setattr__(
                evidence,
                "provider_economic_book_digest",
                "sha256:" + "f" * 64,
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "evidence digest does not match canonical material",
            ):
                reverify_ablation_provider_fee_expense_evidence(
                    owner,
                    cut,
                    evidence,
                    expected_visibility_journal_sequence=cut.visibility_journal_sequence,
                )

    def test_instance_shadowed_historical_reader_cannot_retarget_resolution(self):
        with TemporaryDirectory() as directory:
            owner = self._owner(Path(directory))
            owner.append(self._fill("fill-1", fee="2"))
            cut = owner.resolve_historical_cut(1)
            object.__setattr__(
                owner,
                "read_historical_cut",
                lambda *args, **kwargs: None,
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "shadows canonical historical-read executable",
            ):
                resolve_ablation_provider_fee_expense_evidence(
                    owner,
                    cut,
                    expected_visibility_journal_sequence=cut.visibility_journal_sequence,
                )


if __name__ == "__main__":
    unittest.main()
