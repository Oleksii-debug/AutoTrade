from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.borrow import (
    BorrowLifecycleJournal,
    BorrowLoanEvidence,
    BorrowLocateEvidence,
    BorrowRecallEvidence,
    BorrowRecallResolutionEvidence,
    BorrowResourceIdentity,
)
from mvp.autotrade_mvp.corporate_actions import EquityState
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reconciliation import (
    ResourceAvailabilityEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import (
    load_account_resource_availability_evidence,
    record_reconciliation_checkpoint,
)
from mvp.autotrade_mvp.risk import RiskContext


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"
PROVIDER_ID = "TEST_PROVIDER"
ACCOUNT_ID = "borrow-reconciliation"
ENVIRONMENT = "PAPER"
NOW = "2026-09-25T05:01:00Z"


def resource():
    return BorrowResourceIdentity(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        instrument_id=INSTRUMENT_ID,
        instrument_version=1,
    )


def context(*, position="-40", reserved="0"):
    return RiskContext.create(
        state_version=1,
        equity="10000",
        positions={"ABC": position},
        marks={"ABC": "10"},
        reserved_position_delta=(
            {} if Decimal(reserved) == 0 else {"ABC": reserved}
        ),
        daily_pnl="0",
        drawdown_fraction="0",
        market_data_age_seconds="1",
        fx_age_seconds={"USD": "1"},
        margin_headroom="1",
        capability_allowed=True,
        borrow_available=None,
        stress_scenarios=({"ABC": "0.10"},),
    )


def locate(*, available="50", revision="locate-r1"):
    return BorrowLocateEvidence(
        resource=resource(),
        locate_id="locate-1",
        provider_revision=revision,
        availability_state="AVAILABLE",
        available_quantity=available,
        observed_at="2026-09-25T05:00:20Z",
        effective_at="2026-09-25T05:00:00Z",
        valid_until="2026-09-25T05:04:00Z",
        evidence_refs=(f"provider:{revision}",),
    )


def loan(*, borrowed="40", revision="loan-r1"):
    return BorrowLoanEvidence(
        resource=resource(),
        provider_revision=revision,
        borrowed_quantity=borrowed,
        observed_at="2026-09-25T05:00:20Z",
        effective_at="2026-09-25T05:00:00Z",
        valid_until="2026-09-25T05:04:00Z",
        evidence_refs=(f"provider:{revision}",),
    )


def recall(*, quantity="3", revision="recall-r1"):
    return BorrowRecallEvidence(
        resource=resource(),
        recall_id="recall-1",
        provider_revision=revision,
        recalled_quantity=quantity,
        observed_at="2026-09-25T05:00:40Z",
        effective_at="2026-09-25T05:00:30Z",
        deadline="2026-09-25T06:00:00Z",
        evidence_refs=(f"provider:{revision}",),
    )


def resolution(*, quantity="1", revision="resolve-r1"):
    return BorrowRecallResolutionEvidence(
        resource=resource(),
        recall_id="recall-1",
        provider_revision=revision,
        resolved_quantity=quantity,
        observed_at="2026-09-25T05:01:10Z",
        evidence_refs=(f"provider:{revision}",),
    )


def snapshot():
    return SnapshotConsistencyEvidence(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        mode="ATOMIC",
        query_started_at="2026-09-25T05:00:00Z",
        query_completed_at="2026-09-25T05:00:30Z",
    )


def cash_availability():
    return ResourceAvailabilityEvidence(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        snapshot_id="cash-cut-1",
        query_started_at="2026-09-25T05:00:00Z",
        query_completed_at="2026-09-25T05:00:30Z",
        valid_until="2026-09-25T05:04:00Z",
        available_resources={"CASH:USD": "10000"},
        evidence_refs=("provider:cash-cut-1",),
    )


def account_reconciliation(borrow_evidence):
    return reconcile_account(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        local_cash={"USD": "10000"},
        provider_cash={"USD": "10000"},
        local_positions={"ABC": "-40"},
        provider_positions={"ABC": "-40"},
        local_execution_ids=(),
        provider_fills=(),
        snapshot_consistency=snapshot(),
        coverage_start="2026-09-25T05:00:00Z",
        coverage_end="2026-09-25T05:02:00Z",
        pagination_complete=True,
        resource_availability=cash_availability(),
        borrow_reconciliations=(borrow_evidence,),
    )


class BorrowReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = f"{self.temp.name}/journal.sqlite3"
        self.store = JournalStore(self.path)
        self.journal = BorrowLifecycleJournal(self.store, resource())

    def tearDown(self):
        self.temp.cleanup()

    def record_clean_provider_truth(self, *, available="50", borrowed="40"):
        self.journal.record_locate(locate(available=available))
        self.journal.record_loan(loan(borrowed=borrowed))

    def test_matching_filled_short_and_reserved_capacity_reconcile_cleanly(self):
        self.record_clean_provider_truth(available="30", borrowed="40")
        evidence = self.journal.reconciliation_evidence(
            context(reserved="-30"),
            symbol="ABC",
            now=NOW,
        )
        self.assertEqual(evidence.local_short_quantity, Decimal("40"))
        self.assertEqual(evidence.reserved_short_quantity, Decimal("30"))
        self.assertEqual(evidence.provider_borrowed_quantity, Decimal("40"))
        self.assertEqual(evidence.locate_available_quantity, Decimal("30"))
        self.assertEqual(evidence.loan_difference, Decimal("0"))
        self.assertFalse(evidence.blocks_new_risk)

    def test_working_unknown_short_above_locate_capacity_blocks_resource(self):
        self.record_clean_provider_truth(available="25", borrowed="40")
        evidence = self.journal.reconciliation_evidence(
            context(reserved="-30"),
            symbol="ABC",
            now=NOW,
        )
        self.assertTrue(evidence.blocks_new_risk)
        self.assertIn(
            "reserved short exposure exceeds fresh locate capacity",
            evidence.blocking_reasons,
        )

    def test_provider_loan_mismatch_is_scoped_and_durable_in_wp20_checkpoint(self):
        self.record_clean_provider_truth(available="50", borrowed="39")
        evidence = self.journal.reconciliation_evidence(
            context(),
            symbol="ABC",
            now=NOW,
        )
        self.assertEqual(evidence.loan_difference, Decimal("-1"))
        self.assertTrue(evidence.blocks_new_risk)

        result = account_reconciliation(evidence)
        self.assertTrue(result.complete)
        self.assertIn(resource().resource_key, result.blocking_resources)
        self.assertNotIn("ACCOUNT", result.blocking_resources)

        checkpoint = record_reconciliation_checkpoint(
            self.store,
            reconciliation_id="borrow-mismatch",
            result=result,
            observed_at=NOW,
            host_id="borrow-host",
            owner_epoch="1",
        )
        durable = checkpoint["payload"]["borrow_reconciliations"]
        self.assertEqual(len(durable), 1)
        self.assertEqual(durable[0]["loan_difference"], "-1")
        self.assertEqual(
            durable[0]["resource"]["resource_key"],
            resource().resource_key,
        )

        # The scoped BORROW mismatch must not disable cash needed to cover.
        cash = load_account_resource_availability_evidence(
            self.store,
            checkpoint_event_id=checkpoint["event_id"],
            provider_id=PROVIDER_ID,
            account_id=ACCOUNT_ID,
            environment=ENVIRONMENT,
            resources=("CASH:USD",),
            now=NOW,
            max_age_seconds="60",
        )
        self.assertEqual(cash["availability"]["CASH:USD"], "10000")

    def test_active_recall_survives_restart_and_projects_protection_state(self):
        self.record_clean_provider_truth()
        self.journal.record_recall(recall())

        restarted = BorrowLifecycleJournal(
            JournalStore(self.path),
            resource(),
        )
        evidence = restarted.reconciliation_evidence(
            context(),
            symbol="ABC",
            now=NOW,
        )
        self.assertEqual(evidence.active_recall_quantity, Decimal("3"))
        self.assertTrue(evidence.blocks_new_risk)
        self.assertIn(
            "active provider borrow recall requires protection",
            evidence.blocking_reasons,
        )

        equity = EquityState.create(
            symbol="ABC",
            quantity="-40",
            total_basis="400",
            settled_cash="10000",
            currency="USD",
            borrowed_quantity="40",
        )
        protected = restarted.project_recall_to_equity_state(equity)
        self.assertEqual(protected.recalled_quantity, Decimal("3"))

        # No ACK/timeout/attempted-close method exists on the borrow journal;
        # a restart therefore retains the provider obligation unchanged.
        restarted_again = BorrowLifecycleJournal(
            JournalStore(self.path),
            resource(),
        )
        self.assertEqual(
            restarted_again.state().active_recall_quantity,
            Decimal("3"),
        )

    def test_partial_provider_resolution_reduces_but_does_not_erase_recall(self):
        self.record_clean_provider_truth()
        self.journal.record_recall(recall())
        self.journal.record_recall_resolution(resolution(quantity="1"))

        restarted = BorrowLifecycleJournal(
            JournalStore(self.path),
            resource(),
        )
        self.assertEqual(
            restarted.state().active_recall_quantity,
            Decimal("2"),
        )
        equity = EquityState.create(
            symbol="ABC",
            quantity="-40",
            total_basis="400",
            settled_cash="10000",
            currency="USD",
            borrowed_quantity="40",
        )
        self.assertEqual(
            restarted.project_recall_to_equity_state(equity).recalled_quantity,
            Decimal("2"),
        )

    def test_reconciliation_rejects_cross_account_borrow_evidence(self):
        self.record_clean_provider_truth()
        evidence = self.journal.reconciliation_evidence(
            context(),
            symbol="ABC",
            now=NOW,
        )
        other = BorrowResourceIdentity(
            provider_id=PROVIDER_ID,
            account_id="other-account",
            environment=ENVIRONMENT,
            instrument_id=INSTRUMENT_ID,
            instrument_version=1,
        )
        from dataclasses import replace

        with self.assertRaisesRegex(ValueError, "scope mismatch"):
            reconcile_account(
                provider_id=PROVIDER_ID,
                account_id=ACCOUNT_ID,
                environment=ENVIRONMENT,
                local_cash={"USD": "10000"},
                provider_cash={"USD": "10000"},
                local_positions={"ABC": "-40"},
                provider_positions={"ABC": "-40"},
                local_execution_ids=(),
                provider_fills=(),
                snapshot_consistency=snapshot(),
                coverage_start="2026-09-25T05:00:00Z",
                coverage_end="2026-09-25T05:02:00Z",
                pagination_complete=True,
                resource_availability=cash_availability(),
                borrow_reconciliations=(
                    replace(evidence, resource=other),
                ),
            )


if __name__ == "__main__":
    unittest.main()
