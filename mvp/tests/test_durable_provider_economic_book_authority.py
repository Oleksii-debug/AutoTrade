from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.accounting import (
    AccountingConflict,
    EconomicBook,
    book_external_cash_flow,
)
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
    commit_economic_batch_with_reservation_consumption,
)


def economic_book(store: JournalStore) -> DurableProviderEconomicBook:
    return DurableProviderEconomicBook(
        store,
        provider_id="PROVIDER-A",
        account_id="acct-authority",
        environment="PAPER",
    )


def reservation_book(store: JournalStore) -> DurableReservationBook:
    return DurableReservationBook(
        store,
        environment="PAPER",
        account_id="acct-authority",
    )


def cash_transaction(*, transaction_id: str = "cash-1", amount: str = "10"):
    return book_external_cash_flow(
        transaction_id=transaction_id,
        cause_event_id=f"cause-{transaction_id}",
        currency="USD",
        amount=amount,
    )


class DurableProviderEconomicBookAuthorityTests(unittest.TestCase):
    def test_journal_store_subclass_is_rejected_before_replay(self):
        with TemporaryDirectory() as directory:
            class HostileStore(JournalStore):
                pass

            store = HostileStore(Path(directory) / "journal.sqlite3")
            with self.assertRaisesRegex(TypeError, "exact JournalStore"):
                economic_book(store)

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
            with self.assertRaisesRegex(AccountingConflict, "JournalStore changed"):
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

    def test_projection_substitution_fails_before_financial_use(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book = economic_book(store)
            vars(book)["_book"] = EconomicBook((cash_transaction(transaction_id="forged"),))

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

    def test_normal_authority_attribute_mutation_is_rejected(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book = economic_book(store)
            with self.assertRaisesRegex(AccountingConflict, "authority state is immutable"):
                book.provider_id = "OTHER-PROVIDER"
            with self.assertRaisesRegex(AccountingConflict, "authority state is immutable"):
                book._book = EconomicBook()

    def test_canonical_append_and_restart_preserve_projection(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            book = economic_book(store)
            transaction = cash_transaction()

            self.assertTrue(book.append(transaction))
            first_digest = book.audit_digest()
            self.assertFalse(book.append(transaction))
            self.assertEqual(book.audit_digest(), first_digest)

            reopened_store = JournalStore(path)
            reopened = economic_book(reopened_store)
            self.assertEqual(reopened.transactions, (transaction,))
            self.assertEqual(reopened.audit_digest(), first_digest)

    def test_same_backing_generation_independent_handles_compose_atomically(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            economic_store = JournalStore(path)
            reservation_store = JournalStore(path)
            economics = economic_book(economic_store)
            reservations = reservation_book(reservation_store)
            reservations.reserve(
                command_id="reserve-command",
                idempotency_key="reserve-idempotency",
                reservation_id="reservation-1",
                intent_id="intent-1",
                requirements={"CASH:USD": "20"},
                available={"CASH:USD": "100"},
            )
            transaction = cash_transaction()

            self.assertTrue(
                commit_economic_batch_with_reservation_consumption(
                    economics,
                    reservations,
                    command_id="fill-command",
                    idempotency_key="fill-idempotency",
                    reservation_id="reservation-1",
                    usage={"CASH:USD": "10"},
                    transactions=(transaction,),
                    committed_at="2026-10-03T15:50:00Z",
                )
            )
            self.assertEqual(economics.transactions, (transaction,))
            self.assertEqual(
                str(reservations.get("reservation-1").consumed["CASH:USD"]),
                "10",
            )


if __name__ == "__main__":
    unittest.main()
