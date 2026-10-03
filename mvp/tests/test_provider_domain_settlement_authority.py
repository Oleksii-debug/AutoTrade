from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp.durable_settlement import (
    DurableSettlementBook,
    _legacy_runtime_only_scope_id,
    _scope_payload,
    settlement_completion_evidence_metadata,
    settlement_completion_evidence_receipt,
    settlement_rule_evidence_metadata,
    settlement_rule_evidence_receipt,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.settlement import (
    SettlementAccountScope,
    SettlementConflict,
    SettlementEvidence,
    SettlementObligation,
    SettlementRuleBinding,
)


class ProviderDomainSettlementAuthorityTests(unittest.TestCase):
    def _scope(self, provider_environment: str) -> SettlementAccountScope:
        return SettlementAccountScope(
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
            provider_environment=provider_environment,
        )

    def _rule(self, provider_environment: str) -> SettlementRuleBinding:
        return SettlementRuleBinding(
            rule_id="equity-t1",
            rule_version="v1",
            scope=self._scope(provider_environment),
            instrument_version="ABC:1",
            settlement_currency="USD",
            effective_from=date(2026, 1, 1),
            effective_to=None,
            evidence_refs=(
                "artifact:11111111-1111-4111-8111-111111111111@sha256:" + "a" * 64,
            ),
        )

    def test_bybit_testnet_and_demo_are_distinct_settlement_authorities(self):
        testnet = self._scope("TESTNET")
        demo = self._scope("DEMO")
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

    def test_rule_digest_and_evidence_receipts_bind_provider_domain(self):
        testnet = self._rule("TESTNET")
        demo = self._rule("DEMO")
        self.assertNotEqual(testnet.digest, demo.digest)

        trade_date = date(2026, 9, 25)
        settlement_date = date(2026, 9, 26)
        rule_receipt = settlement_rule_evidence_receipt(
            testnet,
            trade_date=trade_date,
            expected_settlement_date=settlement_date,
        )
        rule_metadata = settlement_rule_evidence_metadata(
            testnet,
            trade_date=trade_date,
            expected_settlement_date=settlement_date,
        )
        self.assertEqual(
            rule_receipt["observation"]["provider_environment"],
            "TESTNET",
        )
        self.assertEqual(rule_metadata["provider_environment"], "TESTNET")

        obligation = SettlementObligation(
            obligation_id="obl-1",
            cause_event_id="fill-1",
            currency="USD",
            amount="100",
            trade_date=trade_date,
            settlement_date=settlement_date,
            source_transaction_id="tx-1",
            rule_binding=testnet,
        )
        evidence = SettlementEvidence(
            obligation_id="obl-1",
            evidence_ref=(
                "artifact:22222222-2222-4222-8222-222222222222@sha256:" + "b" * 64
            ),
            observed_at=datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc),
        )
        completion_receipt = settlement_completion_evidence_receipt(
            scope=testnet.scope,
            obligation=obligation,
            evidence=evidence,
        )
        completion_metadata = settlement_completion_evidence_metadata(
            scope=testnet.scope,
            obligation=obligation,
            evidence=evidence,
        )
        self.assertEqual(
            completion_receipt["observation"]["provider_environment"],
            "TESTNET",
        )
        self.assertEqual(
            completion_metadata["provider_environment"],
            "TESTNET",
        )

    def test_legacy_bybit_paper_history_blocks_silent_domain_split(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.sqlite3")
            artifacts = ArtifactStore(root / "settlement-evidence")
            scope = self._scope("TESTNET")
            legacy_scope_id = _legacy_runtime_only_scope_id(scope)
            payload = {
                "scope": {
                    "provider_id": "BYBIT",
                    "account_id": "acct-1",
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
                            account_id="acct-1",
                            environment="PAPER",
                            provider_environment=provider_environment,
                            evidence_artifact_store=artifacts,
                        )

    def test_bybit_and_unqualified_provider_domains_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "explicit provider_environment"):
            SettlementAccountScope(
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
            )
        with self.assertRaisesRegex(ValueError, "does not match runtime environment"):
            SettlementAccountScope(
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="MAINNET",
            )
        with self.assertRaisesRegex(ValueError, "must equal runtime environment"):
            SettlementAccountScope(
                provider_id="KRAKEN",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="SANDBOX",
            )


if __name__ == "__main__":
    unittest.main()
