from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import unittest

from mvp.autotrade_mvp.reconciliation import (
    CoverageSurfaceEvidence,
    ProviderActivityEvidence,
    ProviderWorkingOrderEvidence,
    ResourceAvailabilityEvidence,
    SnapshotConsistencyEvidence,
    UnknownSubmission,
    reconcile_account,
)


def working(
    provider_order_id,
    client_order_id,
    *,
    instrument="ABC",
    remaining_quantity=Decimal("1"),
):
    return ProviderWorkingOrderEvidence.create(
        provider_id="TEST_PROVIDER",
        account_id="test-account",
        environment="PAPER",
        provider_order_id=provider_order_id,
        client_order_id=client_order_id,
        instrument=instrument,
        remaining_quantity=remaining_quantity,
    )


def activity(
    activity_id,
    *,
    origin="AUTOTRADE",
    currency=None,
    instrument=None,
):
    return ProviderActivityEvidence.create(
        provider_id="TEST_PROVIDER",
        account_id="test-account",
        environment="PAPER",
        activity_id=activity_id,
        activity_type="CASH" if currency is not None else "ORDER",
        origin=origin,
        occurred_at="2026-09-24T18:00:00Z",
        currency=currency,
        instrument=instrument,
    )


def unknown(attempt_id, client_order_id, *, intent_id=None):
    return UnknownSubmission.create(
        attempt_id=attempt_id,
        intent_id=intent_id or f"intent-{attempt_id}",
        provider_id="TEST_PROVIDER",
        account_id="test-account",
        environment="PAPER",
        client_order_id=client_order_id,
        started_at="2026-09-24T16:30:00Z",
    )


