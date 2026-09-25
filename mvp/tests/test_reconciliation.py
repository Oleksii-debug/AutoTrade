from decimal import Decimal
import unittest

from mvp.autotrade_mvp.reconciliation import (
    CoverageSurfaceEvidence,
    ProviderActivityEvidence,
    ProviderFillEvidence,
    ProviderWorkingOrderEvidence,
    ResourceAvailabilityEvidence,
    SnapshotConsistencyEvidence,
    UnknownSubmission,
    reconcile_account,
)


def fill(
    execution_id="e1",
    client_order_id="c1",
    instrument="ABC",
    fee_amount="0",
    environment="PAPER",
    trade_time="2026-09-24T18:00:00Z",
):
    return ProviderFillEvidence.create(
        provider_id="TEST_PROVIDER",
        account_id="test-account",
        environment=environment,
        provider_execution_id=execution_id,
        client_order_id=client_order_id,
        instrument=instrument,
        quantity="1",
        price="100",
        fee_amount=fee_amount,
        fee_currency="USD",
        trade_time=trade_time,
    )


def absence_coverage(**overrides):
    values = []
    for surface in ("OPEN_ORDERS", "ORDER_HISTORY", "EXECUTIONS", "ACTIVITIES"):
        item = dict(
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            surface=surface,
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
            provider_semantics_exclude_execution=True,
        )
        if "surface" not in overrides or surface == overrides.get("surface"):
            item.update({key: value for key, value in overrides.items() if key != "surface"})
        values.append(CoverageSurfaceEvidence(**item))
    return values


def resource_availability(**overrides):
    values = dict(
        provider_id="TEST_PROVIDER",
        account_id="test-account",
        environment="PAPER",
        snapshot_id="snapshot-availability-1",
        query_started_at="2026-09-24T17:00:00Z",
        query_completed_at="2026-09-24T19:00:00Z",
        provider_as_of="2026-09-24T18:59:59Z",
        valid_until="2026-09-24T19:05:00Z",
        available_resources={"CASH:USD": "850", "MARGIN:USD": "1200.50"},
        evidence_refs=("provider:snapshot-availability-1",),
    )
    values.update(overrides)
    return ResourceAvailabilityEvidence(**values)


