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
    borrow_reservation_requirement,
    incremental_short_borrow_quantity,
    locate_capacity,
    validate_borrow_account_truth,
    validated_borrow_capacity,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.risk import RiskContext, RiskIntent


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"


def resource(**overrides):
    values = dict(
        provider_id="TEST_PROVIDER",
        account_id="paper-1",
        environment="PAPER",
        instrument_id=INSTRUMENT_ID,
        instrument_version=3,
    )
    values.update(overrides)
    return BorrowResourceIdentity(**values)


def locate(scope=None, **overrides):
    values = dict(
        resource=scope or resource(),
        locate_id="locate-1",
        provider_revision="rev-1",
        availability_state="AVAILABLE",
        available_quantity="100",
        observed_at="2026-09-24T18:00:00Z",
        effective_at="2026-09-24T17:59:00Z",
        valid_until="2026-09-24T18:05:00Z",
        evidence_refs=("provider:locate-1:rev-1",),
    )
    values.update(overrides)
    return BorrowLocateEvidence(**values)


def loan(scope=None, **overrides):
    values = dict(
        resource=scope or resource(),
        provider_revision="loan-rev-1",
        borrowed_quantity="0",
        observed_at="2026-09-24T18:00:00Z",
        effective_at="2026-09-24T17:59:00Z",
        valid_until="2026-09-24T18:05:00Z",
        evidence_refs=("provider:loan-rev-1",),
    )
    values.update(overrides)
    return BorrowLoanEvidence(**values)


def context(*, position="0", reserved="0"):
    return RiskContext.create(
        state_version=1,
        equity="10000",
        positions={"ABC": position},
        marks={"ABC": "100"},
        reserved_position_delta=(
            {} if Decimal(reserved) == 0 else {"ABC": reserved}
        ),
        daily_pnl="0",
        drawdown_fraction="0",
        market_data_age_seconds="1",
        fx_age_seconds={"USD": "1"},
        margin_headroom="1",
        capability_allowed=True,
        borrow_available=True,
        stress_scenarios=({"ABC": "-0.10"},),
    )


def intent(*, side="SELL", quantity="1", instrument_type="EQUITY", reduce_only=False):
    return RiskIntent.create(
        symbol="ABC",
        side=side,
        quantity=quantity,
        price="100",
        expected_state_version=1,
        instrument_type=instrument_type,
        reduce_only=reduce_only,
        action="REDUCE" if reduce_only else "TRADE",
    )


class BorrowResourceTests(unittest.TestCase):
    def test_resource_identity_is_scope_bound_and_canonical(self):
        one = resource()
        same = resource(provider_id="test_provider")
        other_account = resource(account_id="paper-2")
        other_version = resource(instrument_version=4)
        self.assertEqual(one.resource_key, same.resource_key)
        self.assertTrue(one.resource_key.startswith("BORROW:sha256:"))
        self.assertNotEqual(one.resource_key, other_account.resource_key)
        self.assertNotEqual(one.resource_key, other_version.resource_key)

    def test_short_increment_accounts_for_existing_and_reserved_short(self):
        self.assertEqual(
            incremental_short_borrow_quantity(
                intent(quantity="20"),
                context(position="-40", reserved="-30"),
            ),
            Decimal("20"),
        )
        self.assertEqual(
            incremental_short_borrow_quantity(
                intent(side="BUY", quantity="10", reduce_only=True),
                context(position="-40"),
            ),
            Decimal("0"),
        )
        self.assertEqual(
            incremental_short_borrow_quantity(
                intent(quantity="5", instrument_type="FUTURE"),
                context(),
            ),
            Decimal("0"),
        )

    def test_crossing_long_to_short_reserves_only_resulting_short(self):
        requirement = borrow_reservation_requirement(
            resource(),
            intent(quantity="15"),
            context(position="10"),
        )
        self.assertEqual(
            requirement,
            {resource().resource_key: "5"},
        )

    def test_provider_loan_truth_must_match_current_filled_short(self):
        scope = resource()
        with TemporaryDirectory() as directory:
            journal = BorrowLifecycleJournal(
                JournalStore(f"{directory}/journal.sqlite3"),
                scope,
            )
            journal.record_loan(loan(scope, borrowed_quantity="40"))
            projected = journal.state()
            self.assertEqual(
                validate_borrow_account_truth(
                    projected,
                    context(position="-40", reserved="-30"),
                    symbol="ABC",
                    now="2026-09-24T18:01:00Z",
                ),
                Decimal("40"),
            )
            with self.assertRaisesRegex(ValueError, "does not match"):
                validate_borrow_account_truth(
                    projected,
                    context(position="-41", reserved="-30"),
                    symbol="ABC",
                    now="2026-09-24T18:01:00Z",
                )

    def test_existing_short_requires_fresh_provider_loan_truth(self):
        scope = resource()
        with TemporaryDirectory() as directory:
            journal = BorrowLifecycleJournal(
                JournalStore(f"{directory}/journal.sqlite3"),
                scope,
            )
            with self.assertRaisesRegex(ValueError, "required"):
                validate_borrow_account_truth(
                    journal.state(),
                    context(position="-1"),
                    symbol="ABC",
                    now="2026-09-24T18:01:00Z",
                )
            journal.record_loan(
                loan(
                    scope,
                    borrowed_quantity="1",
                    valid_until="2026-09-24T18:00:59Z",
                )
            )
            with self.assertRaisesRegex(ValueError, "expired"):
                validate_borrow_account_truth(
                    journal.state(),
                    context(position="-1"),
                    symbol="ABC",
                    now="2026-09-24T18:01:00Z",
                )

    def test_locate_capacity_is_fresh_exact_and_recall_adjusted(self):
        evidence = locate(available_quantity="100.25")
        self.assertEqual(
            locate_capacity(
                evidence,
                now="2026-09-24T18:01:00Z",
                active_recall_quantity="30.25",
            ),
            {evidence.resource.resource_key: "70"},
        )
        with self.assertRaisesRegex(ValueError, "expired"):
            locate_capacity(evidence, now="2026-09-24T18:05:00Z")
        with self.assertRaisesRegex(ValueError, "zero"):
            locate(availability_state="UNAVAILABLE", available_quantity="1")


