from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reconciliation import (
    ProviderFillEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
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
            journal_sequence=3,
            journal_population_digest=_SHA,
            reconciliation_checkpoint_digest=_SHA,
        )
        self.assertEqual(cut.journal_sequence, 3)
        self.assertFalse(hasattr(cut, "journal_state"))
        self.assertTrue(cut.cut_digest.startswith("sha256:"))
        with self.assertRaises(AttributeError):
            object.__setattr__(cut, "hidden_authority", "forged")

    def test_bool_is_not_accepted_as_journal_sequence(self):
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            ScientificFinancialCut(
                scientific_protocol_id="protocol-1",
                gate_profile_digest=_SHA,
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                reconciliation_event_id="checkpoint-1",
                journal_sequence=True,
                journal_population_digest=_SHA,
                reconciliation_checkpoint_digest=_SHA,
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
            self.assertNotEqual(
                second.journal_population_digest,
                first.journal_population_digest,
            )
            self.assertNotEqual(
                second.reconciliation_checkpoint_digest,
                first.reconciliation_checkpoint_digest,
            )
            self.assertNotEqual(second.cut_digest, first.cut_digest)


if __name__ == "__main__":
    unittest.main()
