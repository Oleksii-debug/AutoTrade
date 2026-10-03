from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import unittest

from mvp.autotrade_mvp.reconciliation import (
    ResourceAvailabilityEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.securities_borrow import BorrowAvailabilityEvidence


class ReconciliationBorrowCompletionGateTests(unittest.TestCase):
    def _borrow_evidence(self) -> BorrowAvailabilityEvidence:
        return BorrowAvailabilityEvidence(
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            instrument_id="11111111-1111-4111-8111-111111111111",
            instrument_version=1,
            locate_id="locate-1",
            provider_revision="revision-1",
            capacity_quantity=Decimal("10"),
            hard_to_borrow=False,
            observed_at="2026-09-24T18:00:00Z",
            effective_at="2026-09-24T17:59:00Z",
            expires_at="2026-09-24T20:00:00Z",
            evidence_ref=(
                "artifact:22222222-2222-4222-8222-222222222222@sha256:"
                + "a" * 64
            ),
        )

    def _base(self, **overrides):
        borrow = self._borrow_evidence()
        resource = borrow.resource_key
        availability = ResourceAvailabilityEvidence(
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            snapshot_id="snapshot-borrow-1",
            query_started_at="2026-09-24T17:00:00Z",
            query_completed_at="2026-09-24T19:00:00Z",
            provider_as_of="2026-09-24T18:59:59Z",
            valid_until="2026-09-24T19:05:00Z",
            available_resources={resource: Decimal("10")},
            resource_details={resource: borrow.resource_detail()},
            evidence_refs=("provider:snapshot-borrow-1",),
        )
        values = dict(
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            local_cash={},
            provider_cash={},
            local_positions={},
            provider_positions={},
            local_execution_ids=(),
            provider_fills=(),
            snapshot_consistency=SnapshotConsistencyEvidence(
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                mode="ATOMIC",
                query_started_at="2026-09-24T17:00:00Z",
                query_completed_at="2026-09-24T19:00:00Z",
            ),
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=True,
            resource_availability=availability,
        )
        values.update(overrides)
        return resource, reconcile_account(**values)

    def test_borrow_obligation_difference_makes_reconciliation_incomplete(self):
        resource, result = self._base(
            local_borrowed_resources={
                self._borrow_evidence().resource_key: Decimal("3")
            },
            provider_borrowed_resources={
                self._borrow_evidence().resource_key: Decimal("2")
            },
        )

        self.assertEqual(result.borrow_differences[resource], Decimal("-1"))
        self.assertIn(resource, result.blocking_resources)
        self.assertTrue(result.blocks_new_risk)
        self.assertFalse(result.complete)

    def test_borrow_difference_is_independent_of_decimal_context(self):
        resource = self._borrow_evidence().resource_key
        expected = Decimal("1.23456544")
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        _, result = self._base(
                            local_borrowed_resources={
                                resource: Decimal("8.64197777")
                            },
                            provider_borrowed_resources={
                                resource: Decimal("9.87654321")
                            },
                        )
                    self.assertEqual(
                        result.borrow_differences[resource],
                        expected,
                    )
                    self.assertIn(resource, result.blocking_resources)
                    self.assertFalse(result.complete)

    def test_matching_borrow_without_recall_remains_complete(self):
        resource = self._borrow_evidence().resource_key
        _, result = self._base(
            local_borrowed_resources={resource: Decimal("2")},
            provider_borrowed_resources={resource: Decimal("2")},
        )

        self.assertEqual(dict(result.borrow_differences), {})
        self.assertNotIn(resource, result.blocking_resources)
        self.assertFalse(result.blocks_new_risk)
        self.assertTrue(result.complete)

    def test_active_borrow_recall_makes_reconciliation_incomplete(self):
        resource = self._borrow_evidence().resource_key
        _, result = self._base(
            local_borrowed_resources={resource: Decimal("2")},
            provider_borrowed_resources={resource: Decimal("2")},
            active_borrow_recall_resources=(resource,),
        )

        self.assertEqual(dict(result.borrow_differences), {})
        self.assertIn(resource, result.blocking_resources)
        self.assertTrue(result.blocks_new_risk)
        self.assertFalse(result.complete)


if __name__ == "__main__":
    unittest.main()
