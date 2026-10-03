from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts.store import ArtifactStore

from mvp.autotrade_mvp.durable_settlement import (
    DurableSettlementBook,
    _legacy_runtime_only_scope_id,
    _scope_payload,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.settlement import (
    SettlementAccountScope,
    SettlementConflict,
    SettlementRuleBinding,
)


class DurableSettlementProviderDomainCurrentHeadTests(unittest.TestCase):
    def test_bybit_testnet_and_demo_are_distinct_settlement_authorities(self):
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
        self.assertEqual(testnet.provider_environment, "TESTNET")
        self.assertEqual(demo.provider_environment, "DEMO")
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

    def test_rule_digest_binds_narrow_provider_domain(self):
        common = dict(
            rule_id="equity-t1",
            rule_version="v1",
            instrument_version="ABC:1",
            settlement_currency="USD",
            effective_from=date(2026, 1, 1),
            effective_to=None,
            evidence_refs=("artifact:11111111-1111-4111-8111-111111111111@sha256:" + "a" * 64,),
        )
        testnet = SettlementRuleBinding(
            scope=SettlementAccountScope(
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="TESTNET",
            ),
            **common,
        )
        demo = SettlementRuleBinding(
            scope=SettlementAccountScope(
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="DEMO",
            ),
            **common,
        )
        self.assertNotEqual(testnet.digest, demo.digest)

    def test_legacy_bybit_paper_history_blocks_domain_split(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.sqlite3")
            artifacts = ArtifactStore(root / "settlement-evidence")
            scope = SettlementAccountScope(
                provider_id="BYBIT",
                account_id="acct-legacy",
                environment="PAPER",
                provider_environment="TESTNET",
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
                    "committed_at": "2026-10-03T02:00:00Z",
                }
            )
            for provider_environment in ("TESTNET", "DEMO"):
                with self.subTest(provider_environment=provider_environment):
                    with self.assertRaisesRegex(
                        SettlementConflict,
                        "legacy BYBIT/PAPER settlement history",
                    ):
                        DurableSettlementBook(
                            store,
                            provider_id="BYBIT",
                            account_id="acct-legacy",
                            environment="PAPER",
                            provider_environment=provider_environment,
                            evidence_artifact_store=artifacts,
                        )

    def test_bybit_paper_scope_requires_explicit_valid_provider_environment(self):
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
