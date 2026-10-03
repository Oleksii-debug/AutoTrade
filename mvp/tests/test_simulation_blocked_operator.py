"""Operator projection for canonical zero-wire BLOCKED completion."""

from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.authority import AuthorityService
from mvp.autotrade_mvp.cli import get_economic_report, get_status
from mvp.autotrade_mvp.simulation_session import run_canonical_simulation


BUY = ["100", "101", "103"]
NOW = "2026-10-03T17:50:00Z"


class SimulationBlockedOperatorTests(unittest.TestCase):
    def test_completed_zero_wire_blocked_is_valid_operator_state(self):
        with TemporaryDirectory() as directory:
            with patch.object(
                AuthorityService,
                "dispatch_allowed",
                return_value=(False, "test_pre_send_block"),
            ):
                result = run_canonical_simulation(
                    BUY,
                    directory,
                    episode_id="blocked-operator",
                    now=NOW,
                )

            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["reason"], "test_pre_send_block")
            self.assertTrue(result["reconciled"])
            self.assertEqual(result["new_outbound_requests"], 0)

            status = get_status(directory)
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["session_status"], "BLOCKED")
            self.assertEqual(status["decision"], "BUY")
            self.assertTrue(status["reconciled"])
            self.assertTrue(status["replay_verified"])
            self.assertEqual(status["reason"], "test_pre_send_block")
            self.assertEqual(status["cash"], "1000")
            self.assertEqual(status["position"], "0")
            self.assertEqual(status["active_reservations"], [])
            self.assertEqual(status["fills"], {})
            self.assertEqual(status["new_outbound_requests"], 0)

            report = get_economic_report(directory)
            self.assertEqual(report["cash"], "1000")
            self.assertEqual(report["final_equity"], "1000")
            self.assertEqual(report["net_pnl"], "0")
            self.assertEqual(report["total_fees"], "0")
            self.assertEqual(report["ending_position"], "0")
            self.assertEqual(report["trade_count"], 0)
            self.assertEqual(
                report["economic_edge_claim"],
                "UNPROVEN_SIMULATION_ONLY",
            )

            replay = run_canonical_simulation(
                BUY,
                directory,
                episode_id="blocked-operator",
            )
            self.assertEqual(replay["status"], "BLOCKED")
            self.assertTrue(replay["resumed"])
            self.assertEqual(replay["new_outbound_requests"], 0)
            self.assertEqual(get_status(directory), status)


if __name__ == "__main__":
    unittest.main()
