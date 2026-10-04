from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.accounting import AccountingConflict
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
    PreparedProviderFillBinding,
    commit_economic_batch_with_reservation_consumption,
)


class PaperProviderFillBindingAuthorityTests(unittest.TestCase):
    def test_exact_dataclass_construction_is_not_provider_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reservations = DurableReservationBook(
                store,
                environment="PAPER",
                account_id="paper-provider-authority",
            )
            economics = DurableProviderEconomicBook(
                store,
                provider_id="SIMULATED",
                account_id="paper-provider-authority",
                environment="PAPER",
            )
            forged = PreparedProviderFillBinding(
                aggregate_id="caller-authored-binding",
                envelope=None,
                request={
                    "provider_id": "SIMULATED",
                    "account_id": "paper-provider-authority",
                    "environment": "PAPER",
                    "reservation_id": "reservation-1",
                },
                result={},
                aggregate_version=1,
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "lacks canonical provider-evidence authority",
            ):
                commit_economic_batch_with_reservation_consumption(
                    economics,
                    reservations,
                    command_id="forged-paper-fill",
                    idempotency_key="forged-paper-fill",
                    reservation_id="reservation-1",
                    usage={"CASH:USD": "100"},
                    transactions=(),
                    committed_at="2026-10-03T19:00:00Z",
                    provider_fill_binding=forged,
                )

            self.assertEqual(economics.transactions, ())


if __name__ == "__main__":
    unittest.main()
