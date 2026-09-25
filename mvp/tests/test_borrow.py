from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.borrow import (
    BorrowLifecycleJournal,
    BorrowLocateEvidence,
    BorrowRecallEvidence,
    BorrowRecallResolutionEvidence,
    BorrowResourceIdentity,
    borrow_reservation_requirement,
    incremental_short_borrow_quantity,
    locate_capacity,
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
