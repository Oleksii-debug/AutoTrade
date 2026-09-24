from decimal import Decimal
import unittest

from mvp.autotrade_mvp.reconciliation import (
    CoverageSurfaceEvidence,
    ProviderFillEvidence,
    ProviderWorkingOrderEvidence,
    SnapshotConsistencyEvidence,
    UnknownSubmission,
    reconcile_account,
)


def fill(
    execution_id="e1",
    client_order_id="c1",
    instrument="ABC",
    fee_amount="0",
):
    return ProviderFillEvidence.create(
        provider_execution_id=execution_id,
        client_order_id=client_order_id,
        instrument=instrument,
        quantity="1",
        price="100",
        fee_amount=fee_amount,
        fee_currency="USD",
        trade_time="2026-09-24T18:00:00Z",
    )


def absence_coverage(**overrides):
    values = []
    for surface in ("OPEN_ORDERS", "ORDER_HISTORY", "EXECUTIONS", "ACTIVITIES"):
        item = dict(
            surface=surface,
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
            provider_semantics_exclude_execution=True,
        )
        if surface == overrides.get("surface"):
            item.update({key: value for key, value in overrides.items() if key != "surface"})
        values.append(CoverageSurfaceEvidence(**item))
    return values


class ReconciliationTests(unittest.TestCase):
    def base(self, **overrides):
        values = dict(
            local_cash={"USD": "900"},
            provider_cash={"USD": "900"},
            local_positions={"ABC": "1"},
            provider_positions={"ABC": "1"},
            local_execution_ids=["e1"],
            provider_fills=[fill()],
            snapshot_consistency=SnapshotConsistencyEvidence(
                mode="ATOMIC",
                query_started_at="2026-09-24T17:00:00Z",
                query_completed_at="2026-09-24T19:00:00Z",
            ),
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=True,
        )
        values.update(overrides)
        return reconcile_account(**values)

    def test_complete_matching_window_reconciles(self):
        result = self.base()
        self.assertTrue(result.complete)
        self.assertFalse(result.blocks_new_risk)
        self.assertEqual(result.matched_execution_ids, ("e1",))

    def test_incomplete_pagination_cannot_prove_unknown_send_absent(self):
        unknown = UnknownSubmission.create(
            attempt_id="a1",
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

    def test_duplicate_absence_surface_evidence_is_rejected(self):
        evidence = absence_coverage()
        with self.assertRaisesRegex(ValueError, "duplicate absence coverage"):
            self.base(absence_coverage=[*evidence, evidence[0]])

    def test_missing_explicit_lookup_keeps_unknown_even_with_complete_pagination(self):
        unknown = UnknownSubmission.create(
            attempt_id="a1",
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
                provider_order_id="provider-order",
                client_order_id="client-order",
                instrument="ABC",
                remaining_quantity=Decimal("0"),
            )

    def test_direct_fill_cannot_bypass_provider_evidence_invariants(self):
        with self.assertRaisesRegex(ValueError, "quantity and price must be positive"):
            ProviderFillEvidence(
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

    def test_unknown_send_resolves_to_observed_working_order_without_retry(self):
        unknown = UnknownSubmission.create(
            attempt_id="attempt-working",
            client_order_id="client-working",
            started_at="2026-09-24T18:00:00Z",
        )
        order = ProviderWorkingOrderEvidence.create(
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
            client_order_id="missing-order",
            started_at="2026-09-24T18:00:00Z",
        )
        result = self.base(
            snapshot_consistency=SnapshotConsistencyEvidence(
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

    def test_composed_snapshot_requires_buffer_replay_without_gap(self):
        result = self.base(
            snapshot_consistency=SnapshotConsistencyEvidence(
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
                        provider_order_id="p1",
                        client_order_id="c-shared",
                        instrument="ABC",
                        remaining_quantity="1",
                    ),
                    ProviderWorkingOrderEvidence.create(
                        provider_order_id="p2",
                        client_order_id="c-shared",
                        instrument="ABC",
                        remaining_quantity="1",
                    ),
                ]
            )

    def test_provider_execution_conflict_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.base(
                provider_fills=[
                    fill("e1", "c1"),
                    ProviderFillEvidence.create(
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


if __name__ == "__main__":
    unittest.main()
