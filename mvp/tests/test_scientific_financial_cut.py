from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.reconciliation import (
    ProviderFillEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.autotrade_mvp import scientific_financial_cut as cut_module
from mvp.autotrade_mvp.scientific_financial_cut import (
    FinancialCutConflict,
    FinancialCutUnavailable,
    ScientificFinancialCut,
    capture_current_scientific_financial_cut,
)


_SHA = "sha256:" + "1" * 64


class HostileText(str):
    calls = 0

    def strip(self, *args, **kwargs):
        type(self).calls += 1
        raise AssertionError("hostile text callback executed")


def _fill():
    return ProviderFillEvidence.create(
        side="BUY",
        evidence_refs=("test:normalized-fill",),
        provider_id="TEST_PROVIDER",
        account_id="test-account",
        environment="PAPER",
        provider_execution_id="e1",
        client_order_id="c1",
        instrument="ABC",
        quantity="1",
        price="100",
        fee_currency="USD",
        trade_time="2026-09-24T18:00:00Z",
    )


def _snapshot():
    return SnapshotConsistencyEvidence(
        provider_id="TEST_PROVIDER",
        account_id="test-account",
        environment="PAPER",
        mode="ATOMIC",
        query_started_at="2026-09-24T17:00:00Z",
        query_completed_at="2026-09-24T19:00:00Z",
    )


def _reconciliation(*, provider_cash: str = "900"):
    return reconcile_account(
        provider_id="TEST_PROVIDER",
        account_id="test-account",
        environment="PAPER",
        local_cash={"USD": "900"},
        provider_cash={"USD": provider_cash},
        local_positions={"ABC": "1"},
        provider_positions={"ABC": "1"},
        local_execution_ids=["e1"],
        provider_fills=[_fill()],
        snapshot_consistency=_snapshot(),
        coverage_start="2026-09-24T17:00:00Z",
        coverage_end="2026-09-24T19:00:00Z",
        pagination_complete=True,
        provider_activity_provider_id="TEST_PROVIDER",
        provider_activity_account_id="test-account",
    )


def _capture(store, **overrides):
    values = {
        "scientific_protocol_id": "protocol-1",
        "gate_profile_digest": _SHA,
        "provider_id": "TEST_PROVIDER",
        "account_id": "test-account",
        "environment": "PAPER",
        "reconciliation_event_id": "missing-checkpoint",
    }
    values.update(overrides)
    return capture_current_scientific_financial_cut(store, **values)


class ScientificFinancialCutTests(unittest.TestCase):
    def test_rejects_polymorphic_text_before_financial_authority_dispatch(self):
        HostileText.calls = 0
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.db")
            with self.assertRaisesRegex(TypeError, "exact built-in text"):
                _capture(store, provider_id=HostileText("TEST_PROVIDER"))
        self.assertEqual(HostileText.calls, 0)

    def test_rejects_non_journal_store_before_constructing_cut(self):
        with self.assertRaises(TypeError):
            _capture(object())

    def test_missing_current_reconciliation_is_unavailable_not_financial_pass(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.db")
            with self.assertRaisesRegex(FinancialCutUnavailable, "unavailable"):
                _capture(store)

    def test_profile_binding_requires_canonical_sha256_before_store_dispatch(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.db")
            with self.assertRaisesRegex(ValueError, "canonical SHA-256"):
                _capture(store, gate_profile_digest="not-a-digest")

    def test_cut_identity_contains_digests_only_not_mutable_financial_state(self):
        cut = ScientificFinancialCut(
            scientific_protocol_id="protocol-1",
            gate_profile_digest=_SHA,
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            reconciliation_event_id="checkpoint-1",
            reconciliation_journal_sequence=2,
            journal_sequence=3,
            journal_population_digest=_SHA,
            reconciliation_checkpoint_digest=_SHA,
        )
        self.assertEqual(cut.journal_sequence, 3)
        self.assertFalse(hasattr(cut, "journal_state"))
        self.assertTrue(cut.cut_digest.startswith("sha256:"))
        with self.assertRaises(AttributeError):
            object.__setattr__(cut, "hidden_authority", "forged")

    def test_direct_cut_value_canonicalizes_scope_identity(self):
        canonical = ScientificFinancialCut(
            scientific_protocol_id="protocol-1",
            gate_profile_digest=_SHA,
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            reconciliation_event_id="checkpoint-1",
            reconciliation_journal_sequence=2,
            journal_sequence=3,
            journal_population_digest=_SHA,
            reconciliation_checkpoint_digest=_SHA,
        )
        alias = ScientificFinancialCut(
            scientific_protocol_id="protocol-1",
            gate_profile_digest=_SHA,
            provider_id="test_provider",
            account_id="test-account",
            environment="paper",
            reconciliation_event_id="checkpoint-1",
            reconciliation_journal_sequence=2,
            journal_sequence=3,
            journal_population_digest=_SHA,
            reconciliation_checkpoint_digest=_SHA,
        )
        self.assertEqual(alias.provider_id, "TEST_PROVIDER")
        self.assertEqual(alias.environment, "PAPER")
        self.assertEqual(alias.cut_digest, canonical.cut_digest)

    def test_bool_is_not_accepted_as_journal_sequence(self):
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            ScientificFinancialCut(
                scientific_protocol_id="protocol-1",
                gate_profile_digest=_SHA,
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                reconciliation_event_id="checkpoint-1",
                reconciliation_journal_sequence=1,
                journal_sequence=True,
                journal_population_digest=_SHA,
                reconciliation_checkpoint_digest=_SHA,
            )

    def test_reconciliation_sequence_cannot_be_forged_past_financial_cut(self):
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            ScientificFinancialCut(
                scientific_protocol_id="protocol-1",
                gate_profile_digest=_SHA,
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                reconciliation_event_id="checkpoint-1",
                reconciliation_journal_sequence=4,
                journal_sequence=3,
                journal_population_digest=_SHA,
                reconciliation_checkpoint_digest=_SHA,
            )

    def test_scope_aliases_share_one_canonical_financial_cut_identity(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.db")
            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="science-cut-alias",
                result=_reconciliation(),
                observed_at="2026-09-24T19:00:00Z",
                host_id="test-host",
                owner_epoch="epoch-1",
            )
            exact = _capture(
                store,
                provider_id="TEST_PROVIDER",
                environment="PAPER",
                reconciliation_event_id=checkpoint["event_id"],
            )
            alias = _capture(
                store,
                provider_id="test_provider",
                environment="paper",
                reconciliation_event_id=checkpoint["event_id"],
            )

            self.assertEqual(exact.provider_id, alias.provider_id)
            self.assertEqual(exact.provider_id, "TEST_PROVIDER")
            self.assertEqual(exact.environment, alias.environment)
            self.assertEqual(exact.environment, "PAPER")
            self.assertEqual(exact.cut_digest, alias.cut_digest)

    def test_absent_checkpoint_race_is_conflict_not_stable_unavailable(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.db")
            mutation_happened = False

            def append_then_report_absent(
                selected_store,
                *,
                provider_id,
                account_id,
                environment,
            ):
                nonlocal mutation_happened
                self.assertIs(selected_store, store)
                self.assertEqual(provider_id, "TEST_PROVIDER")
                self.assertEqual(account_id, "test-account")
                self.assertEqual(environment, "PAPER")
                record_reconciliation_checkpoint(
                    store,
                    reconciliation_id="science-cut-race",
                    result=_reconciliation(),
                    observed_at="2026-09-24T19:00:00Z",
                    host_id="test-host",
                    owner_epoch="epoch-1",
                )
                mutation_happened = True
                return None

            with patch.object(
                cut_module,
                "load_latest_reconciliation_checkpoint_for_scope",
                new=append_then_report_absent,
            ):
                with self.assertRaisesRegex(
                    FinancialCutConflict,
                    "financial journal changed",
                ):
                    _capture(store)

            self.assertTrue(mutation_happened)
            self.assertEqual(store.current_journal_sequence(), 1)

    def test_cut_distinguishes_reconciliation_sequence_from_later_journal_truth(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.db")
            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="science-cut-ordering",
                result=_reconciliation(),
                observed_at="2026-09-24T19:00:00Z",
                host_id="test-host",
                owner_epoch="epoch-1",
            )
            payload = {"fact": "later non-reconciliation financial journal fact"}
            store.append_event(
                {
                    "event_id": "later-scientific-cut-fact",
                    "event_type": "ScientificCutOrderingFact",
                    "aggregate_type": "scientific_cut_ordering",
                    "aggregate_id": "ordering-1",
                    "aggregate_version": "1",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": "2026-09-24T19:00:30Z",
                }
            )

            cut = _capture(
                store,
                reconciliation_event_id=checkpoint["event_id"],
            )
            self.assertEqual(
                cut.reconciliation_journal_sequence,
                checkpoint["journal_sequence"],
            )
            self.assertGreater(
                cut.journal_sequence,
                cut.reconciliation_journal_sequence,
            )

    def test_checkpoint_must_equal_exact_event_at_bound_population_sequence(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.db")
            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="science-cut-bound-slot",
                result=_reconciliation(),
                observed_at="2026-09-24T19:00:00Z",
                host_id="test-host",
                owner_epoch="epoch-1",
            )
            original = cut_module.require_current_reconciliation_checkpoint

            def forged_checkpoint(*args, **kwargs):
                result = dict(original(*args, **kwargs))
                result["event_id"] = "forged-current-checkpoint"
                return result

            with patch.object(
                cut_module,
                "require_current_reconciliation_checkpoint",
                side_effect=forged_checkpoint,
            ):
                with self.assertRaisesRegex(
                    FinancialCutConflict,
                    "does not match the frozen journal population",
                ):
                    _capture(
                        store,
                        reconciliation_event_id=checkpoint["event_id"],
                    )

    def test_exact_journal_population_cut_rejects_superseded_provider_truth(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.db")
            first_checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="science-cut-a",
                result=_reconciliation(),
                observed_at="2026-09-24T19:00:00Z",
                host_id="test-host",
                owner_epoch="epoch-1",
            )
            first = _capture(
                store,
                reconciliation_event_id=first_checkpoint["event_id"],
            )
            repeated = _capture(
                store,
                reconciliation_event_id=first_checkpoint["event_id"],
            )

            self.assertEqual(first, repeated)
            self.assertEqual(
                first.journal_sequence,
                first_checkpoint["journal_sequence"],
            )
            self.assertEqual(
                first.reconciliation_journal_sequence,
                first_checkpoint["journal_sequence"],
            )
            self.assertTrue(first.journal_population_digest.startswith("sha256:"))
            self.assertTrue(
                first.reconciliation_checkpoint_digest.startswith("sha256:")
            )
            self.assertTrue(first.cut_digest.startswith("sha256:"))

            second_checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="science-cut-b",
                result=_reconciliation(provider_cash="901"),
                observed_at="2026-09-24T19:01:00Z",
                host_id="test-host",
                owner_epoch="epoch-1",
            )

            with self.assertRaisesRegex(FinancialCutConflict, "not authoritative"):
                _capture(
                    store,
                    reconciliation_event_id=first_checkpoint["event_id"],
                )

            second = _capture(
                store,
                reconciliation_event_id=second_checkpoint["event_id"],
            )
            self.assertGreater(second.journal_sequence, first.journal_sequence)
            self.assertEqual(
                second.reconciliation_journal_sequence,
                second_checkpoint["journal_sequence"],
            )
            self.assertNotEqual(
                second.journal_population_digest,
                first.journal_population_digest,
            )
            self.assertNotEqual(
                second.reconciliation_checkpoint_digest,
                first.reconciliation_checkpoint_digest,
            )
            self.assertNotEqual(second.cut_digest, first.cut_digest)


    def test_corrupt_checkpoint_history_is_conflict_after_stable_recheck(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.db")

            with patch.object(
                cut_module,
                "load_latest_reconciliation_checkpoint_for_scope",
                side_effect=ValueError("corrupt reconciliation chronology"),
            ):
                with self.assertRaisesRegex(
                    FinancialCutConflict,
                    "history is not authoritative",
                ):
                    _capture(store)

    def test_corrupt_checkpoint_race_prioritizes_changed_financial_cut(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.db")

            def append_then_fail(*args, **kwargs):
                record_reconciliation_checkpoint(
                    store,
                    reconciliation_id="science-cut-corrupt-race",
                    result=_reconciliation(),
                    observed_at="2026-09-24T19:00:00Z",
                    host_id="test-host",
                    owner_epoch="epoch-1",
                )
                raise ValueError("corrupt reconciliation chronology")

            with patch.object(
                cut_module,
                "load_latest_reconciliation_checkpoint_for_scope",
                new=append_then_fail,
            ):
                with self.assertRaisesRegex(
                    FinancialCutConflict,
                    "financial journal changed",
                ):
                    _capture(store)


if __name__ == "__main__":
    unittest.main()
