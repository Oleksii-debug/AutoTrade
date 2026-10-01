from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.accounting import (
    AccountingConflict,
    EconomicBook,
    book_external_cash_flow,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
)


def economic_book(store: JournalStore) -> DurableProviderEconomicBook:
    return DurableProviderEconomicBook(
        store,
        provider_id="PROVIDER-A",
        account_id="acct-authority",
        environment="PAPER",
    )


def cash_transaction(*, transaction_id: str = "cash-1", amount: str = "10"):
    return book_external_cash_flow(
        transaction_id=transaction_id,
        cause_event_id=f"cause-{transaction_id}",
        currency="USD",
        amount=amount,
    )


class DurableProviderEconomicBookAuthorityTests(unittest.TestCase):
    def test_exact_base_events_shadow_fails_before_callback(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book = economic_book(store)
            called = []

            def hostile(*_args, **_kwargs):
                called.append(True)
                raise AssertionError("shadow callback executed")

            vars(book)["_events"] = hostile
            with self.assertRaisesRegex(AccountingConflict, "state is shadowed"):
                book.refresh()
            self.assertEqual(called, [])

    def test_exact_base_prepare_shadow_fails_before_callback(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book = economic_book(store)
            called = []

            def hostile(*_args, **_kwargs):
                called.append(True)
                raise AssertionError("shadow callback executed")

            vars(book)["prepare_batch_mutation"] = hostile
            with self.assertRaisesRegex(AccountingConflict, "state is shadowed"):
                book.prepare_batch_mutation((cash_transaction(),))
            self.assertEqual(called, [])
            self.assertEqual(
                JournalStore.load_events(store, "economic_book", vars(book)["book_id"]),
                [],
            )

    def test_selected_journal_store_method_shadow_fails_before_callback(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book = economic_book(store)
            called = []

            def hostile(*_args, **_kwargs):
                called.append(True)
                raise AssertionError("JournalStore shadow callback executed")

            vars(store)["load_events"] = hostile
            with self.assertRaisesRegex(TypeError, "instance state is shadowed"):
                book.refresh()
            self.assertEqual(called, [])

    def test_selected_store_replacement_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.sqlite3")
            book = economic_book(store)
            replacement = JournalStore(root / "other.sqlite3")

            vars(book)["store"] = replacement
            with self.assertRaisesRegex(
                AccountingConflict,
                "JournalStore changed",
            ):
                book.refresh()
            self.assertEqual(
                JournalStore.load_events(store, "economic_book", vars(book)["book_id"]),
                [],
            )
            self.assertEqual(
                JournalStore.load_events(
                    replacement,
                    "economic_book",
                    vars(book)["book_id"],
                ),
                [],
            )

    def test_scope_mutation_fails_before_financial_use(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book = economic_book(store)

            vars(book)["provider_id"] = "OTHER-PROVIDER"
            with self.assertRaisesRegex(
                AccountingConflict,
                "financial scope changed",
            ):
                book.prepare_batch_mutation((cash_transaction(),))
            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "economic_book",
                    vars(book)["book_id"],
                ),
                [],
            )

    def test_projection_substitution_fails_before_read_or_append(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book = economic_book(store)
            forged = EconomicBook((cash_transaction(transaction_id="forged"),))

            vars(book)["_book"] = forged
            with self.assertRaisesRegex(
                AccountingConflict,
                "projection changed outside canonical reload",
            ):
                _ = book.transactions
            with self.assertRaisesRegex(
                AccountingConflict,
                "projection changed outside canonical reload",
            ):
                book.append(cash_transaction(transaction_id="real"))

    def test_normal_attribute_mutation_is_rejected_after_seal(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book = economic_book(store)

            with self.assertRaisesRegex(
                AccountingConflict,
                "authority state is immutable",
            ):
                book.provider_id = "OTHER-PROVIDER"
            with self.assertRaisesRegex(
                AccountingConflict,
                "authority state is immutable",
            ):
                book._book = EconomicBook()

    def test_canonical_append_and_restart_preserve_exact_projection(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            book = economic_book(store)
            transaction = cash_transaction()

            self.assertTrue(book.append(transaction))
            self.assertEqual(book.cash("USD"), transaction.postings[0].signed_amount)
            first_digest = book.audit_digest()
            self.assertFalse(book.append(transaction))
            self.assertEqual(book.audit_digest(), first_digest)

            reopened_store = JournalStore(path)
            reopened = economic_book(reopened_store)
            self.assertEqual(reopened.transactions, (transaction,))
            self.assertEqual(reopened.audit_digest(), first_digest)


if __name__ == "__main__":
    unittest.main()
