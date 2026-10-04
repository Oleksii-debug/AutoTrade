"""Independent exact-capital and historical-cut oracles on the product loop."""
from decimal import Decimal, Inexact, Rounded, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import simulation_session as session
from mvp.autotrade_mvp.accessibility import format_accessible_status
from mvp.autotrade_mvp.cli import get_economic_report, get_status
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reconciliation_journal import load_account_resource_availability_evidence
from mvp.autotrade_mvp.simulated_provider import SimulatedProvider
from mvp.tests.test_autonomous_simulation import run

PRICES = ["100", "101", "103", "90", "110", "120", "121"]


class IntegratedCapitalReportTests(unittest.TestCase):
    def test_pending_proceeds_have_exact_separate_cash_buckets_and_pnl(self):
        with TemporaryDirectory() as directory, patch.object(session, "INITIAL_CASH", Decimal("150")):
            run(directory, PRICES, stop_after_episodes=4)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            before = store.whole_store_state_cut()
            with patch.object(SimulatedProvider, "transport_send", side_effect=AssertionError("read cannot send")):
                report = get_economic_report(directory)
                status = get_status(directory)
                text = format_accessible_status(status, report)
            self.assertEqual(report["cash_buckets"], {
                "currency": "USD", "account_cash": "136.807", "settled_cash": "150",
                "unsettled_receivable": "89.91", "unsettled_payable": "103.103",
                "reserved_cash": "0", "available_cash": "46.897",
            })
            self.assertEqual(report["realized_pnl"], "-13")
            self.assertEqual(report["unrealized_pnl"], "0")
            self.assertEqual(report["total_fees"], "0.193")
            self.assertEqual(report["net_pnl"], "-13.193")
            self.assertTrue(report["financial_equality_verified"])
            self.assertIn("Реально доступні кошти: 46.897", text)
            self.assertEqual(store.whole_store_state_cut(), before)
            with localcontext() as context:
                context.prec = 2
                context.traps[Inexact] = context.traps[Rounded] = True
                self.assertEqual(get_economic_report(directory), report)
            run(directory, PRICES)
            reopened = get_economic_report(directory)
            self.assertEqual(reopened["cash_buckets"]["available_cash"], "15.686")
            self.assertEqual(reopened["cash_buckets"]["unsettled_receivable"], "0")
            self.assertEqual(reopened["cash_buckets"]["unsettled_payable"], "121.121")
            self.assertEqual(reopened["net_pnl"], "-13.314")
            self.assertEqual(reopened["open_cost_basis"], "121")

    def test_historical_availability_remains_valid_without_authorizing_current_cash(self):
        with TemporaryDirectory() as directory:
            run(directory)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            risk = store.load_events_by_aggregate_type("risk_decision")[0]["payload"]
            evidence = risk["reservation_availability_evidence"]
            args = dict(checkpoint_event_id=evidence["checkpoint_event_id"],
                provider_id=session.PROVIDER, account_id=session.ACCOUNT,
                environment=session.ENVIRONMENT, resources=("CASH:USD",),
                now=risk["evaluated_at"], max_age_seconds=evidence["max_age_seconds"])
            with self.assertRaisesRegex(ValueError, "predates settlement financial truth"):
                load_account_resource_availability_evidence(store, **args)
            historical = load_account_resource_availability_evidence(store,
                journal_sequence_cut=risk["journal_sequence_cut"], **args)
            self.assertEqual(historical["availability"], {"CASH:USD": "1000"})
            with self.assertRaisesRegex(ValueError, "current availability cannot use a historical"):
                load_account_resource_availability_evidence(store, require_latest_scope=True,
                    journal_sequence_cut=risk["journal_sequence_cut"], **args)
            for invalid in (-1, True, store.current_journal_sequence() + 1):
                with self.subTest(cut=invalid), self.assertRaisesRegex(ValueError, "historical availability journal cut"):
                    load_account_resource_availability_evidence(store, journal_sequence_cut=invalid, **args)


if __name__ == "__main__":
    unittest.main()
