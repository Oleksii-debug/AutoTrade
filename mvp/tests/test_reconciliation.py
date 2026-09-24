from decimal import Decimal
import unittest

from mvp.autotrade_mvp.reconciliation import (
    ProviderFillEvidence,
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


class ReconciliationTests(unittest.TestCase):
    def base(self, **overrides):
        values = dict(
            local_cash={"USD": "900"},
            provider_cash={"USD": "900"},
            local_positions={"ABC": "1"},
            provider_positions={"ABC": "1"},
            local_execution_ids=["e1"],
            provider_fills=[fill()],
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
        )
        self.assertEqual(
            result.submission_resolutions[0].outcome,
            "PROVEN_ABSENT",
        )
        self.assertTrue(result.complete)

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

    def test_declared_tolerance_is_explicit_not_a_hidden_bucket(self):
        result = self.base(
            provider_cash={"USD": "899.99"},
            cash_tolerance={"USD": "0.01"},
        )
        self.assertTrue(result.complete)
        self.assertEqual(dict(result.cash_differences), {})

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