class ReconciliationAuthoritySnapshotAndExactnessTests(unittest.TestCase):
    def base(self, **overrides):
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
            coverage_start="2026-09-24T16:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=True,
        )
        values.update(overrides)
        return reconcile_account(**values)

    def test_polymorphic_text_is_rejected_before_virtual_dispatch(self):
        class HostileText(str):
            def strip(self):
                raise AssertionError("hostile strip must not run")

            def upper(self):
                raise AssertionError("hostile upper must not run")

        with self.assertRaisesRegex(ValueError, "provider_id is required"):
            self.base(provider_id=HostileText("TEST_PROVIDER"))

        order = working("provider-order-1", "client-order-1")
        object.__setattr__(order, "provider_order_id", HostileText("provider-order-1"))
        with self.assertRaisesRegex(TypeError, "provider_order_id must be exact str"):
            self.base(provider_working_orders=(order,))

    def test_singleton_authority_is_frozen_before_sequence_callbacks(self):
        snapshot = SnapshotConsistencyEvidence(
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            mode="ATOMIC",
            query_started_at="2026-09-24T17:00:00Z",
            query_completed_at="2026-09-24T19:00:00Z",
        )
        availability = ResourceAvailabilityEvidence(
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            snapshot_id="snapshot-1",
            query_started_at="2026-09-24T17:00:00Z",
            query_completed_at="2026-09-24T19:00:00Z",
            valid_until="2026-09-24T19:05:00Z",
            available_resources={"CASH:USD": Decimal("850")},
            evidence_refs=("provider:snapshot-1",),
        )
        first = working("provider-order-1", "client-order-1")
        second = working("provider-order-2", "client-order-2")

        class MutatingSequence:
            def __len__(self):
                return 2

            def __getitem__(self, index):
                if index == 0:
                    return first
                if index == 1:
                    object.__setattr__(
                        snapshot,
                        "query_started_at",
                        "2026-09-24T20:00:00Z",
                    )
                    object.__setattr__(snapshot, "sequence_gap_detected", True)
                    object.__setattr__(availability, "snapshot_id", "mutated")
                    object.__setattr__(
                        availability,
                        "available_resources",
                        {"CASH:USD": Decimal("999999")},
                    )
                    return second
                raise IndexError

        result = self.base(
            local_working_client_order_ids=("client-order-1", "client-order-2"),
            provider_working_orders=MutatingSequence(),
            snapshot_consistency=snapshot,
            resource_availability=availability,
        )

        self.assertTrue(snapshot.sequence_gap_detected)
        self.assertEqual(availability.snapshot_id, "mutated")
        self.assertTrue(result.snapshot_consistent)
        self.assertEqual(
            result.snapshot_query_started_at,
            "2026-09-24T17:00:00Z",
        )
        self.assertIsNot(result.resource_availability, availability)
        self.assertEqual(result.resource_availability.snapshot_id, "snapshot-1")
        self.assertEqual(
            result.resource_availability.available_resources["CASH:USD"],
            Decimal("850"),
        )

    def test_absence_coverage_sequence_mutation_cannot_rewrite_proven_absence(self):
        evidence = [
            CoverageSurfaceEvidence(
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                surface=surface,
                coverage_start="2026-09-24T16:00:00Z",
                coverage_end="2026-09-24T19:00:00Z",
                pagination_complete=True,
                consistency_horizon_satisfied=True,
                provider_semantics_exclude_execution=True,
            )
            for surface in (
                "OPEN_ORDERS",
                "ORDER_HISTORY",
                "EXECUTIONS",
                "ACTIVITIES",
            )
        ]

        class MutatingCoverageSequence:
            def __len__(self):
                return 4

            def __getitem__(self, index):
                if index == 0:
                    return evidence[0]
                if index == 1:
                    object.__setattr__(
                        evidence[0],
                        "provider_semantics_exclude_execution",
                        False,
                    )
                if 0 <= index < 4:
                    return evidence[index]
                raise IndexError

        result = self.base(
            unknown_submissions=(unknown("attempt-absence", "missing-order"),),
            searched_client_order_ids=("missing-order",),
            absence_coverage=MutatingCoverageSequence(),
        )

        self.assertFalse(evidence[0].provider_semantics_exclude_execution)
        self.assertEqual(
            result.submission_resolutions[0].outcome,
            "PROVEN_ABSENT",
        )
        self.assertTrue(result.complete)

    def test_working_order_sequence_mutation_cannot_rewrite_unknown_resolution(self):
        first = working("provider-order-1", "client-order-1")
        second = working("provider-order-2", "client-order-2")

        class MutatingSequence:
            def __len__(self):
                return 2

            def __getitem__(self, index):
                if index == 0:
                    return first
                if index == 1:
                    object.__setattr__(first, "client_order_id", "mutated-client")
                    object.__setattr__(first, "provider_order_id", "mutated-provider")
                    object.__setattr__(first, "instrument", "MUTATED")
                    return second
                raise IndexError

        result = self.base(
            local_working_client_order_ids=("client-order-1", "client-order-2"),
            provider_working_orders=MutatingSequence(),
            unknown_submissions=(unknown("attempt-1", "client-order-1"),),
        )

        self.assertEqual(first.client_order_id, "mutated-client")
        self.assertEqual(result.matched_working_client_order_ids, (
            "client-order-1",
            "client-order-2",
        ))
        self.assertEqual(result.submission_resolutions[0].outcome, "OBSERVED_WORKING_ORDER")
        self.assertEqual(
            result.submission_resolutions[0].provider_order_ids,
            ("provider-order-1",),
        )

    def test_provider_activity_sequence_mutation_cannot_rewrite_blocking_scope(self):
        first = activity("activity-manual", origin="MANUAL", currency="USD")
        second = activity("activity-local", origin="AUTOTRADE", instrument="ABC")

        class MutatingSequence:
            def __len__(self):
                return 2

            def __getitem__(self, index):
                if index == 0:
                    return first
                if index == 1:
                    object.__setattr__(first, "origin", "AUTOTRADE")
                    object.__setattr__(first, "currency", "EUR")
                    return second
                raise IndexError

        result = self.base(
            local_provider_activity_ids=("activity-local",),
            provider_activities=MutatingSequence(),
        )

        self.assertEqual(first.origin, "AUTOTRADE")
        self.assertEqual(first.currency, "EUR")
        self.assertEqual(
            result.unexpected_provider_activity_ids,
            ("activity-manual",),
        )
        self.assertEqual(
            result.manual_or_external_activity_ids,
            ("activity-manual",),
        )
        self.assertIn("CASH:USD", result.blocking_resources)
        self.assertNotIn("CASH:EUR", result.blocking_resources)
        self.assertFalse(result.complete)

    def test_unknown_submission_sequence_mutation_cannot_rewrite_resolution_identity(self):
        first = unknown("attempt-1", "client-order-1")
        second = unknown("attempt-2", "client-order-2")

        class MutatingSequence:
            def __len__(self):
                return 2

            def __getitem__(self, index):
                if index == 0:
                    return first
                if index == 1:
                    object.__setattr__(first, "attempt_id", "mutated-attempt")
                    object.__setattr__(first, "intent_id", "mutated-intent")
                    object.__setattr__(first, "client_order_id", "mutated-client")
                    return second
                raise IndexError

        result = self.base(
            local_working_client_order_ids=("client-order-1", "client-order-2"),
            provider_working_orders=(
                working("provider-order-1", "client-order-1"),
                working("provider-order-2", "client-order-2"),
            ),
            unknown_submissions=MutatingSequence(),
        )

        self.assertEqual(first.attempt_id, "mutated-attempt")
        self.assertEqual(
            tuple(item.attempt_id for item in result.submission_resolutions),
            ("attempt-1", "attempt-2"),
        )
        self.assertEqual(
            tuple(item.client_order_id for item in result.submission_resolutions),
            ("client-order-1", "client-order-2"),
        )
        self.assertEqual(
            tuple(item.provider_order_ids for item in result.submission_resolutions),
            (("provider-order-1",), ("provider-order-2",)),
        )

    def test_adjacent_authority_snapshots_reject_subclass_and_hidden_state(self):
        canonical = working("provider-order-1", "client-order-1")

        class WorkingSubclass(ProviderWorkingOrderEvidence):
            pass

        subclass = WorkingSubclass(
            provider_id=canonical.provider_id,
            account_id=canonical.account_id,
            environment=canonical.environment,
            provider_order_id=canonical.provider_order_id,
            client_order_id=canonical.client_order_id,
            instrument=canonical.instrument,
            remaining_quantity=canonical.remaining_quantity,
        )
        with self.assertRaisesRegex(TypeError, "exact ProviderWorkingOrderEvidence"):
            self.base(provider_working_orders=(subclass,))

        injected_activity = activity(
            "activity-injected",
            origin="MANUAL",
            currency="USD",
        )
        object.__setattr__(
            injected_activity,
            "unreviewed_state",
            "must-not-enter-authority",
        )
        with self.assertRaisesRegex(TypeError, "unexpected state fields"):
            self.base(provider_activities=(injected_activity,))

        injected_submission = unknown("attempt-injected", "client-injected")
        object.__setattr__(
            injected_submission,
            "unreviewed_state",
            "must-not-enter-authority",
        )
        with self.assertRaisesRegex(TypeError, "unexpected state fields"):
            self.base(unknown_submissions=(injected_submission,))

    def test_cash_and_position_verdicts_are_independent_of_decimal_context(self):
        expected_cash = Decimal("1.23456789")
        expected_position = Decimal("1.23456544")
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        result = self.base(
                            local_cash={"USD": Decimal("0")},
                            provider_cash={"USD": Decimal("1.23456789")},
                            cash_tolerance={"USD": Decimal("1.234565")},
                            local_positions={"ABC": Decimal("8.64197777")},
                            provider_positions={"ABC": Decimal("9.87654321")},
                            position_tolerance={"ABC": Decimal("1.234565")},
                        )
                    self.assertEqual(
                        result.cash_differences["USD"],
                        expected_cash,
                    )
                    self.assertEqual(
                        result.position_differences["ABC"],
                        expected_position,
                    )
                    self.assertIn("CASH:USD", result.blocking_resources)
                    self.assertIn("INSTRUMENT:ABC", result.blocking_resources)
                    self.assertFalse(result.complete)

    def test_settlement_verdict_is_independent_of_decimal_context(self):
        expected = Decimal("1.23456544")
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        result = self.base(
                            local_settled_cash={"USD": Decimal("8.64197777")},
                            provider_settled_cash={"USD": Decimal("9.87654321")},
                            local_unsettled_receivable={"USD": Decimal("0")},
                            provider_unsettled_receivable={"USD": Decimal("0")},
                            local_unsettled_payable={"USD": Decimal("0")},
                            provider_unsettled_payable={"USD": Decimal("0")},
                            settlement_activity_complete=True,
                        )
                    self.assertEqual(
                        result.settlement_differences["SETTLED:USD"],
                        expected,
                    )
                    self.assertIn("CASH:USD", result.blocking_resources)
                    self.assertFalse(result.complete)


if __name__ == "__main__":
    unittest.main()