class BorrowLifecycleJournalTests(unittest.TestCase):
    def test_recall_is_restart_safe_and_blocks_new_short(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            scope = resource()
            journal = BorrowLifecycleJournal(store, scope)
            journal.record_locate(locate(scope))
            journal.record_loan(loan(scope, borrowed_quantity="25"))
            journal.record_recall(
                BorrowRecallEvidence(
                    resource=scope,
                    recall_id="recall-1",
                    provider_revision="recall-rev-1",
                    recalled_quantity="25",
                    observed_at="2026-09-24T18:01:00Z",
                    effective_at="2026-09-24T18:00:30Z",
                    deadline="2026-09-24T20:00:00Z",
                    evidence_refs=("provider:recall-1:rev-1",),
                )
            )
            self.assertTrue(journal.state().blocks_new_short)
            self.assertEqual(journal.state().active_recall_quantity, Decimal("25"))

            reopened = BorrowLifecycleJournal(JournalStore(path), scope)
            state = reopened.state()
            self.assertTrue(state.blocks_new_short)
            self.assertEqual(state.active_recall_quantity, Decimal("25"))
            self.assertEqual(state.latest_locate.available_quantity, Decimal("100"))
            self.assertEqual(state.latest_loan.borrowed_quantity, Decimal("25"))

    def test_validated_capacity_blocks_recall_and_loan_mismatch(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            scope = resource()
            journal = BorrowLifecycleJournal(store, scope)
            journal.record_locate(locate(scope, available_quantity="50"))
            journal.record_loan(loan(scope, borrowed_quantity="10"))
            self.assertEqual(
                validated_borrow_capacity(
                    journal.state(),
                    context(position="-10", reserved="-5"),
                    symbol="ABC",
                    now="2026-09-24T18:01:00Z",
                ),
                {scope.resource_key: "50"},
            )

            journal.record_recall(
                BorrowRecallEvidence(
                    resource=scope,
                    recall_id="recall-block",
                    provider_revision="recall-block-rev-1",
                    recalled_quantity="2",
                    observed_at="2026-09-24T18:01:30Z",
                    effective_at="2026-09-24T18:01:00Z",
                    evidence_refs=("provider:recall-block:rev-1",),
                )
            )
            with self.assertRaisesRegex(ValueError, "recall"):
                validated_borrow_capacity(
                    journal.state(),
                    context(position="-10"),
                    symbol="ABC",
                    now="2026-09-24T18:02:00Z",
                )

    def test_authority_snapshot_replays_exact_cut_while_later_recall_blocks_current_send(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            scope = resource()
            journal = BorrowLifecycleJournal(store, scope)
            journal.record_locate(locate(scope, available_quantity="20"))
            journal.record_loan(loan(scope, borrowed_quantity="5"))

            bound = journal.authority_snapshot(
                context(position="-5", reserved="-2"),
                symbol="ABC",
                now="2026-09-24T18:01:00Z",
            )
            self.assertEqual(
                bound["availability"],
                {scope.resource_key: "20"},
            )
            self.assertEqual(
                journal.validate_authority_snapshot(
                    bound,
                    now="2026-09-24T18:01:00Z",
                ),
                {scope.resource_key: "20"},
            )

            journal.record_recall(
                BorrowRecallEvidence(
                    resource=scope,
                    recall_id="recall-after-admission",
                    provider_revision="recall-after-rev-1",
                    recalled_quantity="1",
                    observed_at="2026-09-24T18:01:30Z",
                    effective_at="2026-09-24T18:01:15Z",
                    evidence_refs=("provider:recall-after:rev-1",),
                )
            )
            self.assertTrue(journal.current_blocks_new_short())
            # Historical admission evidence remains reproducible at its cut;
            # the later recall is a dispatch-time fence, not a rewrite of history.
            self.assertEqual(
                journal.validate_authority_snapshot(
                    bound,
                    now="2026-09-24T18:01:00Z",
                ),
                {scope.resource_key: "20"},
            )

    def test_authority_snapshot_rejects_tampered_cut(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            scope = resource()
            journal = BorrowLifecycleJournal(store, scope)
            journal.record_locate(locate(scope, available_quantity="20"))
            bound = journal.authority_snapshot(
                context(),
                symbol="ABC",
                now="2026-09-24T18:01:00Z",
            )
            tampered = dict(bound)
            tampered["availability"] = {scope.resource_key: "200"}
            with self.assertRaisesRegex(ValueError, "capacity mismatch"):
                journal.validate_authority_snapshot(
                    tampered,
                    now="2026-09-24T18:01:00Z",
                )

    def test_partial_resolution_requires_affirmative_provider_evidence(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            scope = resource()
            journal = BorrowLifecycleJournal(store, scope)
            journal.record_recall(
                BorrowRecallEvidence(
                    resource=scope,
                    recall_id="recall-1",
                    provider_revision="recall-rev-1",
                    recalled_quantity="30",
                    observed_at="2026-09-24T18:01:00Z",
                    effective_at="2026-09-24T18:00:30Z",
                    evidence_refs=("provider:recall-1:rev-1",),
                )
            )
            journal.record_recall_resolution(
                BorrowRecallResolutionEvidence(
                    resource=scope,
                    recall_id="recall-1",
                    provider_revision="resolution-rev-1",
                    resolved_quantity="10",
                    observed_at="2026-09-24T18:02:00Z",
                    evidence_refs=("provider:recall-1:resolution-rev-1",),
                )
            )
            self.assertEqual(journal.state().active_recall_quantity, Decimal("20"))
            with self.assertRaisesRegex(ValueError, "exceeds"):
                journal.record_recall_resolution(
                    BorrowRecallResolutionEvidence(
                        resource=scope,
                        recall_id="recall-1",
                        provider_revision="resolution-rev-2",
                        resolved_quantity="21",
                        observed_at="2026-09-24T18:03:00Z",
                        evidence_refs=("provider:recall-1:resolution-rev-2",),
                    )
                )

    def test_time_reversed_recall_resolution_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            scope = resource()
            journal = BorrowLifecycleJournal(store, scope)
            journal.record_recall(
                BorrowRecallEvidence(
                    resource=scope,
                    recall_id="recall-time",
                    provider_revision="recall-time-rev-1",
                    recalled_quantity="3",
                    observed_at="2026-09-24T18:02:00Z",
                    effective_at="2026-09-24T18:01:30Z",
                    evidence_refs=("provider:recall-time:rev-1",),
                )
            )
            with self.assertRaisesRegex(ValueError, "predates"):
                journal.record_recall_resolution(
                    BorrowRecallResolutionEvidence(
                        resource=scope,
                        recall_id="recall-time",
                        provider_revision="resolution-time-rev-1",
                        resolved_quantity="1",
                        observed_at="2026-09-24T18:01:59Z",
                        evidence_refs=("provider:recall-time:resolution-rev-1",),
                    )
                )

    def test_conflicting_provider_revision_fails_closed_and_exact_retry_is_idempotent(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            scope = resource()
            journal = BorrowLifecycleJournal(store, scope)
            first = journal.record_locate(locate(scope))
            replay = journal.record_locate(locate(scope))
            self.assertEqual(first["event_id"], replay["event_id"])
            with self.assertRaisesRegex(ValueError, "conflicts"):
                journal.record_locate(
                    locate(scope, available_quantity="99")
                )


if __name__ == "__main__":
    unittest.main()
