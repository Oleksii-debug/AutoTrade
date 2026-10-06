from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.accounting import book_equity_fill, book_external_cash_flow
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.reconciliation import (
    ProviderFillEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.autotrade_mvp.scientific_financial_cut import (
    capture_current_scientific_financial_cut,
)
from research.autotrade_research.evaluation.scientific_financial_accounting_owner import (
    ScientificFinancialOwnerConflict,
    evaluate_gates_with_scientific_financial_accounting_owner,
    resolve_scientific_financial_accounting_owner,
)
from research.autotrade_research.evaluation.scientific_trial_owner import (
    gate_profile_subject_digest,
)
from research.autotrade_research.science.registry import ScientificRegistry
from research.tests.test_evaluation_gates import evidence, profile
from research.tests.test_science_registry import protocol


PROVIDER = "TEST_PROVIDER"
ACCOUNT = "test-account"
ENVIRONMENT = "SIMULATION"


def _bound_protocol(gate_profile, *, trial_budget: int = 1):
    value = deepcopy(protocol())
    value["trial_budget"] = trial_budget
    value["gate_profile_id"] = gate_profile.profile_id
    value["gate_profile_digest"] = gate_profile_subject_digest(gate_profile)
    return value


def _fill():
    return ProviderFillEvidence.create(
        side="BUY",
        evidence_refs=("test:normalized-fill",),
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
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
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        mode="ATOMIC",
        query_started_at="2026-09-24T17:00:00Z",
        query_completed_at="2026-09-24T19:00:00Z",
    )


def _reconciliation(*, local_cash: str = "900", provider_cash: str = "900"):
    return reconcile_account(
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        local_cash={"USD": local_cash},
        provider_cash={"USD": provider_cash},
        local_positions={"ABC": "1"},
        provider_positions={"ABC": "1"},
        local_execution_ids=["e1"],
        provider_fills=[_fill()],
        snapshot_consistency=_snapshot(),
        coverage_start="2026-09-24T17:00:00Z",
        coverage_end="2026-09-24T19:00:00Z",
        pagination_complete=True,
        provider_activity_provider_id=PROVIDER,
        provider_activity_account_id=ACCOUNT,
    )


def _economic_book(store: JournalStore) -> DurableProviderEconomicBook:
    return DurableProviderEconomicBook(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
    )


def _book_matching_provider(store: JournalStore) -> DurableProviderEconomicBook:
    book = _economic_book(store)
    book.append_batch(
        (
            book_external_cash_flow(
                transaction_id="cash-seed",
                cause_event_id="cash-seed-event",
                currency="USD",
                amount="1000",
            ),
            book_equity_fill(
                transaction_id="fill-e1",
                cause_event_id="provider-fill-e1",
                instrument="ABC",
                settlement_currency="USD",
                side="BUY",
                quantity="1",
                price="100",
            ),
        ),
        committed_at="2026-09-24T18:00:00Z",
    )
    return book


def _science_owner(directory: str):
    gate_profile = profile()
    registry = ScientificRegistry(Path(directory) / "science.sqlite3")
    registration = registry.register_protocol(
        _bound_protocol(gate_profile, trial_budget=1)
    )
    registry.record_trial(
        registration.protocol_id,
        status="COMPLETED",
        payload={"candidate": "candidate-1"},
    )
    return gate_profile, registry, registration


def _checkpoint_and_cut(
    store: JournalStore,
    *,
    gate_profile,
    registration,
    reconciliation=None,
):
    result = _reconciliation() if reconciliation is None else reconciliation
    checkpoint = record_reconciliation_checkpoint(
        store,
        reconciliation_id="wp36-financial-owner",
        result=result,
        observed_at="2026-09-24T19:00:00Z",
        host_id="test-host",
        owner_epoch="epoch-1",
    )
    cut = capture_current_scientific_financial_cut(
        store,
        scientific_protocol_id=registration.protocol_id,
        gate_profile_digest=gate_profile_subject_digest(gate_profile),
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        reconciliation_event_id=checkpoint["event_id"],
    )
    return checkpoint, cut


class ScientificFinancialAccountingOwnerTests(unittest.TestCase):
    def test_owner_binds_registry_cut_book_and_clean_reconciliation(self):
        with TemporaryDirectory() as directory:
            gate_profile, registry, registration = _science_owner(directory)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book = _book_matching_provider(store)
            _, cut = _checkpoint_and_cut(
                store,
                gate_profile=gate_profile,
                registration=registration,
            )

            owner = resolve_scientific_financial_accounting_owner(
                store=store,
                financial_cut=cut,
                scientific_registry=registry,
                profile=gate_profile,
            )

            self.assertEqual(owner.scientific_protocol_id, registration.protocol_id)
            self.assertEqual(owner.financial_cut_digest, cut.cut_digest)
            self.assertEqual(owner.economic_book_digest, book.audit_digest())
            self.assertEqual(owner.economic_transaction_count, 2)
            self.assertTrue(owner.owner_digest.startswith("sha256:"))
            self.assertTrue(owner.reconciled_cash_digest.startswith("sha256:"))
            self.assertTrue(owner.reconciled_position_digest.startswith("sha256:"))

    def test_balanced_but_wrong_local_book_cannot_ride_foreign_reconciliation(self):
        with TemporaryDirectory() as directory:
            gate_profile, registry, registration = _science_owner(directory)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            _, cut = _checkpoint_and_cut(
                store,
                gate_profile=gate_profile,
                registration=registration,
            )

            with self.assertRaisesRegex(
                ScientificFinancialOwnerConflict,
                "economic-book cash does not match",
            ):
                resolve_scientific_financial_accounting_owner(
                    store=store,
                    financial_cut=cut,
                    scientific_registry=registry,
                    profile=gate_profile,
                )

    def test_nonzero_reconciliation_difference_fails_closed(self):
        with TemporaryDirectory() as directory:
            gate_profile, registry, registration = _science_owner(directory)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            _book_matching_provider(store)
            _, cut = _checkpoint_and_cut(
                store,
                gate_profile=gate_profile,
                registration=registration,
                reconciliation=_reconciliation(provider_cash="899"),
            )

            with self.assertRaisesRegex(
                ScientificFinancialOwnerConflict,
                "cash_differences is non-zero",
            ):
                resolve_scientific_financial_accounting_owner(
                    store=store,
                    financial_cut=cut,
                    scientific_registry=registry,
                    profile=gate_profile,
                )

    def test_cut_becomes_invalid_after_any_later_journal_append(self):
        with TemporaryDirectory() as directory:
            gate_profile, registry, registration = _science_owner(directory)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            _book_matching_provider(store)
            _, cut = _checkpoint_and_cut(
                store,
                gate_profile=gate_profile,
                registration=registration,
            )
            _economic_book(store).append(
                book_external_cash_flow(
                    transaction_id="cash-later",
                    cause_event_id="cash-later-event",
                    currency="USD",
                    amount="1",
                ),
                committed_at="2026-09-24T19:01:00Z",
            )

            with self.assertRaisesRegex(
                ScientificFinancialOwnerConflict,
                "not the current exact journal cut",
            ):
                resolve_scientific_financial_accounting_owner(
                    store=store,
                    financial_cut=cut,
                    scientific_registry=registry,
                    profile=gate_profile,
                )

    def test_cut_cannot_be_reused_for_duplicate_registry_owner(self):
        with TemporaryDirectory() as directory:
            gate_profile, registry, registration = _science_owner(directory)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            _book_matching_provider(store)
            _, cut = _checkpoint_and_cut(
                store,
                gate_profile=gate_profile,
                registration=registration,
            )
            other = _bound_protocol(gate_profile, trial_budget=1)
            other["hypothesis"] = "duplicate profile owner"
            registry.register_protocol(other)

            with self.assertRaisesRegex(
                Exception,
                "multiple immutable scientific protocols",
            ):
                resolve_scientific_financial_accounting_owner(
                    store=store,
                    financial_cut=cut,
                    scientific_registry=registry,
                    profile=gate_profile,
                )

    def test_wrapper_ignores_caller_financial_pass_and_keeps_g2_inconclusive(self):
        with TemporaryDirectory() as directory:
            gate_profile, registry, registration = _science_owner(directory)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            _book_matching_provider(store)
            _, cut = _checkpoint_and_cut(
                store,
                gate_profile=gate_profile,
                registration=registration,
            )

            decision = evaluate_gates_with_scientific_financial_accounting_owner(
                gate_profile,
                evidence(
                    trials_attempted=1,
                    trial_log_complete=True,
                    financial_invariants_passed=True,
                ),
                scientific_registry=registry,
                financial_store=store,
                financial_cut=cut,
            )

            self.assertEqual(
                decision.checks["scientific_financial_accounting_owner"],
                "PASS",
            )
            self.assertEqual(decision.checks["financial_invariants"], "INCONCLUSIVE")
            self.assertEqual(decision.checks["semantic_owner_evidence"], "INCONCLUSIVE")
            self.assertEqual(decision.status, "INCONCLUSIVE")
            self.assertIn(
                "scientific_financial_economic_book_digest",
                decision.provenance,
            )

    def test_wrapper_ignores_caller_financial_fail_without_forging_pass(self):
        with TemporaryDirectory() as directory:
            gate_profile, registry, registration = _science_owner(directory)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            _book_matching_provider(store)
            _, cut = _checkpoint_and_cut(
                store,
                gate_profile=gate_profile,
                registration=registration,
            )

            decision = evaluate_gates_with_scientific_financial_accounting_owner(
                gate_profile,
                evidence(
                    trials_attempted=1,
                    trial_log_complete=True,
                    financial_invariants_passed=False,
                ),
                scientific_registry=registry,
                financial_store=store,
                financial_cut=cut,
            )

            self.assertEqual(
                decision.checks["scientific_financial_accounting_owner"],
                "PASS",
            )
            self.assertEqual(decision.checks["financial_invariants"], "INCONCLUSIVE")
            self.assertNotEqual(decision.status, "PASS")

    def test_wrapper_fails_stale_financial_owner_even_if_caller_claims_pass(self):
        with TemporaryDirectory() as directory:
            gate_profile, registry, registration = _science_owner(directory)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            _book_matching_provider(store)
            _, cut = _checkpoint_and_cut(
                store,
                gate_profile=gate_profile,
                registration=registration,
            )
            record_reconciliation_checkpoint(
                store,
                reconciliation_id="newer-provider-truth",
                result=_reconciliation(),
                observed_at="2026-09-24T19:01:00Z",
                host_id="test-host",
                owner_epoch="epoch-1",
            )

            decision = evaluate_gates_with_scientific_financial_accounting_owner(
                gate_profile,
                evidence(
                    trials_attempted=1,
                    trial_log_complete=True,
                    financial_invariants_passed=True,
                ),
                scientific_registry=registry,
                financial_store=store,
                financial_cut=cut,
            )

            self.assertEqual(
                decision.checks["scientific_financial_accounting_owner"],
                "FAIL",
            )
            self.assertEqual(decision.checks["financial_invariants"], "INCONCLUSIVE")
            self.assertEqual(decision.status, "FAIL")


if __name__ == "__main__":
    unittest.main()
