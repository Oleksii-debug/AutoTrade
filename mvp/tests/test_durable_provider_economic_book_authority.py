from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.accounting import (
    AccountingConflict,
    EconomicBook,
    book_external_cash_flow,
)
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
    _scoped_identity,
    commit_economic_batch_with_reservation_consumption,
)


def economic_book(store: JournalStore, *, environment="PAPER") -> DurableProviderEconomicBook:
    return DurableProviderEconomicBook(
        store,
        provider_id="PROVIDER-A",
        account_id="acct-authority",
        environment=environment,
    )


def reservation_book(store: JournalStore, *, environment="PAPER") -> DurableReservationBook:
    return DurableReservationBook(
        store,
        environment=environment,
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
    def test_sealed_scoped_authority_composes_with_durable_book(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            book = economic_book(JournalStore(path), environment="SIMULATION")

            initial_digest = book.audit_digest()
            self.assertEqual(book.transactions, ())
            self.assertEqual(str(book.balance("CASH:USD", "USD")), "0")
            self.assertEqual(str(book.cash("USD")), "0")
            self.assertEqual(str(book.position("ASSET")), "0")
            self.assertEqual(str(book.fee_expense("USD")), "0")

            transaction = cash_transaction()
            self.assertTrue(book.append(transaction))
            self.assertEqual(book.transactions, (transaction,))
            self.assertEqual(str(book.cash("USD")), "10")
            self.assertNotEqual(book.audit_digest(), initial_digest)

            reopened = economic_book(JournalStore(path), environment="SIMULATION")
            self.assertEqual(reopened.transactions, (transaction,))
            self.assertEqual(reopened.audit_digest(), book.audit_digest())

    def test_bybit_provider_environment_separates_durable_book_identity(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            testnet = DurableProviderEconomicBook(
                store,
                provider_id="BYBIT",
                account_id="acct-authority",
                environment="PAPER",
                provider_environment="TESTNET",
            )
            demo = DurableProviderEconomicBook(
                store,
                provider_id="BYBIT",
                account_id="acct-authority",
                environment="PAPER",
                provider_environment="DEMO",
            )

            self.assertEqual(testnet.provider_environment, "TESTNET")
            self.assertEqual(demo.provider_environment, "DEMO")
            self.assertNotEqual(testnet.book_id, demo.book_id)
            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "economic_book",
                    testnet.book_id,
                ),
                [],
            )
            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "economic_book",
                    demo.book_id,
                ),
                [],
            )

    def test_bybit_legacy_runtime_only_economic_history_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            legacy_book_id = _scoped_identity(
                "economic-book",
                "BYBIT",
                "acct-authority",
                "PAPER",
            )
            payload = {
                "provider_id": "BYBIT",
                "account_id": "acct-authority",
                "environment": "PAPER",
                "transaction": {"legacy": "ambiguous-provider-domain"},
            }
            store.append_event(
                {
                    "event_id": "legacy-bybit-economic-event",
                    "event_type": "EconomicTransactionBooked",
                    "aggregate_type": "economic_book",
                    "aggregate_id": legacy_book_id,
                    "aggregate_version": "1",
                    "committed_at": "2026-09-24T18:00:00Z",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                }
            )

            for provider_environment in ("TESTNET", "DEMO"):
                with self.subTest(provider_environment=provider_environment):
                    with self.assertRaisesRegex(
                        AccountingConflict,
                        "ambiguous financial history",
                    ):
                        DurableProviderEconomicBook(
                            store,
                            provider_id="BYBIT",
                            account_id="acct-authority",
                            environment="PAPER",
                            provider_environment=provider_environment,
                        )

    def test_bybit_durable_book_requires_explicit_provider_environment(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            with self.assertRaisesRegex(
                AccountingConflict,
                "requires explicit provider_environment",
            ):
                DurableProviderEconomicBook(
                    store,
                    provider_id="BYBIT",
                    account_id="acct-authority",
                    environment="PAPER",
                )

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

    def test_bybit_provider_environment_binds_book_batch_and_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            testnet = DurableProviderEconomicBook(
                store,
                provider_id="BYBIT",
                account_id="bybit-account",
                environment="PAPER",
                provider_environment="TESTNET",
            )
            demo = DurableProviderEconomicBook(
                store,
                provider_id="BYBIT",
                account_id="bybit-account",
                environment="PAPER",
                provider_environment="DEMO",
            )
            self.assertNotEqual(testnet.book_id, demo.book_id)

            transaction = cash_transaction()
            testnet_plan = testnet.prepare_batch_mutation(
                (transaction,),
                committed_at="2026-10-06T08:20:00Z",
            )
            demo_plan = demo.prepare_batch_mutation(
                (transaction,),
                committed_at="2026-10-06T08:20:00Z",
            )
            self.assertNotEqual(testnet_plan.batch_digest, demo_plan.batch_digest)
            self.assertEqual(
                testnet_plan.request["provider_environment"],
                "TESTNET",
            )
            self.assertEqual(
                demo_plan.request["provider_environment"],
                "DEMO",
            )

            self.assertTrue(
                testnet.append_batch(
                    (transaction,),
                    committed_at="2026-10-06T08:20:00Z",
                )
            )
            reopened = DurableProviderEconomicBook(
                JournalStore(path),
                provider_id="BYBIT",
                account_id="bybit-account",
                environment="PAPER",
                provider_environment="TESTNET",
            )
            self.assertEqual(reopened.transactions, (transaction,))
            self.assertEqual(demo.transactions, ())

    def test_same_backing_generation_independent_handles_compose_atomically(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            economic_store = JournalStore(path)
            reservation_store = JournalStore(path)
            economics = economic_book(economic_store, environment="SIMULATION")
            reservations = reservation_book(reservation_store, environment="SIMULATION")
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
