from datetime import datetime, timezone, tzinfo
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
from mvp.autotrade_mvp.provider_account_cut import ProviderAccountCutIdentity
from mvp.autotrade_mvp.provider_domain import ProviderFinancialScope


NOW = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)
D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64
D3 = "sha256:" + "3" * 64
D4 = "sha256:" + "4" * 64
Q1 = "provider-qualification:sha256:" + "1" * 64


def scope(provider_environment: str = "TESTNET") -> ProviderFinancialScope:
    return ProviderFinancialScope(
        provider_id="BYBIT",
        runtime_environment="PAPER",
        provider_environment=provider_environment,
        entity_policy_id="LINEAR_ACCOUNT_V1",
    )


def account_cut_for(
    acquisition: SerializedProviderAccountAcquisition,
    **overrides,
) -> ProviderAccountCutIdentity:
    values = {
        "provider_scope": acquisition.provider_scope,
        "account_id": acquisition.account_id,
        "acquisition_mode": "SERIALIZED_ACQUISITION_GENERATION",
        "acquisition_id": acquisition.acquisition_id,
        "acquisition_generation": acquisition.acquisition_generation,
        "acquisition_journal_sequence_cut":
            acquisition.acquisition_journal_sequence_cut,
        "qualification_identity_digest": Q1,
        "consistency_method_id": "snapshot-readback-v1",
        "consistency_method_version": 1,
        "origin_binding_set_digest": D1,
        "stream_binding_set_digest": D2,
        "backfill_binding_set_digest": D3,
        "coverage_window_digest": D4,
        "provider_native_generation_token": None,
    }
    values.update(overrides)
    return ProviderAccountCutIdentity(**values)


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

    def test_preexisting_durable_fact_is_inside_acquisition_pre_cut(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            JournalStore.append_event(store, unrelated_event())
            authority = DurableProviderAccountAcquisitionAuthority(store)

            issued = authority.issue_serialized(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id="read-cycle-after-financial-fact",
                committed_at=NOW,
            )

            self.assertEqual(issued.acquisition_journal_sequence_cut, 1)
            self.assertEqual(issued.issued_journal_sequence, 2)
            self.assertEqual(store.current_journal_sequence(), 2)

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

    def test_public_scope_class_rebinding_cannot_admit_foreign_scope(self):
        touched = []

        class ForeignScope:
            provider_id = "BYBIT"
            runtime_environment = "PAPER"
            provider_environment = "TESTNET"
            entity_policy_id = "LINEAR_ACCOUNT_V1"

            def payload(self):
                touched.append("payload")
                raise AssertionError("foreign scope callback executed")

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)
            with patch.object(
                acquisition_module,
                "ProviderFinancialScope",
                ForeignScope,
            ):
                with self.assertRaisesRegex(
                    ProviderAccountAcquisitionError,
                    "exact ProviderFinancialScope",
                ):
                    authority.issue_serialized(
                        provider_scope=ForeignScope(),
                        account_id="account-1",
                        acquisition_request_id="foreign-scope",
                        committed_at=NOW,
                    )

            self.assertEqual(touched, [])
            self.assertEqual(store.current_journal_sequence(), 0)

    def test_public_scope_lifecycle_and_equality_rebinding_do_not_execute(self):
        selected_scope = scope()
        touched = []

        def bomb_post_init(_self):
            touched.append("post_init")
            raise AssertionError("rebound ProviderFinancialScope.__post_init__ executed")

        def bomb_eq(_self, _other):
            touched.append("eq")
            raise AssertionError("rebound ProviderFinancialScope.__eq__ executed")

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)
            with (
                patch.object(ProviderFinancialScope, "__post_init__", bomb_post_init),
                patch.object(ProviderFinancialScope, "__eq__", bomb_eq),
            ):
                issued = authority.issue_serialized(
                    provider_scope=selected_scope,
                    account_id="account-1",
                    acquisition_request_id="read-cycle-pinned-lifecycle",
                    committed_at=NOW,
                )
                self.assertEqual(
                    authority.require_current(issued).acquisition_id,
                    issued.acquisition_id,
                )

            self.assertEqual(touched, [])
            self.assertEqual(store.current_journal_sequence(), 1)

    def test_public_scope_serializer_rebinding_does_not_redirect_identity(self):
        selected_scope = scope()
        touched = []

        def bomb_payload(_self):
            touched.append("payload")
            raise AssertionError("rebound ProviderFinancialScope.payload executed")

        def bomb_digest(_self):
            touched.append("digest")
            raise AssertionError(
                "rebound ProviderFinancialScope.content_digest executed"
            )

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)
            with (
                patch.object(ProviderFinancialScope, "payload", bomb_payload),
                patch.object(
                    ProviderFinancialScope,
                    "content_digest",
                    property(bomb_digest),
                ),
            ):
                issued = authority.issue_serialized(
                    provider_scope=selected_scope,
                    account_id="account-1",
                    acquisition_request_id="read-cycle-pinned-scope",
                    committed_at=NOW,
                )

            self.assertEqual(touched, [])
            self.assertEqual(issued.provider_scope, selected_scope)
            self.assertEqual(store.current_journal_sequence(), 1)

    def test_public_canonical_helper_rebinding_does_not_redirect_identity(self):
        touched = []

        def bomb(*_args, **_kwargs):
            touched.append("helper")
            raise AssertionError("rebound canonical helper executed")

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)
            with (
                patch.object(acquisition_module, "canonical_json", bomb),
                patch.object(acquisition_module, "payload_digest", bomb),
                patch.object(acquisition_module, "sha256", bomb),
            ):
                issued = authority.issue_serialized(
                    provider_scope=scope(),
                    account_id="account-1",
                    acquisition_request_id="read-cycle-pinned-helpers",
                    committed_at=NOW,
                )

            self.assertEqual(touched, [])
            self.assertEqual(issued.acquisition_generation, 1)
            self.assertEqual(store.current_journal_sequence(), 1)

    def test_public_account_cut_lifecycle_rebinding_does_not_execute(self):
        touched = []

        def bomb_post_init(_self):
            touched.append("post_init")
            raise AssertionError("rebound ProviderAccountCutIdentity.__post_init__ executed")

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)
            issued = authority.issue_serialized(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id="read-cycle-1",
                committed_at=NOW,
            )
            cut = account_cut_for(issued)
            with patch.object(
                ProviderAccountCutIdentity,
                "__post_init__",
                bomb_post_init,
            ):
                rebound_safe = authority.require_account_cut_acquisition(cut)

            self.assertEqual(touched, [])
            self.assertEqual(rebound_safe.acquisition_id, issued.acquisition_id)
            self.assertEqual(store.current_journal_sequence(), 1)

    def test_public_acquisition_lifecycle_rebinding_does_not_execute_on_revalidation(self):
        touched = []

        def bomb_post_init(_self):
            touched.append("post_init")
            raise AssertionError(
                "rebound SerializedProviderAccountAcquisition.__post_init__ executed"
            )

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)
            issued = authority.issue_serialized(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id="read-cycle-1",
                committed_at=NOW,
            )
            with patch.object(
                SerializedProviderAccountAcquisition,
                "__post_init__",
                bomb_post_init,
            ):
                current = authority.require_current(issued)

            self.assertEqual(touched, [])
            self.assertEqual(current.acquisition_id, issued.acquisition_id)
            self.assertEqual(store.current_journal_sequence(), 1)

    def test_public_account_cut_class_rebinding_cannot_admit_foreign_cut(self):
        touched = []

        class ForeignCut:
            def __getattribute__(self, name):
                if name.startswith("_"):
                    return object.__getattribute__(self, name)
                touched.append(name)
                raise AssertionError("foreign account-cut attribute executed")

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)
            authority.issue_serialized(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id="read-cycle-1",
                committed_at=NOW,
            )
            with patch.object(
                acquisition_module,
                "ProviderAccountCutIdentity",
                ForeignCut,
            ):
                with self.assertRaisesRegex(
                    ProviderAccountAcquisitionError,
                    "exact ProviderAccountCutIdentity",
                ):
                    authority.require_account_cut_acquisition(ForeignCut())

            self.assertEqual(touched, [])
            self.assertEqual(store.current_journal_sequence(), 1)

    def test_account_cut_must_bind_exact_current_durable_acquisition(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)
            issued = authority.issue_serialized(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id="read-cycle-1",
                committed_at=NOW,
            )
            cut = account_cut_for(issued)

            self.assertEqual(
                authority.require_account_cut_acquisition(cut),
                issued,
            )

    def test_stale_account_cut_cannot_survive_newer_acquisition(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)
            first = authority.issue_serialized(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id="read-cycle-1",
                committed_at=NOW,
            )
            stale_cut = account_cut_for(first)
            authority.issue_serialized(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id="read-cycle-2",
                committed_at=NOW,
            )

            with self.assertRaisesRegex(
                ProviderAccountAcquisitionError,
                "acquisition_id is not current",
            ):
                authority.require_account_cut_acquisition(stale_cut)

    def test_account_cut_cannot_self_author_forged_acquisition_identity(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)
            issued = authority.issue_serialized(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id="read-cycle-1",
                committed_at=NOW,
            )
            forged = account_cut_for(
                issued,
                acquisition_id="self-authored-acquisition",
            )

            with self.assertRaisesRegex(
                ProviderAccountAcquisitionError,
                "acquisition_id is not current",
            ):
                authority.require_account_cut_acquisition(forged)

    def test_account_cut_generation_and_journal_cut_are_independently_bound(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)
            issued = authority.issue_serialized(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id="read-cycle-1",
                committed_at=NOW,
            )

            wrong_generation = account_cut_for(
                issued,
                acquisition_generation=issued.acquisition_generation + 1,
            )
            with self.assertRaisesRegex(
                ProviderAccountAcquisitionError,
                "acquisition_generation is not current",
            ):
                authority.require_account_cut_acquisition(wrong_generation)

            wrong_cut = account_cut_for(
                issued,
                acquisition_journal_sequence_cut=
                    issued.acquisition_journal_sequence_cut + 1,
            )
            with self.assertRaisesRegex(
                ProviderAccountAcquisitionError,
                "journal cut does not match",
            ):
                authority.require_account_cut_acquisition(wrong_cut)

    def test_provider_native_account_cut_is_not_serialized_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)
            issued = authority.issue_serialized(
                provider_scope=scope(),
                account_id="account-1",
                acquisition_request_id="read-cycle-1",
                committed_at=NOW,
            )
            native = account_cut_for(
                issued,
                acquisition_mode="PROVIDER_NATIVE_GENERATION",
                provider_native_generation_token="provider-token-1",
            )

            with self.assertRaisesRegex(
                ProviderAccountAcquisitionError,
                "does not use serialized",
            ):
                authority.require_account_cut_acquisition(native)

    def test_hostile_tzinfo_is_rejected_before_callback_or_write(self):
        touched = []

        class HostileTimezone(tzinfo):
            def utcoffset(self, _dt):
                touched.append("utcoffset")
                raise AssertionError("hostile tzinfo callback executed")

            def dst(self, _dt):
                touched.append("dst")
                raise AssertionError("hostile tzinfo callback executed")

            def tzname(self, _dt):
                touched.append("tzname")
                raise AssertionError("hostile tzinfo callback executed")

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            authority = DurableProviderAccountAcquisitionAuthority(store)
            hostile_time = datetime(
                2026, 10, 4, 1, 0, tzinfo=HostileTimezone()
            )

            with self.assertRaisesRegex(
                ProviderAccountAcquisitionError,
                "exact datetime with datetime.timezone",
            ):
                authority.issue_serialized(
                    provider_scope=scope(),
                    account_id="account-1",
                    acquisition_request_id="read-cycle-1",
                    committed_at=hostile_time,
                )

            self.assertEqual(touched, [])
            self.assertEqual(store.current_journal_sequence(), 0)

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