class ReconciliationTests(unittest.TestCase):
    def base(self, **overrides):
        values = dict(
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            local_cash={"USD": "900"},
            provider_cash={"USD": "900"},
            local_positions={"ABC": "1"},
            provider_positions={"ABC": "1"},
            local_execution_ids=["e1"],
            provider_fills=[fill()],
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
            provider_activity_provider_id="TEST_PROVIDER",
            provider_activity_account_id="test-account",
        )
        values.update(overrides)
        return reconcile_account(**values)

    def test_resource_availability_is_bound_to_same_provider_snapshot_cut(self):
        evidence = resource_availability()
        result = self.base(resource_availability=evidence)
        self.assertIs(result.resource_availability, evidence)
        self.assertEqual(
            result.resource_availability.available_resources["CASH:USD"],
            Decimal("850"),
        )
        self.assertEqual(
            result.resource_availability.available_resources["MARGIN:USD"],
            Decimal("1200.50"),
        )

    def test_resource_availability_scope_or_snapshot_cut_mismatch_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "resource availability scope mismatch"):
            self.base(
                resource_availability=resource_availability(account_id="other-account")
            )
        with self.assertRaisesRegex(
            ValueError,
            "snapshot cut differs from reconciliation",
        ):
            self.base(
                resource_availability=resource_availability(
                    query_completed_at="2026-09-24T18:59:00Z",
                )
            )
        with self.assertRaisesRegex(
            ValueError,
            "requires snapshot consistency evidence",
        ):
            self.base(
                snapshot_consistency=None,
                resource_availability=resource_availability(),
            )

    def test_resource_availability_rejects_inflated_or_ambiguous_numeric_shapes(self):
        with self.assertRaises(TypeError):
            resource_availability(available_resources={"CASH:USD": 850.5})
        with self.assertRaisesRegex(ValueError, "non-negative"):
            resource_availability(available_resources={"CASH:USD": "-1"})
        with self.assertRaisesRegex(ValueError, "unique after normalization"):
            resource_availability(
                available_resources={"CASH:USD": "1", " CASH:USD ": "2"}
            )
        with self.assertRaisesRegex(ValueError, "valid_until must be after"):
            resource_availability(valid_until="2026-09-24T19:00:00Z")

    def test_complete_matching_window_reconciles(self):
        result = self.base()
        self.assertTrue(result.complete)
        self.assertFalse(result.blocks_new_risk)
        self.assertEqual(result.matched_execution_ids, ("e1",))

    def test_matching_fill_before_unknown_submission_does_not_resolve_send(self):
        unknown = UnknownSubmission.create(
            attempt_id="a-causal-before",
            intent_id="intent-causal-before",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            client_order_id="c1",
            started_at="2026-09-24T18:30:00Z",
        )
        result = self.base(unknown_submissions=[unknown])
        resolution = result.submission_resolutions[0]
        self.assertEqual(resolution.outcome, "UNKNOWN")
        self.assertEqual(
            resolution.evidence_reason,
            "matching_provider_execution_outside_submission_window",
        )
        self.assertFalse(result.complete)
        self.assertIn("ACCOUNT", result.blocking_resources)

    def test_matching_fill_after_reconciliation_end_does_not_resolve_send(self):
        unknown = UnknownSubmission.create(
            attempt_id="a-causal-after",
            intent_id="intent-causal-after",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            client_order_id="c1",
            started_at="2026-09-24T18:00:00Z",
        )
        result = self.base(
            unknown_submissions=[unknown],
            provider_fills=[fill(trade_time="2026-09-24T19:30:00Z")],
        )
        resolution = result.submission_resolutions[0]
        self.assertEqual(resolution.outcome, "UNKNOWN")
        self.assertEqual(
            resolution.evidence_reason,
            "matching_provider_execution_outside_submission_window",
        )
        self.assertFalse(result.complete)
        self.assertIn("ACCOUNT", result.blocking_resources)

    def test_observed_execution_retains_exact_provider_execution_identity(self):
        unknown = UnknownSubmission.create(
            attempt_id="attempt-execution-id",
            intent_id="intent-execution-id",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            client_order_id="c1",
            started_at="2026-09-24T17:30:00Z",
        )
        result = self.base(unknown_submissions=[unknown])
        resolution = result.submission_resolutions[0]
        self.assertEqual(resolution.outcome, "OBSERVED_EXECUTION")
        self.assertEqual(resolution.provider_execution_ids, ("e1",))
        self.assertEqual(resolution.provider_order_ids, ())

    def test_multiple_causal_fills_retain_deterministic_execution_id_tuple(self):
        unknown = UnknownSubmission.create(
            attempt_id="attempt-multi-execution",
            intent_id="intent-multi-execution",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            client_order_id="c1",
            started_at="2026-09-24T17:30:00Z",
        )
        result = self.base(
            unknown_submissions=[unknown],
            local_execution_ids=["e1", "e2"],
            provider_fills=[
                fill(execution_id="e2", trade_time="2026-09-24T18:10:00Z"),
                fill(execution_id="e1", trade_time="2026-09-24T18:00:00Z"),
            ],
        )
        resolution = result.submission_resolutions[0]
        self.assertEqual(resolution.outcome, "OBSERVED_EXECUTION")
        self.assertEqual(resolution.provider_execution_ids, ("e1", "e2"))
        self.assertEqual(resolution.provider_order_ids, ())

    def test_conflicting_same_unknown_attempt_id_fails_closed_before_resolution(self):
        first = UnknownSubmission.create(
            attempt_id="attempt-conflict",
            intent_id="intent-one",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            client_order_id="missing-one",
            started_at="2026-09-24T18:00:00Z",
        )
        conflicting = UnknownSubmission.create(
            attempt_id="attempt-conflict",
            intent_id="intent-two",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            client_order_id="missing-two",
            started_at="2026-09-24T18:01:00Z",
        )
        with self.assertRaisesRegex(
            ValueError,
            "attempt_id has conflicting observations",
        ):
            self.base(
                unknown_submissions=[first, conflicting],
                searched_client_order_ids=["missing-one", "missing-two"],
                absence_coverage=absence_coverage(),
            )

    def test_distinct_unknown_attempts_cannot_reuse_scoped_client_order_id(self):
        first = UnknownSubmission.create(
            attempt_id="attempt-one",
            intent_id="intent-one",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            client_order_id="shared-missing-order",
            started_at="2026-09-24T18:00:00Z",
        )
        second = UnknownSubmission.create(
            attempt_id="attempt-two",
            intent_id="intent-two",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            client_order_id="shared-missing-order",
            started_at="2026-09-24T18:01:00Z",
        )
        with self.assertRaisesRegex(
            ValueError,
            "client_order_id is reused across attempts",
        ):
            self.base(
                unknown_submissions=[first, second],
                searched_client_order_ids=["shared-missing-order"],
                absence_coverage=absence_coverage(),
            )

    def test_exact_unknown_attempt_replay_is_deduped_before_resolution(self):
        unknown = UnknownSubmission.create(
            attempt_id="attempt-replayed",
            intent_id="intent-replayed",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            client_order_id="missing-replayed",
            started_at="2026-09-24T18:00:00Z",
        )
        result = self.base(
            unknown_submissions=[unknown, unknown],
            searched_client_order_ids=["missing-replayed"],
            absence_coverage=absence_coverage(),
        )
        self.assertEqual(len(result.submission_resolutions), 1)
        self.assertEqual(
            result.submission_resolutions[0].attempt_id,
            "attempt-replayed",
        )

    def test_incomplete_pagination_cannot_prove_unknown_send_absent(self):
        unknown = UnknownSubmission.create(
            attempt_id="a1",
            intent_id="intent-unknown",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
                        client_order_id="missing-order",
            started_at="2026-09-24T18:00:00Z",
        )
        result = self.base(
            unknown_submissions=[unknown],
            searched_client_order_ids=["missing-order"],
            pagination_complete=False,
        )
        self.assertFalse(result.complete)
        self.assertTrue(result.blocks_new_risk)
        self.assertEqual(
            result.submission_resolutions[0].outcome,
            "UNKNOWN",
        )

    def test_complete_window_plus_explicit_lookup_can_prove_absence(self):
        unknown = UnknownSubmission.create(
            attempt_id="a1",
            intent_id="intent-unknown",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
                        client_order_id="missing-order",
            started_at="2026-09-24T18:00:00Z",
        )
        result = self.base(
            unknown_submissions=[unknown],
            searched_client_order_ids=["missing-order"],
            absence_coverage=absence_coverage(),
        )
        self.assertEqual(
            result.submission_resolutions[0].outcome,
            "PROVEN_ABSENT",
        )
        self.assertTrue(result.complete)

    def test_inconsistent_snapshot_cannot_prove_unknown_send_absent(self):
        unknown = UnknownSubmission.create(
            attempt_id="a-inconsistent-snapshot",
            intent_id="intent-unknown",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
                        client_order_id="missing-order",
            started_at="2026-09-24T18:00:00Z",
        )
        result = self.base(
            snapshot_consistency=None,
            unknown_submissions=[unknown],
            searched_client_order_ids=["missing-order"],
            absence_coverage=absence_coverage(),
        )
        self.assertEqual(result.submission_resolutions[0].outcome, "UNKNOWN")
        self.assertEqual(
            result.submission_resolutions[0].evidence_reason,
            "provider_snapshot_consistency_not_evidenced",
        )
        self.assertIn("ACCOUNT", result.blocking_resources)

    def test_single_complete_activity_window_is_not_enough_for_proven_absence(self):
        unknown = UnknownSubmission.create(
            attempt_id="a-single-window",
            intent_id="intent-unknown",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
                        client_order_id="missing-order",
            started_at="2026-09-24T18:00:00Z",
        )
        result = self.base(
            unknown_submissions=[unknown],
            searched_client_order_ids=["missing-order"],
        )
        self.assertEqual(result.submission_resolutions[0].outcome, "UNKNOWN")
        self.assertEqual(
            result.submission_resolutions[0].evidence_reason,
            "absence_surface_evidence_incomplete",
        )
        self.assertIn("ACCOUNT", result.blocking_resources)

    def test_missing_required_surface_keeps_unknown(self):
        unknown = UnknownSubmission.create(
            attempt_id="a-missing-surface",
            intent_id="intent-unknown",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
                        client_order_id="missing-order",
            started_at="2026-09-24T18:00:00Z",
        )
        evidence = [
            item
            for item in absence_coverage()
            if item.surface != "ACTIVITIES"
        ]
        result = self.base(
            unknown_submissions=[unknown],
            searched_client_order_ids=["missing-order"],
            absence_coverage=evidence,
        )
        self.assertEqual(result.submission_resolutions[0].outcome, "UNKNOWN")

    def test_unelapsed_consistency_horizon_keeps_unknown(self):
        unknown = UnknownSubmission.create(
            attempt_id="a-history-lag",
            intent_id="intent-unknown",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
                        client_order_id="missing-order",
            started_at="2026-09-24T18:00:00Z",
        )
        result = self.base(
            unknown_submissions=[unknown],
            searched_client_order_ids=["missing-order"],
            absence_coverage=absence_coverage(
                surface="ORDER_HISTORY",
                consistency_horizon_satisfied=False,
            ),
        )
        self.assertEqual(result.submission_resolutions[0].outcome, "UNKNOWN")

    def test_provider_semantics_must_exclude_execution_before_proven_absence(self):
        unknown = UnknownSubmission.create(
            attempt_id="a-semantics",
            intent_id="intent-unknown",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
                        client_order_id="missing-order",
            started_at="2026-09-24T18:00:00Z",
        )
        result = self.base(
            unknown_submissions=[unknown],
            searched_client_order_ids=["missing-order"],
            absence_coverage=absence_coverage(
                surface="EXECUTIONS",
                provider_semantics_exclude_execution=False,
            ),
        )
        self.assertEqual(result.submission_resolutions[0].outcome, "UNKNOWN")

    def test_absence_surfaces_must_cover_full_post_send_window(self):
        unknown = UnknownSubmission.create(
            attempt_id="a-truncated-window",
            intent_id="intent-unknown",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
                        client_order_id="missing-order",
            started_at="2026-09-24T18:00:00Z",
        )
        result = self.base(
            unknown_submissions=[unknown],
            searched_client_order_ids=["missing-order"],
            absence_coverage=absence_coverage(
                surface="EXECUTIONS",
                coverage_end="2026-09-24T18:00:00Z",
            ),
        )
        self.assertEqual(result.submission_resolutions[0].outcome, "UNKNOWN")
        self.assertEqual(
            result.submission_resolutions[0].evidence_reason,
            "absence_surface_evidence_incomplete",
        )
        self.assertIn("ACCOUNT", result.blocking_resources)

    def test_duplicate_absence_surface_evidence_is_rejected(self):
        evidence = absence_coverage()
        with self.assertRaisesRegex(ValueError, "duplicate absence coverage"):
            self.base(absence_coverage=[*evidence, evidence[0]])

    def test_missing_explicit_lookup_keeps_unknown_even_with_complete_pagination(self):
        unknown = UnknownSubmission.create(
            attempt_id="a1",
            intent_id="intent-unknown",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
                        client_order_id="missing-order",
            started_at="2026-09-24T18:00:00Z",
        )
        result = self.base(unknown_submissions=[unknown])
        self.assertEqual(result.submission_resolutions[0].outcome, "UNKNOWN")
        self.assertIn("ACCOUNT", result.blocking_resources)

    def test_manual_or_external_fill_is_never_discarded(self):
        result = self.base(
            provider_fills=[
                fill(),
                fill("manual-e2", None, "XYZ"),
            ]
        )
        self.assertEqual(result.unexpected_execution_ids, ("manual-e2",))
        self.assertIn("INSTRUMENT:XYZ", result.blocking_resources)
        self.assertFalse(result.complete)

    def test_late_fee_or_cash_adjustment_blocks_currency_until_explained(self):
        result = self.base(provider_cash={"USD": "899.50"})
        self.assertEqual(result.cash_differences["USD"], Decimal("-0.50"))
        self.assertIn("CASH:USD", result.blocking_resources)
        self.assertFalse(result.complete)

    def test_position_snapshot_difference_blocks_affected_instrument(self):
        result = self.base(provider_positions={"ABC": "0.5"})
        self.assertEqual(
            result.position_differences["ABC"],
            Decimal("-0.5"),
        )
        self.assertIn("INSTRUMENT:ABC", result.blocking_resources)

    def test_account_truth_rejects_non_string_and_normalized_duplicate_keys(self):
        with self.assertRaisesRegex(TypeError, "keys must be strings"):
            self.base(local_cash={1: "900"})
        with self.assertRaisesRegex(ValueError, "unique after normalization"):
            self.base(local_cash={"USD": "900", " USD ": "900"})

    def test_declared_tolerance_is_explicit_not_a_hidden_bucket(self):
        result = self.base(
            provider_cash={"USD": "899.99"},
            cash_tolerance={"USD": "0.01"},
        )
        self.assertTrue(result.complete)
        self.assertEqual(dict(result.cash_differences), {})

    def test_direct_working_order_cannot_bypass_positive_remaining_quantity(self):
        with self.assertRaisesRegex(ValueError, "remaining_quantity must be positive"):
            ProviderWorkingOrderEvidence(
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                provider_order_id="provider-order",
                client_order_id="client-order",
                instrument="ABC",
                remaining_quantity=Decimal("0"),
            )

    def test_direct_fill_cannot_bypass_provider_evidence_invariants(self):
        with self.assertRaisesRegex(ValueError, "quantity and price must be positive"):
            ProviderFillEvidence(
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                provider_execution_id="execution-1",
                client_order_id="client-1",
                instrument="ABC",
                quantity=Decimal("0"),
                price=Decimal("100"),
                fee_amount=Decimal("0"),
                fee_currency="USD",
                trade_time="2026-09-24T18:00:00Z",
            )
        with self.assertRaisesRegex(ValueError, "provider_execution_id is required"):
            ProviderFillEvidence(
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                provider_execution_id=" ",
                client_order_id="client-1",
                instrument="ABC",
                quantity=Decimal("1"),
                price=Decimal("100"),
                fee_amount=Decimal("0"),
                fee_currency="USD",
                trade_time="2026-09-24T18:00:00Z",
            )

    def test_direct_unknown_submission_cannot_bypass_identity_or_time_validation(self):
        with self.assertRaisesRegex(ValueError, "attempt_id is required"):
            UnknownSubmission(
                attempt_id=" ",
                client_order_id="client-1",
                started_at="2026-09-24T18:00:00Z",
            )
        with self.assertRaisesRegex(ValueError, "must include timezone"):
            UnknownSubmission(
                attempt_id="attempt-1",
                client_order_id="client-1",
                started_at="2026-09-24T18:00:00",
            )

    def test_pre_submission_working_snapshot_cannot_resolve_unknown_send(self):
        unknown = UnknownSubmission.create(
            attempt_id="attempt-working-pre-snapshot",
            intent_id="intent-working-pre-snapshot",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            client_order_id="client-working-pre-snapshot",
            started_at="2026-09-24T18:00:00Z",
        )
        order = ProviderWorkingOrderEvidence.create(
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            provider_order_id="provider-working-pre-snapshot",
            client_order_id="client-working-pre-snapshot",
            instrument="ABC",
            remaining_quantity="1",
        )
        # Base snapshot starts at 17:00, before the 18:00 submission. With no
        # per-order observation timestamp, that snapshot cannot prove the
        # working order was observed after this send.
        result = self.base(
            local_working_client_order_ids=["client-working-pre-snapshot"],
            provider_working_orders=[order],
            unknown_submissions=[unknown],
            searched_client_order_ids=["client-working-pre-snapshot"],
        )
        resolution = result.submission_resolutions[0]
        self.assertEqual(resolution.outcome, "UNKNOWN")
        self.assertEqual(
            resolution.evidence_reason,
            "provider_working_order_snapshot_not_causal_for_submission",
        )
        self.assertFalse(result.complete)
        self.assertIn("ACCOUNT", result.blocking_resources)

    def test_unknown_send_resolves_to_observed_working_order_without_retry(self):
        unknown = UnknownSubmission.create(
            attempt_id="attempt-working",
            intent_id="intent-unknown",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
                        client_order_id="client-working",
            started_at="2026-09-24T18:00:00Z",
        )
        order = ProviderWorkingOrderEvidence.create(
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            provider_order_id="provider-working",
            client_order_id="client-working",
            instrument="ABC",
            remaining_quantity="1",
        )
        result = self.base(
            local_working_client_order_ids=["client-working"],
            provider_working_orders=[order],
            unknown_submissions=[unknown],
            searched_client_order_ids=["client-working"],
            snapshot_consistency=SnapshotConsistencyEvidence(
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                mode="ATOMIC",
                query_started_at="2026-09-24T18:01:00Z",
                query_completed_at="2026-09-24T19:00:00Z",
            ),
        )
        resolution = result.submission_resolutions[0]
        self.assertEqual(resolution.outcome, "OBSERVED_WORKING_ORDER")
        self.assertEqual(resolution.provider_order_ids, ("provider-working",))
        self.assertTrue(result.complete)
        self.assertIn(
            "previously UNKNOWN submission has provider working-order evidence",
            result.reasons,
        )

    def test_missing_snapshot_consistency_evidence_blocks_ready_state(self):
        result = self.base(snapshot_consistency=None)
        self.assertFalse(result.complete)
        self.assertIn("ACCOUNT", result.blocking_resources)
        self.assertIn(
            "provider snapshot consistency is not evidenced",
            result.reasons,
        )

    def test_snapshot_window_must_be_covered_by_reconciliation_window(self):
        result = self.base(
            snapshot_consistency=SnapshotConsistencyEvidence(
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                mode="ATOMIC",
                query_started_at="2026-09-24T16:59:59Z",
                query_completed_at="2026-09-24T18:00:00Z",
            )
        )
        self.assertFalse(result.complete)
        self.assertFalse(result.snapshot_consistent)
        self.assertIn("ACCOUNT", result.blocking_resources)
        self.assertIn(
            "provider snapshot query window is outside reconciliation coverage",
            result.reasons,
        )

    def test_snapshot_completion_after_coverage_end_blocks_unknown_absence(self):
        unknown = UnknownSubmission.create(
            attempt_id="a-window-mismatch",
            intent_id="intent-unknown",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
                        client_order_id="missing-order",
            started_at="2026-09-24T18:00:00Z",
        )
        result = self.base(
            snapshot_consistency=SnapshotConsistencyEvidence(
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                mode="ATOMIC",
                query_started_at="2026-09-24T18:00:00Z",
                query_completed_at="2026-09-24T19:00:01Z",
            ),
            unknown_submissions=[unknown],
            searched_client_order_ids=["missing-order"],
            absence_coverage=absence_coverage(),
        )
        self.assertFalse(result.complete)
        self.assertEqual(result.submission_resolutions[0].outcome, "UNKNOWN")
        self.assertEqual(
            result.submission_resolutions[0].evidence_reason,
            "provider_snapshot_consistency_not_evidenced",
        )

    def test_atomic_snapshot_with_sequence_gap_is_not_consistent(self):
        result = self.base(
            snapshot_consistency=SnapshotConsistencyEvidence(
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                mode="ATOMIC",
                query_started_at="2026-09-24T17:00:00Z",
                query_completed_at="2026-09-24T19:00:00Z",
                sequence_gap_detected=True,
            )
        )
        self.assertFalse(result.snapshot_consistent)
        self.assertFalse(result.complete)
        self.assertIn("ACCOUNT", result.blocking_resources)
        self.assertIn(
            "provider snapshot stream contains a sequence gap",
            result.reasons,
        )

    def test_composed_snapshot_requires_buffer_replay_without_gap(self):
        result = self.base(
            snapshot_consistency=SnapshotConsistencyEvidence(
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                mode="COMPOSED",
                query_started_at="2026-09-24T17:00:00Z",
                query_completed_at="2026-09-24T19:00:00Z",
                buffered_stream_events=True,
                replay_complete=False,
                sequence_gap_detected=False,
            )
        )
        self.assertFalse(result.complete)
        self.assertIn("ACCOUNT", result.blocking_resources)

        complete = self.base(
            snapshot_consistency=SnapshotConsistencyEvidence(
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                mode="COMPOSED",
                query_started_at="2026-09-24T17:00:00Z",
                query_completed_at="2026-09-24T19:00:00Z",
                buffered_stream_events=True,
                replay_complete=True,
                sequence_gap_detected=False,
            )
        )
        self.assertTrue(complete.complete)

    def test_stream_gap_blocks_composed_snapshot_even_after_replay(self):
        result = self.base(
            snapshot_consistency=SnapshotConsistencyEvidence(
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                mode="COMPOSED",
                query_started_at="2026-09-24T17:00:00Z",
                query_completed_at="2026-09-24T19:00:00Z",
                buffered_stream_events=True,
                replay_complete=True,
                sequence_gap_detected=True,
            )
        )
        self.assertFalse(result.complete)
        self.assertIn(
            "provider snapshot stream contains a sequence gap",
            result.reasons,
        )

    def test_external_working_order_blocks_affected_instrument(self):
        result = self.base(
            provider_working_orders=[
                ProviderWorkingOrderEvidence.create(
                    provider_id="TEST_PROVIDER",
                    account_id="test-account",
                    environment="PAPER",
                    provider_order_id="manual-42",
                    client_order_id=None,
                    instrument="XYZ",
                    remaining_quantity="3",
                )
            ]
        )
        self.assertFalse(result.complete)
        self.assertEqual(
            result.unexpected_working_provider_order_ids,
            ("manual-42",),
        )
        self.assertIn("INSTRUMENT:XYZ", result.blocking_resources)

    def test_local_and_provider_working_orders_must_match(self):
        matching = ProviderWorkingOrderEvidence.create(
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            provider_order_id="provider-1",
            client_order_id="client-1",
            instrument="ABC",
            remaining_quantity="2",
        )
        result = self.base(
            local_working_client_order_ids=["client-1"],
            provider_working_orders=[matching],
        )
        self.assertTrue(result.complete)
        self.assertEqual(
            result.matched_working_client_order_ids,
            ("client-1",),
        )

        missing = self.base(local_working_client_order_ids=["client-1"])
        self.assertFalse(missing.complete)
        self.assertEqual(
            missing.missing_local_working_client_order_ids,
            ("client-1",),
        )
        self.assertIn("ACCOUNT", missing.blocking_resources)

    def test_duplicate_provider_client_order_mapping_fails_closed(self):
        with self.assertRaisesRegex(
            ValueError,
            "client order id maps to multiple provider working orders",
        ):
            self.base(
                provider_working_orders=[
                    ProviderWorkingOrderEvidence.create(
                        provider_id="TEST_PROVIDER",
                        account_id="test-account",
                        environment="PAPER",
                        provider_order_id="p1",
                        client_order_id="c-shared",
                        instrument="ABC",
                        remaining_quantity="1",
                    ),
                    ProviderWorkingOrderEvidence.create(
                        provider_id="TEST_PROVIDER",
                        account_id="test-account",
                        environment="PAPER",
                        provider_order_id="p2",
                        client_order_id="c-shared",
                        instrument="ABC",
                        remaining_quantity="1",
                    ),
                ]
            )

    def test_fill_evidence_from_other_provider_or_account_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "provider_id mismatch"):
            self.base(
                provider_fills=[
                    ProviderFillEvidence.create(
                        provider_id="OTHER_PROVIDER",
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
                ]
            )
        with self.assertRaisesRegex(ValueError, "account_id mismatch"):
            self.base(
                provider_fills=[
                    ProviderFillEvidence.create(
                        provider_id="TEST_PROVIDER",
                        account_id="other-account",
                        environment="PAPER",
                        provider_execution_id="e1",
                        client_order_id="c1",
                        instrument="ABC",
                        quantity="1",
                        price="100",
                        fee_currency="USD",
                        trade_time="2026-09-24T18:00:00Z",
                    )
                ]
            )

    def test_working_order_evidence_from_other_account_fails_closed(self):
        foreign = ProviderWorkingOrderEvidence.create(
            provider_id="TEST_PROVIDER",
            account_id="other-account",
            environment="PAPER",
            provider_order_id="provider-foreign",
            client_order_id="client-1",
            instrument="ABC",
            remaining_quantity="1",
        )
        with self.assertRaisesRegex(ValueError, "account_id mismatch"):
            self.base(
                local_working_client_order_ids=["client-1"],
                provider_working_orders=[foreign],
            )

    def test_provider_execution_conflict_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.base(
                provider_fills=[
                    fill("e1", "c1"),
                    ProviderFillEvidence.create(
                        provider_id="TEST_PROVIDER",
                        account_id="test-account",
                        environment="PAPER",
                        provider_execution_id="e1",
                        client_order_id="c1",
                        instrument="ABC",
                        quantity="1",
                        price="101",
                        fee_currency="USD",
                        trade_time="2026-09-24T18:00:00Z",
                    ),
                ]
            )

    def activity(self, **overrides):
        values = dict(
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            activity_id="activity-1",
            activity_type="CASH_ADJUSTMENT",
            origin="MANUAL",
            occurred_at="2026-09-24T18:15:00Z",
            currency="USD",
        )
        values.update(overrides)
        return ProviderActivityEvidence.create(**values)

    def activity_coverage(self, **overrides):
        values = dict(
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            surface="ACTIVITIES",
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
            provider_semantics_exclude_execution=False,
        )
        values.update(overrides)
        return CoverageSurfaceEvidence(**values)

    def test_provider_activity_scope_is_self_bound_and_must_match(self):
        evidence = self.activity()
        result = self.base(provider_activities=[evidence])
        self.assertEqual(result.unexpected_provider_activity_ids, ("activity-1",))

        with self.assertRaisesRegex(ValueError, "reconciliation provider_id"):
            self.base(
                provider_activities=[evidence],
                provider_activity_provider_id="OTHER",
            )
        with self.assertRaisesRegex(ValueError, "reconciliation account_id"):
            self.base(
                provider_activities=[evidence],
                provider_activity_account_id="other-account",
            )
        with self.assertRaisesRegex(ValueError, "environment mismatch"):
            self.base(
                provider_activities=[self.activity(environment="LIVE")],
            )
        with self.assertRaisesRegex(ValueError, "coverage scope mismatch"):
            self.base(
                activity_coverage=self.activity_coverage(environment="LIVE"),
                require_activity_reconciliation=True,
            )

    def test_environment_scope_is_enforced_before_reconciliation(self):
        with self.assertRaisesRegex(ValueError, "fill evidence environment mismatch"):
            self.base(provider_fills=[fill(environment="LIVE")])

        working = ProviderWorkingOrderEvidence.create(
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="LIVE",
            provider_order_id="working-live",
            client_order_id="client-live",
            instrument="ABC",
            remaining_quantity="1",
        )
        with self.assertRaisesRegex(ValueError, "working-order evidence environment mismatch"):
            self.base(provider_working_orders=[working])

        unknown = UnknownSubmission.create(
            attempt_id="attempt-live",
            intent_id="intent-live",
            client_order_id="client-live",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="LIVE",
            started_at="2026-09-24T18:00:00Z",
        )
        with self.assertRaisesRegex(ValueError, "unknown submission scope mismatch"):
            self.base(unknown_submissions=[unknown])

        snapshot = SnapshotConsistencyEvidence(
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="LIVE",
            mode="ATOMIC",
            query_started_at="2026-09-24T17:00:00Z",
            query_completed_at="2026-09-24T19:00:00Z",
        )
        with self.assertRaisesRegex(ValueError, "snapshot consistency scope mismatch"):
            self.base(snapshot_consistency=snapshot)

        foreign_coverage = absence_coverage(environment="LIVE")
        with self.assertRaisesRegex(ValueError, "absence coverage scope mismatch"):
            self.base(absence_coverage=foreign_coverage)

    def test_unexpected_manual_activity_blocks_affected_currency(self):
        result = self.base(provider_activities=[self.activity()])
        self.assertFalse(result.complete)
        self.assertEqual(
            result.unexpected_provider_activity_ids,
            ("activity-1",),
        )
        self.assertEqual(
            result.manual_or_external_activity_ids,
            ("activity-1",),
        )
        self.assertIn("CASH:USD", result.blocking_resources)

    def test_unexpected_external_instrument_activity_blocks_instrument(self):
        result = self.base(
            provider_activities=[
                self.activity(
                    activity_id="activity-position",
                    activity_type="POSITION_ADJUSTMENT",
                    origin="EXTERNAL",
                    currency=None,
                    instrument="XYZ",
                )
            ]
        )
        self.assertIn("INSTRUMENT:XYZ", result.blocking_resources)
        self.assertFalse(result.complete)

    def test_unexpected_autotrade_activity_still_blocks_local_truth_gap(self):
        result = self.base(
            provider_activities=[
                self.activity(
                    activity_id="activity-auto",
                    origin="AUTOTRADE",
                )
            ]
        )
        self.assertEqual(
            result.unexpected_provider_activity_ids,
            ("activity-auto",),
        )
        self.assertFalse(result.complete)

    def test_matched_activity_and_complete_activity_window_reconcile(self):
        activity = self.activity()
        result = self.base(
            local_provider_activity_ids=["activity-1"],
            provider_activities=[activity],
            activity_coverage=self.activity_coverage(),
            require_activity_reconciliation=True,
        )
        self.assertTrue(result.complete)
        self.assertEqual(
            result.matched_provider_activity_ids,
            ("activity-1",),
        )
        self.assertTrue(result.activity_coverage_complete)
        self.assertFalse(result.manual_or_external_activity_ids)

    def test_required_activity_coverage_fails_closed_when_missing_or_incomplete(self):
        missing = self.base(require_activity_reconciliation=True)
        incomplete = self.base(
            activity_coverage=self.activity_coverage(
                pagination_complete=False,
            ),
            require_activity_reconciliation=True,
        )
        wrong_surface = self.base(
            activity_coverage=CoverageSurfaceEvidence(
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                surface="ORDER_HISTORY",
                coverage_start="2026-09-24T17:00:00Z",
                coverage_end="2026-09-24T19:00:00Z",
                pagination_complete=True,
                consistency_horizon_satisfied=True,
                provider_semantics_exclude_execution=True,
            ),
            require_activity_reconciliation=True,
        )
        for result in (missing, incomplete, wrong_surface):
            self.assertFalse(result.activity_coverage_complete)
            self.assertFalse(result.complete)
            self.assertIn("ACCOUNT", result.blocking_resources)

    def test_missing_local_expected_activity_blocks_account(self):
        result = self.base(
            local_provider_activity_ids=["activity-missing"],
            activity_coverage=self.activity_coverage(),
            require_activity_reconciliation=True,
        )
        self.assertEqual(
            result.missing_local_provider_activity_ids,
            ("activity-missing",),
        )
        self.assertIn("ACCOUNT", result.blocking_resources)
        self.assertFalse(result.complete)

    def test_conflicting_provider_activity_identity_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "conflicting observations"):
            self.base(
                provider_activities=[
                    self.activity(),
                    self.activity(activity_type="FEE_ADJUSTMENT"),
                ]
            )

    def test_identical_provider_activity_duplicate_is_idempotent(self):
        activity = self.activity()
        result = self.base(
            local_provider_activity_ids=["activity-1"],
            provider_activities=[activity, activity],
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.matched_provider_activity_ids, ("activity-1",))

    def test_provider_activity_origin_and_timestamp_are_strict(self):
        with self.assertRaisesRegex(ValueError, "unsupported provider activity origin"):
            self.activity(origin="MODEL")
        with self.assertRaisesRegex(ValueError, "must include timezone"):
            self.activity(occurred_at="2026-09-24T18:15:00")

    def test_generic_activity_does_not_resolve_unknown_submission(self):
        unknown = UnknownSubmission.create(
            attempt_id="attempt-activity-only",
            intent_id="intent-unknown",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
                        client_order_id="client-activity-only",
            started_at="2026-09-24T18:00:00Z",
        )
        result = self.base(
            unknown_submissions=[unknown],
            provider_activities=[
                self.activity(
                    client_order_id="client-activity-only",
                    origin="AUTOTRADE",
                )
            ],
        )
        self.assertEqual(result.submission_resolutions[0].outcome, "UNKNOWN")
        self.assertFalse(result.complete)


    def test_reconciliation_environment_is_canonical_contract_enum(self):
        with self.assertRaisesRegex(ValueError, "environment must be one of"):
            ProviderFillEvidence.create(
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PROD",
                provider_execution_id="e-invalid-env",
                client_order_id="c-invalid-env",
                instrument="ABC",
                quantity="1",
                price="100",
                fee_currency="USD",
                trade_time="2026-09-24T18:00:00Z",
            )

        with self.assertRaisesRegex(ValueError, "environment must be one of"):
            UnknownSubmission.create(
                attempt_id="a-invalid-env",
                intent_id="intent-invalid-env",
                client_order_id="c-invalid-env",
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="SANDBOX",
                started_at="2026-09-24T18:00:00Z",
            )

        with self.assertRaisesRegex(ValueError, "environment must be one of"):
            self.base(environment="PRODUCTION")


if __name__ == "__main__":
    unittest.main()
