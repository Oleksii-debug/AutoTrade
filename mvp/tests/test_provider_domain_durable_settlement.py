import unittest

from mvp.autotrade_mvp.durable_settlement import (
    DurableSettlementBook,
    _scope_payload,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.settlement import SettlementAccountScope
from research.autotrade_research.artifacts.store import ArtifactStore


class DurableSettlementProviderDomainTests(unittest.TestCase):
    def test_bybit_testnet_and_demo_have_distinct_durable_scope_identity(self):
        testnet = SettlementAccountScope(
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        demo = SettlementAccountScope(
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
            provider_environment="DEMO",
        )

        self.assertEqual(
            _scope_payload(testnet),
            {
                "provider_id": "BYBIT",
                "account_id": "acct-1",
                "environment": "PAPER",
                "provider_environment": "TESTNET",
            },
        )
        self.assertEqual(
            _scope_payload(demo),
            {
                "provider_id": "BYBIT",
                "account_id": "acct-1",
                "environment": "PAPER",
                "provider_environment": "DEMO",
            },
        )

        store = JournalStore(":memory:")
        artifacts = ArtifactStore(".autotrade-test-provider-domain-settlement")
        testnet_book = DurableSettlementBook(
            store,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
            provider_environment="TESTNET",
            evidence_artifact_store=artifacts,
        )
        demo_book = DurableSettlementBook(
            store,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
            provider_environment="DEMO",
            evidence_artifact_store=artifacts,
        )

        self.assertNotEqual(testnet_book.scope_id, demo_book.scope_id)
        self.assertEqual(testnet_book.obligations, ())
        self.assertEqual(demo_book.obligations, ())

    def test_bybit_durable_scope_requires_exact_provider_environment(self):
        with self.assertRaises(ValueError):
            SettlementAccountScope(
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
            )
        with self.assertRaises(ValueError):
            SettlementAccountScope(
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="MAINNET",
            )


if __name__ == "__main__":
    unittest.main()
