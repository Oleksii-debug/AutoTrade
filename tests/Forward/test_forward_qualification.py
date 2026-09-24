import unittest
from decimal import Decimal

from mvp.autotrade_mvp.forward_qualification import (
    ForwardPaperEvidence,
    ForwardRequirements,
    qualify_forward_paper,
)


class ForwardQualificationTests(unittest.TestCase):
    def evidence(self, **changes):
        values = dict(
            run_id="paper-1",
            source_revision="abc123",
            provider_id="ALPACA",
            environment="PAPER",
            account_fingerprint="acct-hash",
            started_at="2026-09-20T00:00:00Z",
            ended_at="2026-09-22T00:00:00Z",
            decision_count=1200,
            submitted_order_count=120,
            reconciled_order_count=120,
            unknown_submission_count=0,
            unresolved_reconciliation_count=0,
            market_data_gap_count=0,
            restart_recovery_count=3,
            restart_recovery_failures=0,
            realized_pnl="12.50",
            explicit_costs="4.25",
            max_drawdown="18.00",
            evidence_ids=("journal-1", "reconcile-1", "restart-1"),
        )
        values.update(changes)
        return ForwardPaperEvidence(**values)

    def requirements(self):
        return ForwardRequirements("24", 500, 50)

    def test_complete_forward_evidence_passes_without_claiming_profitability(self):
        result = qualify_forward_paper(self.evidence(realized_pnl="-100"), self.requirements())
        self.assertTrue(result.passed)
        self.assertEqual(result.reasons, ())
        self.assertEqual(len(result.evidence_fingerprint), 64)

    def test_backtest_or_live_evidence_cannot_masquerade_as_forward_paper(self):
        for mode in ("SIMULATION", "LIVE"):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ValueError, "PAPER, TEST or DEMO"):
                    qualify_forward_paper(self.evidence(environment=mode), self.requirements())

    def test_unknown_or_unreconciled_send_blocks(self):
        result = qualify_forward_paper(
            self.evidence(unknown_submission_count=1, reconciled_order_count=119),
            self.requirements(),
        )
        self.assertFalse(result.passed)
        self.assertIn("unknown_submissions_present", result.reasons)
        self.assertIn("not_all_orders_reconciled", result.reasons)

    def test_market_gap_and_restart_failure_block(self):
        result = qualify_forward_paper(
            self.evidence(market_data_gap_count=1, restart_recovery_failures=1),
            self.requirements(),
        )
        self.assertFalse(result.passed)
        self.assertIn("market_data_gaps_present", result.reasons)
        self.assertIn("restart_recovery_failure", result.reasons)

    def test_duration_and_sample_requirements_are_exact(self):
        result = qualify_forward_paper(
            self.evidence(
                ended_at="2026-09-20T12:00:00Z",
                decision_count=10,
                submitted_order_count=2,
                reconciled_order_count=2,
            ),
            self.requirements(),
        )
        self.assertFalse(result.passed)
        self.assertIn("insufficient_forward_duration", result.reasons)
        self.assertIn("insufficient_decision_count", result.reasons)
        self.assertIn("insufficient_submitted_orders", result.reasons)


if __name__ == "__main__":
    unittest.main()
