from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import provider_account_acquisition as acquisition_module
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.provider_account_acquisition import (
    DurableProviderAccountAcquisitionAuthority,
    ProviderAccountAcquisitionError,
    SerializedProviderAccountAcquisition,
)
from mvp.autotrade_mvp.provider_domain import ProviderFinancialScope


NOW = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)


def scope(provider_environment: str = "TESTNET") -> ProviderFinancialScope:
    return ProviderFinancialScope(
        provider_id="BYBIT",
        runtime_environment="PAPER",
        provider_environment=provider_environment,
        entity_policy_id="LINEAR_ACCOUNT_V1",
    )


def unrelated_event(version: int = 1) -> dict:
    payload = {"kind": "concurrent-financial-fact", "version": version}
    return {
        "event_id": f"concurrent-financial-fact-{version}",
        "event_type": "ConcurrentFinancialFact.v1",
        "schema_version": "1.0.0",
        "aggregate_type": "concurrent_financial_fact",
        "aggregate_id": "global",
        "aggregate_version": str(version),
        "host_id": "test",
        "owner_epoch": "test",
        "environment": "PAPER",
        "occurred_at": NOW.isoformat(),
        "observed_at": NOW.isoformat(),
        "committed_at": NOW.isoformat(),
        "correlation_id": f"concurrent-financial-fact-{version}",
        "causation_id": None,
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "evidence_refs": [],
    }


