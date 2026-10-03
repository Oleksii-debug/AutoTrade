from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.accounting import AccountingConflict, book_external_cash_flow
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook


class ProviderEnvironmentEconomicBookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = JournalStore(self.directory.name + "/journal.sqlite3")

    def test_bybit_economic_book_requires_exact_provider_environment(self):
        with self.assertRaisesRegex(
            AccountingConflict,
            "requires an exact provider_environment",
        ):
            DurableProviderEconomicBook(
                self.store,
                provider_id="BYBIT",
                account_id="paper-1",
                environment="PAPER",
            )

    def test_testnet_and_demo_have_disjoint_durable_book_identity(self):
        testnet = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        demo = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
            provider_environment="DEMO",
        )
        self.assertNotEqual(testnet.book_id, demo.book_id)
        self.assertEqual(testnet.provider_environment, "TESTNET")
        self.assertEqual(demo.provider_environment, "DEMO")

    def test_provider_environment_is_durable_and_replay_scoped(self):
        testnet = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        transaction = book_external_cash_flow(
            transaction_id="deposit-1",
            cause_event_id="provider-cash-1",
            currency="USD",
            amount=Decimal("25"),
        )
        self.assertTrue(testnet.append(transaction))
        events = self.store.load_events("economic_book", testnet.book_id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["schema_version"], "1.1.0")
        self.assertEqual(events[0]["payload"]["provider_environment"], "TESTNET")

        restarted = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        demo = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
            provider_environment="DEMO",
        )
        self.assertEqual(restarted.cash("USD"), Decimal("25"))
        self.assertEqual(demo.cash("USD"), Decimal("0"))

    def test_narrow_domain_refuses_ambiguous_legacy_financial_book(self):
        legacy = DurableProviderEconomicBook(
            self.store,
            provider_id="BINANCE",
            account_id="paper-1",
            environment="PAPER",
        )
        legacy.append(
            book_external_cash_flow(
                transaction_id="deposit-legacy",
                cause_event_id="provider-cash-legacy",
                currency="USD",
                amount=Decimal("10"),
            )
        )
        with self.assertRaisesRegex(
            AccountingConflict,
            "ambiguous legacy provider economic book requires explicit migration",
        ):
            DurableProviderEconomicBook(
                self.store,
                provider_id="BINANCE",
                account_id="paper-1",
                environment="PAPER",
                provider_environment="TESTNET",
            )


if __name__ == "__main__":
    unittest.main()
