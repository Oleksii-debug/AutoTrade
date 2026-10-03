from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp.durable_settlement import (
    DurableSettlementBook,
    SETTLEMENT_EVIDENCE_MEDIA_TYPE,
    settlement_rule_evidence_metadata,
    settlement_rule_evidence_receipt,
)
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.autotrade_mvp.settlement import (
    SettlementAccountScope,
    SettlementConflict,
    SettlementObligation,
    SettlementRuleBinding,
)
from research.autotrade_research.artifacts.store import ArtifactStore


class SettlementArtifactStoreUnforgeabilityTests(unittest.TestCase):
    def test_instance_shadow_cannot_participate_in_authenticated_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            journal = JournalStore(root / "journal.sqlite3")
            artifacts = ArtifactStore(root / "settlement-evidence")
            scope = SettlementAccountScope(
                provider_id="PROVIDER-A",
                account_id="acct-1",
                environment="PAPER",
            )
            rule = SettlementRuleBinding(
                rule_id="equity-cash",
                rule_version="1",
                scope=scope,
                instrument_version="ABC",
                settlement_currency="USD",
                effective_from=date(2026, 9, 1),
                effective_to=None,
                evidence_refs=("provider-doc:test-settlement-rule",),
            )
            trade_date = date(2026, 9, 25)
            settlement_date = date(2026, 9, 26)
            receipt = settlement_rule_evidence_receipt(
                rule,
                trade_date=trade_date,
                expected_settlement_date=settlement_date,
            )
            raw = canonical_json(receipt).encode("utf-8")
            artifact_id = str(
                uuid5(
                    NAMESPACE_URL,
                    "https://evidence.autotrade.local/settlement-rule/"
                    + canonical_json(receipt),
                )
            )
            manifest = artifacts.publish_bytes(
                artifact_id=artifact_id,
                data=raw,
                media_type=SETTLEMENT_EVIDENCE_MEDIA_TYPE,
                rights={"storage": True, "export": False},
                source_refs=["provider-doc:test-settlement-rule"],
                metadata=settlement_rule_evidence_metadata(
                    rule,
                    trade_date=trade_date,
                    expected_settlement_date=settlement_date,
                ),
            )
            bound_rule = replace(
                rule,
                evidence_refs=(
                    *rule.evidence_refs,
                    f"artifact:{artifact_id}@{manifest['sha256']}",
                ),
            )
            obligation = SettlementObligation(
                obligation_id="obligation-1",
                cause_event_id="fill-1",
                currency="USD",
                amount=Decimal("100"),
                trade_date=trade_date,
                settlement_date=settlement_date,
                source_transaction_id="tx-1",
                rule_binding=bound_rule,
            )
            book = DurableSettlementBook(
                journal,
                provider_id=scope.provider_id,
                account_id=scope.account_id,
                environment=scope.environment,
                evidence_artifact_store=artifacts,
            )

            shadow_calls = []
            artifacts._read_verified_object_bytes = (
                lambda _manifest: shadow_calls.append(True) or raw
            )

            with self.assertRaises(SettlementConflict):
                book.prepare_register_mutation(
                    (obligation,),
                    committed_at="2026-09-25T09:00:01Z",
                )
            self.assertEqual(shadow_calls, [])


if __name__ == "__main__":
    unittest.main()
