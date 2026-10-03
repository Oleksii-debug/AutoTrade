from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts.store import ArtifactStore

from mvp.autotrade_mvp.authority import AuthorityConflict, AuthorityService
from mvp.autotrade_mvp.durable_settlement import DurableSettlementBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook


class SettlementProviderDomainAuthorityTests(unittest.TestCase):
    def test_matching_bybit_testnet_settlement_and_economic_authorities_compose(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.sqlite3")
            artifacts = ArtifactStore(root / "settlement-evidence")
            economic = DurableProviderEconomicBook(
                store,
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="TESTNET",
            )
            settlement = DurableSettlementBook(
                store,
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="TESTNET",
                evidence_artifact_store=artifacts,
            )
            authority = AuthorityService(
                store,
                evidence_artifact_store=artifacts,
                settlement_book=settlement,
                economic_book=economic,
            )

            evidence = authority._settlement_cash_availability_adjustments(
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="TESTNET",
                provider_available={"CASH:USD": Decimal("100")},
            )

            self.assertIsNotNone(evidence)
            self.assertEqual(
                evidence["resources"]["CASH:USD"],
                {
                    "provider_available": "100",
                    "local_available_to_spend": "0",
                    "reservable_available": "0",
                },
            )
            self.assertEqual(evidence["settlement_scope_id"], settlement.scope_id)

    def test_cross_domain_settlement_authority_remains_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JournalStore(root / "journal.sqlite3")
            artifacts = ArtifactStore(root / "settlement-evidence")
            economic = DurableProviderEconomicBook(
                store,
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="TESTNET",
            )
            settlement = DurableSettlementBook(
                store,
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="TESTNET",
                evidence_artifact_store=artifacts,
            )
            authority = AuthorityService(
                store,
                evidence_artifact_store=artifacts,
                settlement_book=settlement,
                economic_book=economic,
            )

            with self.assertRaisesRegex(
                AuthorityConflict,
                "scope does not match admission",
            ):
                authority._settlement_cash_availability_adjustments(
                    provider_id="BYBIT",
                    account_id="acct-1",
                    environment="PAPER",
                    provider_environment="DEMO",
                    provider_available={"CASH:USD": Decimal("100")},
                )


if __name__ == "__main__":
    unittest.main()
