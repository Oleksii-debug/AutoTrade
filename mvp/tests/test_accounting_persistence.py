from decimal import Decimal
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.accounting import AccountingConflict, transaction_digest
from mvp.autotrade_mvp.accounting_persistence import (
    commit_provider_fill,
    commit_provider_fill_correction,
    load_durable_scoped_book,
)
from mvp.autotrade_mvp.fill_accounting import ProjectedFillEvidence
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reconciliation import ProviderFillEvidence
from mvp.tests.test_fill_accounting import matched_fill


def corrected_fill():
    projected = ProjectedFillEvidence.create(
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
    provider = ProviderFillEvidence.create(
        provider_id="PROVIDER-A",
        account_id="acct-1",
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
    return projected, provider


class DurableAccountingTests(unittest.TestCase):
    def _store(self, directory):
        return JournalStore(Path(directory) / "journal.sqlite3")

    def _commit_original(self, store):
        projected, provider = matched_fill()
        return commit_provider_fill(
            store,
            environment="PAPER",
            account_id="acct-1",
            provider_id="provider-a",
            projected_fill=projected,
            provider_fill=provider,
            expected_instrument="ABC",
            settlement_currency="USD",
            observed_at="2026-01-01T00:00:01Z",
        )

    def _commit_correction(self, store):
        original_projected, original_provider = matched_fill()
        corrected_projected, corrected_provider = corrected_fill()
        return commit_provider_fill_correction(
            store,
            environment="PAPER",
            account_id="acct-1",
            provider_id="provider-a",
            original_projected_fill=original_projected,
            original_provider_fill=original_provider,
            corrected_projected_fill=corrected_projected,
            corrected_provider_fill=corrected_provider,
            expected_instrument="ABC",
            settlement_currency="USD",
            correction_observed_at="2026-01-01T00:00:02Z",
        )

    def test_fill_and_correction_reopen_to_identical_financial_state(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            initial = self._commit_original(store)
            self.assertTrue(initial.inserted)
            self.assertEqual(initial.book.position("ABC"), Decimal("2"))
            self.assertEqual(initial.book.cash("USD"), Decimal("-201"))

            corrected = self._commit_correction(store)
            self.assertTrue(corrected.inserted)
            self.assertEqual(len(corrected.transactions), 2)
            self.assertEqual(len(corrected.book.transactions), 3)
            self.assertEqual(corrected.book.position("ABC"), Decimal("2"))
            self.assertEqual(corrected.book.cash("USD"), Decimal("-203"))
            self.assertEqual(corrected.book.fee_expense("USD"), Decimal("1"))

            reopened_store = self._store(directory)
            reopened = load_durable_scoped_book(
                reopened_store,
                environment="PAPER",
                account_id="acct-1",
            )
            self.assertEqual(
                reopened.transactions,
                corrected.book.transactions,
            )
            self.assertEqual(
                reopened.audit_digest(),
                corrected.book.audit_digest(),
            )

    def test_post_commit_ack_loss_retries_without_second_posting(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            first = self._commit_original(store)
            self.assertTrue(first.inserted)

            # Simulate losing the caller acknowledgement by discarding the first
            # result, reopening the process, and issuing the exact same command.
            retry = self._commit_original(self._store(directory))
            self.assertFalse(retry.inserted)
            self.assertEqual(retry.command_id, first.command_id)
            self.assertEqual(retry.command_digest, first.command_digest)
            self.assertEqual(len(retry.book.transactions), 1)

            correction = self._commit_correction(self._store(directory))
            self.assertTrue(correction.inserted)
            correction_retry = self._commit_correction(self._store(directory))
            self.assertFalse(correction_retry.inserted)
            self.assertEqual(
                correction_retry.command_id,
                correction.command_id,
            )
            self.assertEqual(len(correction_retry.book.transactions), 3)
            self.assertEqual(
                correction_retry.book.audit_digest(),
                correction.book.audit_digest(),
            )

    def test_failure_before_atomic_commit_leaves_neither_correction_fact(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            initial = self._commit_original(store)
            self.assertEqual(len(initial.book.transactions), 1)

            with patch.object(
                store,
                "commit_command",
                side_effect=RuntimeError("injected pre-commit failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected pre-commit failure",
                ):
                    self._commit_correction(store)

            reopened = load_durable_scoped_book(
                self._store(directory),
                environment="PAPER",
                account_id="acct-1",
            )
            self.assertEqual(len(reopened.transactions), 1)
            self.assertEqual(
                reopened.transactions,
                initial.book.transactions,
            )
            self.assertEqual(reopened.cash("USD"), Decimal("-201"))

    def test_partial_durable_correction_is_corruption_not_auto_completed(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            self._commit_original(store)
            correction = self._commit_correction(store)
            replacement_event_id = (
                "accounting-event:"
                + transaction_digest(correction.transactions[1]).removeprefix("sha256:")
            )

            # Direct storage corruption models a torn/externally damaged durable
            # state. The normal JournalStore transaction cannot produce this.
            connection = sqlite3.connect(store.path)
            try:
                connection.execute(
                    "DELETE FROM events WHERE event_id = ?",
                    (replacement_event_id,),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                AccountingConflict,
                "only partially committed",
            ):
                load_durable_scoped_book(
                    store,
                    environment="PAPER",
                    account_id="acct-1",
                )

            # The surviving reversal must never be silently accepted as final
            # corrected economics.
            with self.assertRaises(AccountingConflict):
                self._commit_correction(store)

    def test_durable_scope_is_account_and_environment_specific(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            self._commit_original(store)
            other = load_durable_scoped_book(
                store,
                environment="PAPER",
                account_id="acct-2",
            )
            self.assertEqual(other.transactions, ())
            self.assertEqual(other.position("ABC"), Decimal("0"))

            replay = load_durable_scoped_book(
                store,
                environment="REPLAY",
                account_id="acct-1",
            )
            self.assertEqual(replay.transactions, ())
            self.assertEqual(replay.cash("USD"), Decimal("0"))


if __name__ == "__main__":
    unittest.main()
