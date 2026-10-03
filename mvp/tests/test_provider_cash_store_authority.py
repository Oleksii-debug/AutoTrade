from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import (
    AccountingConflict,
    book_external_provider_cash_activity,
)
from mvp.autotrade_mvp.reconciliation import ProviderActivityEvidence


def _deposit() -> ProviderActivityEvidence:
    return ProviderActivityEvidence.create(
        provider_id="ALPACA",
        account_id="acct-cash-authority",
        environment="PAPER",
        activity_id="deposit-1",
        activity_type="DEPOSIT",
        origin="EXTERNAL",
        occurred_at="2026-10-03T15:00:00Z",
        currency="USD",
        signed_amount="100",
    )


def _book(store: JournalStore, activity=None):
    return book_external_provider_cash_activity(
        store,
        provider_id="ALPACA",
        account_id="acct-cash-authority",
        environment="PAPER",
        activity=_deposit() if activity is None else activity,
        observed_at="2026-10-03T15:00:01Z",
    )


class ProviderCashStoreAuthorityTests(unittest.TestCase):
    def test_provider_activity_subclass_cannot_enter_external_cash_authority(self):
        with TemporaryDirectory() as directory:
            canonical = _deposit()

            class ActivitySubclass(ProviderActivityEvidence):
                pass

            hostile = ActivitySubclass(
                provider_id=canonical.provider_id,
                account_id=canonical.account_id,
                environment=canonical.environment,
                activity_id=canonical.activity_id,
                activity_type=canonical.activity_type,
                origin=canonical.origin,
                occurred_at=canonical.occurred_at,
                instrument=canonical.instrument,
                currency=canonical.currency,
                client_order_id=canonical.client_order_id,
                provider_order_id=canonical.provider_order_id,
                provider_execution_id=canonical.provider_execution_id,
                signed_amount=canonical.signed_amount,
            )
            store = JournalStore(Path(directory) / "selected.sqlite3")
            with self.assertRaisesRegex(
                TypeError,
                "exact ProviderActivityEvidence",
            ):
                _book(store, hostile)
            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "economic_book",
                    "unused",
                ),
                [],
            )

    def test_hidden_provider_activity_state_cannot_enter_external_cash_authority(self):
        with TemporaryDirectory() as directory:
            activity = _deposit()
            object.__setattr__(
                activity,
                "unreviewed_state",
                "must-not-enter-financial-authority",
            )
            store = JournalStore(Path(directory) / "selected.sqlite3")
            with self.assertRaisesRegex(TypeError, "unexpected state fields"):
                _book(store, activity)

    def test_journal_store_subclass_cannot_mint_external_cash_economics(self):
        with TemporaryDirectory() as directory:
            calls = []

            class HostileStore(JournalStore):
                def load_command_event_batch(self, **_kwargs):
                    calls.append("load")
                    raise AssertionError("hostile JournalStore callback executed")

            store = HostileStore(Path(directory) / "hostile.sqlite3")
            with self.assertRaises(TypeError):
                _book(store)
            self.assertEqual(calls, [])

    def test_exact_store_instance_shadow_cannot_enter_cash_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "selected.sqlite3")
            calls = []

            def hostile(**_kwargs):
                calls.append("load")
                raise AssertionError("instance shadow callback executed")

            vars(store)["load_command_event_batch"] = hostile
            with self.assertRaises((AccountingConflict, TypeError)):
                _book(store)
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
