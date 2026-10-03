import io
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.accessibility import format_accessible_status
from mvp.autotrade_mvp.cli import main
from mvp.autotrade_mvp.pipeline import run_vertical_slice


class AccessibleStatusTests(unittest.TestCase):
    def test_not_started_is_truthful_and_non_visual(self):
        text = format_accessible_status({"status": "not_started"})
        self.assertIn("System state: Not started", text)
        self.assertIn("Live order submission: unavailable", text)
        self.assertIn("Economic edge: unproven", text)
        self.assertNotIn("\x1b", text)

    def test_recovery_state_is_explicit(self):
        text = format_accessible_status(
            {
                "status": "needs_recovery",
                "symbol": "SIM",
                "initial_cash": "1000",
                "evidence_count": 2,
                "fills": {},
                "replay_verified": False,
            }
        )
        self.assertIn("Replay verification: failed", text)
        self.assertIn("Action required: recovery or reconciliation is needed", text)
        self.assertNotIn("completed", text.lower())

    def test_missing_replay_evidence_is_unavailable_not_failed(self):
        text = format_accessible_status(
            {
                "status": "running",
                "symbol": "SIM",
                "initial_cash": "1000",
                "evidence_count": 0,
                "fills": {},
            }
        )
        self.assertIn("Replay verification: unavailable", text)
        self.assertNotIn("Replay verification: failed", text)

    def test_economic_fields_are_copyable_text(self):
        text = format_accessible_status(
            {
                "status": "running",
                "symbol": "SIM",
                "initial_cash": "1000",
                "evidence_count": 1,
                "fills": {"fill-1": {}},
                "replay_verified": True,
            },
            {
                "final_equity": "1001.25",
                "net_pnl": "1.25",
                "total_fees": "0.25",
                "turnover": "100",
                "max_drawdown": "0",
                "reconciled": True,
            },
        )
        for expected in (
            "Final equity: 1001.25",
            "Net profit or loss: 1.25",
            "Total fees: 0.25",
            "Maximum drawdown: 0",
            "Economic reconciliation: passed",
        ):
            self.assertIn(expected, text)

    def test_malformed_canonical_reservations_remain_readable_and_truthful(self):
        text = format_accessible_status(
            {
                "status": "running",
                "state_format": "canonical_journal",
                "symbol": "SIM",
                "initial_cash": "1000",
                "fills": {},
                "active_reservations": {"unexpected": "mapping"},
            }
        )
        self.assertIn("Active reservations: unavailable; malformed state", text)
        self.assertIn("inspect or restore reservation state", text)
        self.assertIn("Economic edge: unproven", text)

    def test_malformed_reservation_items_do_not_hide_status_surface(self):
        text = format_accessible_status(
            {
                "status": "running",
                "state_format": "canonical_journal",
                "symbol": "SIM",
                "initial_cash": "1000",
                "fills": {},
                "active_reservations": [
                    None,
                    {"state": "WORKING", "remaining": "not-a-mapping"},
                    {"state": "WORKING", "remaining": {"USD": "25.00"}},
                ],
            }
        )
        self.assertIn(
            "Active reservations: unavailable; one or more reservation entries are malformed",
            text,
        )
        self.assertIn("Structurally readable reservation entries: 1", text)
        self.assertNotIn("Active reservations: 3", text)
        self.assertIn("inspect or restore reservation state", text)
        self.assertIn("Reservation detail: unavailable; malformed state", text)
        self.assertIn("malformed remaining resources", text)
        self.assertIn("Reserved USD: 25.00; state: WORKING", text)

    def test_unknown_top_level_state_fails_closed_as_corrupt(self):
        text = format_accessible_status(
            {
                "status": "UNKNOWN_VENDOR_STATE",
                "state_format": "canonical_journal",
                "cash": "999999",
                "position": "123",
                "fills": {"fake": {}},
            }
        )
        self.assertIn("System state: Corrupt or unreadable state", text)
        self.assertIn(
            "Action required: inspect or restore the simulated state before continuing",
            text,
        )
        self.assertNotIn("Cash (USD): 999999", text)
        self.assertNotIn("Position (shares): 123", text)
        self.assertNotIn("Recorded fills: 1", text)

    def test_hostile_top_level_dict_subclass_fails_closed_without_get(self):
        class HostileStatus(dict):
            def get(self, *args, **kwargs):
                raise AssertionError("malformed status must not execute overridden get")

        text = format_accessible_status(HostileStatus({"status": "running"}))
        self.assertIn("System state: Corrupt or unreadable state", text)
        self.assertIn("Economic edge: unproven", text)

    def test_unknown_reservation_state_is_not_announced_as_valid_exposure(self):
        text = format_accessible_status(
            {
                "status": "running",
                "state_format": "canonical_journal",
                "symbol": "SIM",
                "initial_cash": "1000",
                "fills": {},
                "active_reservations": [
                    {"state": "FILLED", "remaining": {"CASH:USD": "0"}},
                    {"state": float("nan"), "remaining": {"CASH:USD": "1"}},
                ],
            }
        )
        self.assertIn(
            "Active reservations: unavailable; one or more reservation entries are malformed",
            text,
        )
        self.assertIn("Structurally readable reservation entries: 0", text)
        self.assertGreaterEqual(
            text.count("Reservation state: unavailable; malformed value"),
            2,
        )

    def test_dict_subclass_reservation_does_not_execute_overridden_get(self):
        class HostileDict(dict):
            def get(self, *args, **kwargs):
                raise AssertionError("malformed mapping must not execute overridden get")

        text = format_accessible_status(
            {
                "status": "running",
                "state_format": "canonical_journal",
                "symbol": "SIM",
                "initial_cash": "1000",
                "fills": {},
                "active_reservations": [HostileDict()],
            }
        )
        self.assertIn("Reservation detail: unavailable; malformed state", text)
        self.assertIn("Structurally readable reservation entries: 0", text)

    def test_hostile_reservation_values_cannot_crash_status_surface(self):
        class HostileText:
            def __str__(self):
                raise AssertionError("malformed state must not execute __str__")

        text = format_accessible_status(
            {
                "status": "running",
                "state_format": "canonical_journal",
                "symbol": "SIM",
                "initial_cash": "1000",
                "fills": {},
                "active_reservations": [
                    {
                        "state": HostileText(),
                        "remaining": {
                            HostileText(): "25.00",
                            "USD": HostileText(),
                            "EUR": "10.00",
                        },
                    }
                ],
            }
        )

        self.assertIn(
            "Active reservations: unavailable; one or more reservation entries are malformed",
            text,
        )
        self.assertIn("Structurally readable reservation entries: 0", text)
        self.assertIn("Reservation state: unavailable; malformed value", text)
        self.assertIn("Reservation resource detail: unavailable; malformed value", text)
        self.assertIn("Reserved EUR: 10.00; state: Unavailable", text)
        self.assertIn("Economic edge: unproven", text)

    def test_hostile_nested_status_containers_fail_closed_without_execution(self):
        class HostileDict(dict):
            def __len__(self):
                raise AssertionError("malformed nested mapping must not execute __len__")

            def get(self, *args, **kwargs):
                raise AssertionError("malformed nested mapping must not execute get")

        class HostileList(list):
            def __iter__(self):
                raise AssertionError("malformed reservation list must not execute iteration")

        status = {
            "status": "running",
            "state_format": "canonical_journal",
            "symbol": "SIM",
            "initial_cash": "1000",
            "fills": HostileDict(),
            "active_reservations": HostileList(),
        }
        text = format_accessible_status(status, HostileDict())

        self.assertIn("Recorded fills: Unavailable", text)
        self.assertIn("Active reservations: unavailable; malformed state", text)
        self.assertIn("Economic report: unavailable; malformed state", text)
        self.assertIn("Economic edge: unproven", text)

    def test_cli_accessible_status_after_simulation(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            output = io.StringIO()
            with patch("sys.argv", ["autotrade", "--state-dir", directory, "--accessible-status"]):
                with patch("sys.stdout", output):
                    self.assertEqual(main(), 0)
            text = output.getvalue()
            self.assertIn("Mode: simulation only", text)
            self.assertIn("Replay verification: passed", text)
            self.assertIn("Instrument: SIM", text)
            self.assertIn("Economic edge: unproven", text)
            self.assertNotIn("{", text)


if __name__ == "__main__":
    unittest.main()