class DurableProviderAccountAcquisitionAuthorityTests(unittest.TestCase):
    def test_issue_binds_generation_to_exact_preissuance_journal_cut(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)

            issued = authority.issue_serialized(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id="read-cycle-1",
                committed_at=NOW,
            )

            self.assertEqual(issued.acquisition_generation, 1)
            self.assertEqual(issued.acquisition_journal_sequence_cut, 0)
            self.assertEqual(issued.issued_journal_sequence, 1)
            self.assertEqual(store.current_journal_sequence(), 1)
            self.assertTrue(
                issued.acquisition_id.startswith(
                    "provider-account-acquisition:sha256:"
                )
            )
            self.assertEqual(authority.require_current(issued), issued)

    def test_exact_request_retry_is_idempotent_while_current(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)
            first = authority.issue_serialized(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id="read-cycle-1",
                committed_at=NOW,
            )
            before = store.current_journal_sequence()

            retry = authority.issue_serialized(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id="read-cycle-1",
                committed_at=NOW,
            )

            self.assertEqual(retry, first)
            self.assertEqual(store.current_journal_sequence(), before)

    def test_newer_same_scope_generation_supersedes_older(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)
            first = authority.issue_serialized(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id="read-cycle-1",
                committed_at=NOW,
            )
            second = authority.issue_serialized(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id="read-cycle-2",
                committed_at=NOW,
            )

            self.assertEqual(second.acquisition_generation, 2)
            self.assertEqual(second.acquisition_journal_sequence_cut, 1)
            self.assertEqual(second.issued_journal_sequence, 2)
            self.assertEqual(authority.resolve_current(
                provider_scope=scope(),
                account_id="account-1",
            ), second)
            with self.assertRaisesRegex(
                ProviderAccountAcquisitionError,
                "superseded or forged",
            ):
                authority.require_current(first)
            with self.assertRaisesRegex(
                ProviderAccountAcquisitionError,
                "superseded acquisition",
            ):
                authority.issue_serialized(
                    provider_scope=scope(),
                    account_id="account-1",
                    acquisition_request_id="read-cycle-1",
                    committed_at=NOW,
                )

    def test_restart_reconstructs_current_generation_without_provider_io(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            first_authority = DurableProviderAccountAcquisitionAuthority(store)
            issued = first_authority.issue_serialized(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id="read-cycle-1",
                committed_at=NOW,
            )

            reopened = JournalStore(path)
            restarted = DurableProviderAccountAcquisitionAuthority(reopened)
            resolved = restarted.resolve_current(
                provider_scope=scope(),
                account_id="account-1",
            )

            self.assertEqual(resolved, issued)
            self.assertEqual(restarted.require_current(issued), issued)

    def test_testnet_and_demo_have_independent_generation_lineages(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)

            testnet = authority.issue_serialized(
                provider_scope=scope("TESTNET"),
                account_id="account-1",
                acquisition_request_id="same-operator-intent",
                committed_at=NOW,
            )
            demo = authority.issue_serialized(
                provider_scope=scope("DEMO"),
                account_id="account-1",
                acquisition_request_id="same-operator-intent",
                committed_at=NOW,
            )

            self.assertNotEqual(testnet.aggregate_id, demo.aggregate_id)
            self.assertNotEqual(testnet.acquisition_id, demo.acquisition_id)
            self.assertEqual(testnet.acquisition_generation, 1)
            self.assertEqual(demo.acquisition_generation, 1)

    def test_global_journal_interleave_fails_cas_before_acquisition_commit(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)
            original_commit = acquisition_module._COMMIT_COMMAND

            def interleaving_commit(bound_store, **kwargs):
                JournalStore.append_event(bound_store, unrelated_event())
                return original_commit(bound_store, **kwargs)

            with patch.object(
                acquisition_module,
                "_COMMIT_COMMAND",
                interleaving_commit,
            ):
                with self.assertRaisesRegex(
                    ProviderAccountAcquisitionError,
                    "changed concurrently",
                ):
                    authority.issue_serialized(
                        provider_scope=scope(),
                        account_id="account-1",
                        acquisition_request_id="read-cycle-1",
                        committed_at=NOW,
                    )

            self.assertEqual(store.current_journal_sequence(), 1)
            with self.assertRaisesRegex(
                ProviderAccountAcquisitionError,
                "no serialized",
            ):
                authority.resolve_current(
                    provider_scope=scope(),
                    account_id="account-1",
                )

    def test_caller_constructed_future_generation_is_not_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)
            authority.issue_serialized(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id="read-cycle-1",
                committed_at=NOW,
            )
            forged = SerializedProviderAccountAcquisition(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id="forged",
                acquisition_generation=2,
                acquisition_journal_sequence_cut=1,
            )

            with self.assertRaisesRegex(
                ProviderAccountAcquisitionError,
                "superseded or forged",
            ):
                authority.require_current(forged)
            self.assertEqual(store.current_journal_sequence(), 1)

    def test_post_construction_scope_mutation_fails_before_write(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)
            poisoned = scope()
            object.__setattr__(poisoned, "provider_environment", "demo")

            with self.assertRaisesRegex(
                ProviderAccountAcquisitionError,
                "not canonical at use time",
            ):
                authority.issue_serialized(
                    provider_scope=poisoned,
                    account_id="account-1",
                    acquisition_request_id="read-cycle-1",
                    committed_at=NOW,
                )
            self.assertEqual(store.current_journal_sequence(), 0)

    def test_store_subclass_and_composition_retarget_fail_closed(self):
        class HostileJournalStore(JournalStore):
            pass

        with TemporaryDirectory() as directory:
            with self.assertRaises(TypeError):
                DurableProviderAccountAcquisitionAuthority(
                    HostileJournalStore(Path(directory) / "hostile.sqlite3")
                )

            first = JournalStore(Path(directory) / "first.sqlite3")
            second = JournalStore(Path(directory) / "second.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(first)
            authority.store = second
            with self.assertRaisesRegex(
                ProviderAccountAcquisitionError,
                "composition changed",
            ):
                authority.issue_serialized(
                    provider_scope=scope(),
                    account_id="account-1",
                    acquisition_request_id="read-cycle-1",
                    committed_at=NOW,
                )
            self.assertEqual(first.current_journal_sequence(), 0)
            self.assertEqual(second.current_journal_sequence(), 0)

    def test_public_journal_method_rebinding_does_not_redirect_retained_path(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)

            def bomb(*_args, **_kwargs):
                raise AssertionError("retargeted public JournalStore method executed")

            with patch.object(JournalStore, "current_journal_sequence", bomb):
                issued = authority.issue_serialized(
                    provider_scope=scope(),
                    account_id="account-1",
                    acquisition_request_id="read-cycle-1",
                    committed_at=NOW,
                )
            self.assertEqual(issued.acquisition_generation, 1)

    def test_noncanonical_request_and_boolean_generation_fail_closed(self):
        with self.assertRaises(ProviderAccountAcquisitionError):
            SerializedProviderAccountAcquisition(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id=" bad ",
                acquisition_generation=1,
                acquisition_journal_sequence_cut=0,
            )
        with self.assertRaises(ProviderAccountAcquisitionError):
            SerializedProviderAccountAcquisition(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id="read-cycle-1",
                acquisition_generation=True,
                acquisition_journal_sequence_cut=0,
            )


if __name__ == "__main__":
    unittest.main()
