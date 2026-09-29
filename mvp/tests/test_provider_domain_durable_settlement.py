from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.durable_settlement import (
    DurableSettlementBook,
    _legacy_runtime_only_scope_id,
    _scope_payload,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.settlement import SettlementAccountScope, SettlementConflict
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

        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.sqlite3")
            artifacts = ArtifactStore(root / "settlement-evidence")
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

    def test_legacy_bybit_paper_settlement_history_blocks_domain_split(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.sqlite3")
            artifacts = ArtifactStore(root / "settlement-evidence")
            scope = SettlementAccountScope(
                provider_id="BYBIT", account_id="acct-legacy",
                environment="PAPER", provider_environment="TESTNET",
            )
            legacy_scope_id = _legacy_runtime_only_scope_id(scope)
            payload = {
                "scope": {
                    "provider_id": "BYBIT",
                    "account_id": "acct-legacy",
                    "environment": "PAPER",
                },
                "legacy_marker": True,
            }
            store.append_event(
                {
                    "event_id": "legacy-bybit-paper-settlement-1",
                    "event_type": "SettlementObligationsRegistered",
                    "aggregate_type": "settlement_book",
                    "aggregate_id": legacy_scope_id,
                    "aggregate_version": "1",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": "2026-09-29T00:00:00Z",
                }
            )
            for provider_environment in ("TESTNET", "DEMO"):
                with self.subTest(
                    provider_environment=provider_environment
                ), self.assertRaisesRegex(
                    SettlementConflict, "legacy BYBIT/PAPER settlement history"
                ):
                    DurableSettlementBook(
                        store,
                        provider_id="BYBIT",
                        account_id="acct-legacy",
                        environment="PAPER",
                        provider_environment=provider_environment,
                        evidence_artifact_store=artifacts,
                    )

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
