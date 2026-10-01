from datetime import datetime, timezone
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_transport import (
    KrakenSpotDurableNonceAllocator,
    ProviderTransportScopeError,
    WhiteBitDurableNonceAllocator,
    _DurableProviderNonceAllocator,
)
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle


class _JournalStoreSubclass(JournalStore):
    pass


def _subclass_shell(store: JournalStore) -> JournalStore:
    shell = object.__new__(_JournalStoreSubclass)
    shell.__dict__.update(store.__dict__)
    return shell


class ProviderNonceJournalAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)

    def test_generic_nonce_allocator_rejects_journal_store_subclass_shell(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            shell = _subclass_shell(store)
            with self.assertRaisesRegex(TypeError, "exact JournalStore"):
                _DurableProviderNonceAllocator(
                    provider_id="TEST",
                    display_name="Test",
                    journal=shell,
                    account_id="acct",
                    environment="LIVE",
                    clock_millis=lambda: 100,
                    clock_utc=lambda: self.now,
                )
            self.assertEqual(store.load_events_by_aggregate_type("provider_nonce"), ())

    def test_whitebit_manager_rejects_journal_store_subclass_shell(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            shell = _subclass_shell(store)
            with self.assertRaisesRegex(TypeError, "exact JournalStore"):
                WhiteBitDurableNonceAllocator(
                    journal=shell,
                    account_id="acct-wb",
                    environment="LIVE",
                    clock_millis=lambda: 100,
                    clock_utc=lambda: self.now,
                )
            self.assertEqual(store.load_events_by_aggregate_type("provider_nonce"), ())

    def test_kraken_manager_rejects_journal_store_subclass_shell(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            shell = _subclass_shell(store)
            handle = PersistentCredentialHandle(
                handle_id="cred-kraken",
                account_id="acct-kraken",
                provider="KRAKEN",
                environment="LIVE",
                purpose="TRADE",
                generation=1,
            )
            with self.assertRaisesRegex(TypeError, "exact JournalStore"):
                KrakenSpotDurableNonceAllocator(
                    journal=shell,
                    account_id="acct-kraken",
                    environment="LIVE",
                    credential_handle=handle,
                    clock_millis=lambda: 100,
                    clock_utc=lambda: self.now,
                )
            self.assertEqual(store.load_events_by_aggregate_type("provider_nonce"), ())

    def test_manager_store_replacement_fails_before_nonce_domain_creation(self):
        with TemporaryDirectory() as directory:
            first = JournalStore(f"{directory}/first.sqlite3")
            second = JournalStore(f"{directory}/second.sqlite3")
            manager = WhiteBitDurableNonceAllocator(
                journal=first,
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: 100,
                clock_utc=lambda: self.now,
            )
            manager.journal = second
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "nonce journal authority changed",
            ):
                manager.for_provider_api_key("provider-key")
            self.assertEqual(first.load_events_by_aggregate_type("provider_nonce"), ())
            self.assertEqual(second.load_events_by_aggregate_type("provider_nonce"), ())

    def test_nonce_domain_store_replacement_fails_before_append(self):
        with TemporaryDirectory() as directory:
            first = JournalStore(f"{directory}/first.sqlite3")
            second = JournalStore(f"{directory}/second.sqlite3")
            manager = WhiteBitDurableNonceAllocator(
                journal=first,
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: 100,
                clock_utc=lambda: self.now,
            )
            allocator = manager.for_provider_api_key("provider-key")
            allocator.journal = second
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "nonce journal authority changed",
            ):
                allocator.allocate()
            self.assertEqual(first.load_events_by_aggregate_type("provider_nonce"), ())
            self.assertEqual(second.load_events_by_aggregate_type("provider_nonce"), ())

    def test_nonce_send_lock_revalidates_selected_store_identity(self):
        with TemporaryDirectory() as directory:
            first = JournalStore(f"{directory}/first.sqlite3")
            second = JournalStore(f"{directory}/second.sqlite3")
            manager = WhiteBitDurableNonceAllocator(
                journal=first,
                account_id="acct-wb",
                environment="LIVE",
                clock_millis=lambda: 100,
                clock_utc=lambda: self.now,
            )
            allocator = manager.for_provider_api_key("provider-key")
            allocator.journal = second
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "nonce journal authority changed",
            ):
                allocator.serialized_send()
            self.assertEqual(first.load_events_by_aggregate_type("provider_nonce"), ())
            self.assertEqual(second.load_events_by_aggregate_type("provider_nonce"), ())


if __name__ == "__main__":
    unittest.main()
