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

    def test_malformed_nested_state_is_reported_without_crashing(self):
        text = format_accessible_status(
            {
                "status": "running",
                "symbol": "SIM",
                "initial_cash": "1000",
                "evidence_count": 1,
                "fills": [],
                "state_format": "canonical_journal",
                "episode_id": "episode-1",
                "session_status": "RUNNING",
                "active_reservations": [
                    {"remaining": None, "state": "ACTIVE"},
                    "corrupt-reservation",
                ],
            },
            {
                "final_equity": "1000",
                "net_pnl": "0",
                "total_fees": "0",
                "turnover": "0",
                "max_drawdown": "0",
                "reconciled": False,
                "cash_buckets": {"currency": "USD", "account_cash": "1000"},
            },
        )
        self.assertIn("Recorded fills: Unavailable", text)
        self.assertIn(
            "Active reservations: unavailable; one or more reservation entries are malformed",
            text,
        )
        self.assertIn("Structurally readable reservation entries: 0", text)
        self.assertNotIn("Active reservations: 2", text)
        self.assertIn("Reservation 1: unavailable; state: ACTIVE", text)
        self.assertIn("Reservation 2: unavailable", text)
        self.assertIn("Гроші на рахунку (USD): 1000", text)
        self.assertIn("Розраховані кошти: Unavailable", text)
        self.assertIn("Нереалізований прибуток/збиток: Unavailable", text)
        self.assertIn("Economic edge: unproven", text)

    def test_partial_reservation_corruption_does_not_announce_raw_list_count(self):
        text = format_accessible_status(
            {
                "status": "running",
                "state_format": "canonical_journal",
                "fills": {},
                "active_reservations": [
                    {
                        "remaining": {"CASH:USD": "10"},
                        "state": "WORKING",
                    },
                    {"remaining": None, "state": "UNKNOWN"},
                    "corrupt",
                ],
            }
        )
        self.assertIn(
            "Active reservations: unavailable; one or more reservation entries are malformed",
            text,
        )
        self.assertIn("Structurally readable reservation entries: 1", text)
        self.assertNotIn("Active reservations: 3", text)
        self.assertIn("Reserved CASH:USD: 10; state: WORKING", text)
        self.assertIn(
            "Action required: inspect or restore reservation state before relying on exposure status",
            text,
        )

    def test_hostile_value_objects_are_not_executed_by_accessible_status(self):
        class TrapText(str):
            def __str__(self):
                raise AssertionError("caller-controlled __str__ executed")

        class TrapDict(dict):
            def get(self, *args, **kwargs):
                raise AssertionError("caller-controlled get executed")

            def items(self):
                raise AssertionError("caller-controlled items executed")

            def __len__(self):
                raise AssertionError("caller-controlled len executed")

        text = format_accessible_status(
            {
                "status": "running",
                "symbol": TrapText("SIM"),
                "initial_cash": "1000",
                "evidence_count": 1,
                "fills": TrapDict({"fill-1": {}}),
                "state_format": "canonical_journal",
                "session_status": "RUNNING",
                "active_reservations": TrapDict(),
            }
        )
        self.assertIn("Instrument: Unavailable", text)
        self.assertIn("Recorded fills: Unavailable", text)
        self.assertIn("Active reservations: unavailable", text)

    def test_non_dict_status_fails_closed_as_corrupt(self):
        class TrapDict(dict):
            def get(self, *args, **kwargs):
                raise AssertionError("caller-controlled get executed")

        text = format_accessible_status(TrapDict({"status": "running"}))
        self.assertIn("System state: Corrupt or unreadable state", text)
        self.assertIn(
            "Action required: inspect or restore the simulated state before continuing",
            text,
        )
        self.assertIn("Economic edge: unproven", text)

    def test_unknown_state_fails_closed_as_corrupt(self):
        text = format_accessible_status(
            {
                "status": "future_state",
                "symbol": "SIM",
                "economic_edge_claim": "PROVEN",
            }
        )
        self.assertIn("System state: Corrupt or unreadable state", text)
        self.assertIn("Replay verification: unavailable", text)
        self.assertIn(
            "Action required: inspect or restore the simulated state before continuing",
            text,
        )
        self.assertNotIn("Instrument: SIM", text)
        self.assertIn("Economic edge: unproven", text)

    def test_explicit_unknown_state_format_fails_closed_before_financial_details(self):
        text = format_accessible_status(
            {
                "status": "running",
                "state_format": "future_journal",
                "symbol": "SIM",
                "initial_cash": "999999",
                "cash": "999999",
                "position": "999",
                "fills": {},
            }
        )
        self.assertIn("System state: Corrupt or unreadable state", text)
        self.assertIn("Replay verification: unavailable", text)
        self.assertNotIn("Instrument: SIM", text)
        self.assertNotIn("Initial capital: 999999", text)
        self.assertNotIn("Position (shares): 999", text)
        self.assertIn("Economic edge: unproven", text)

    def test_absent_state_format_preserves_legacy_status_compatibility(self):
        text = format_accessible_status(
            {
                "status": "running",
                "symbol": "SIM",
                "initial_cash": "1000",
                "fills": {},
            }
        )
        self.assertIn("System state: Running", text)
        self.assertIn("Instrument: SIM", text)
        self.assertIn("Initial capital: 1000", text)

    def test_hostile_enum_subclasses_are_rejected_before_equality_callbacks(self):
        class TrapEnum(str):
            def __eq__(self, other):
                raise AssertionError("caller-controlled equality executed")

            def __hash__(self):
                return str.__hash__(self)

        running = format_accessible_status(
            {
                "status": "running",
                "state_format": TrapEnum("canonical_journal"),
                "session_status": TrapEnum("BLOCKED"),
                "fills": {},
            },
            {
                "valuation_status": TrapEnum("MARK_UNAVAILABLE"),
                "reconciled": False,
            },
        )
        self.assertIn("System state: Running", running)
        self.assertNotIn("Episode:", running)
        self.assertNotIn("Blocked reason:", running)
        self.assertNotIn("Portfolio valuation and profit or loss: unavailable", running)
        self.assertIn("Economic edge: unproven", running)

    def test_control_text_cannot_break_plain_text_status_contract(self):
        text = format_accessible_status(
            {
                "status": "running",
                "symbol": "SIM\x1b[31m",
                "initial_cash": "1000\nforged status",
                "evidence_count": 1,
                "fills": {},
                "state_format": "canonical_journal",
                "episode_id": "episode\rforged",
                "session_status": "RUNNING",
                "active_reservations": [
                    {
                        "remaining": {"CASH:USD": "10\tforged"},
                        "state": "ACTIVE",
                    }
                ],
            }
        )
        self.assertIn("Instrument: Unavailable", text)
        self.assertIn("Initial capital: Unavailable", text)
        self.assertIn("Episode: Unavailable", text)
        self.assertIn("Reserved CASH:USD: Unavailable; state: ACTIVE", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("forged status", text)
        self.assertNotIn("\t", text)

    def test_unicode_format_controls_cannot_reorder_or_split_accessible_status(self):
        text = format_accessible_status(
            {
                "status": "running",
                "symbol": "SIM\u202eUSD",
                "initial_cash": "1000\u2028forged",
                "evidence_count": 1,
                "fills": {},
                "state_format": "canonical_journal",
                "episode_id": "episode\u2066spoof",
                "session_status": "RUNNING",
                "active_reservations": [
                    {
                        "remaining": {"CASH:USD": "10\u2029forged"},
                        "state": "ACTIVE",
                    }
                ],
            }
        )
        self.assertIn("Instrument: Unavailable", text)
        self.assertIn("Initial capital: Unavailable", text)
        self.assertIn("Episode: Unavailable", text)
        self.assertIn("Reserved CASH:USD: Unavailable; state: ACTIVE", text)
        self.assertNotIn("\u202e", text)
        self.assertNotIn("\u2028", text)
        self.assertNotIn("\u2029", text)
        self.assertNotIn("\u2066", text)
        self.assertNotIn("forged", text)

    def test_nonfinite_float_values_are_unavailable_not_operator_facts(self):
        text = format_accessible_status(
            {
                "status": "running",
                "symbol": "SIM",
                "initial_cash": float("nan"),
                "evidence_count": float("inf"),
                "fills": {},
            },
            {
                "final_equity": float("-inf"),
                "net_pnl": float("nan"),
                "total_fees": "0",
                "turnover": "0",
                "max_drawdown": "0",
                "reconciled": False,
            },
        )
        self.assertIn("Initial capital: Unavailable", text)
        self.assertIn("Recorded evidence items: Unavailable", text)
        self.assertIn("Final equity: Unavailable", text)
        self.assertIn("Net profit or loss: Unavailable", text)
        self.assertNotIn(" nan", text.lower())
        self.assertNotIn(" inf", text.lower())

    def test_accessible_cli_returns_failure_for_unknown_state(self):
        output = io.StringIO()
        canonical = {
            "status": {"status": "future_state"},
            "economic_report": None,
        }
        with patch(
            "mvp.autotrade_mvp.cli._read_canonical_state",
            return_value=canonical,
        ):
            with patch("sys.stdout", output):
                self.assertEqual(
                    main(["--state-dir", "unused", "--accessible-status"]),
                    2,
                )
        text = output.getvalue()
        self.assertIn("System state: Corrupt or unreadable state", text)
        self.assertIn("Economic edge: unproven", text)

    def test_accessible_cli_keeps_known_nonerror_state_successful(self):
        output = io.StringIO()
        canonical = {
            "status": {"status": "not_started"},
            "economic_report": None,
        }
        with patch(
            "mvp.autotrade_mvp.cli._read_canonical_state",
            return_value=canonical,
        ):
            with patch("sys.stdout", output):
                self.assertEqual(
                    main(["--state-dir", "unused", "--accessible-status"]),
                    0,
                )
        self.assertIn("System state: Not started", output.getvalue())

    def test_accessible_cli_preserves_status_when_optional_economics_are_malformed(self):
        output = io.StringIO()
        with patch(
            "mvp.autotrade_mvp.cli._read_canonical_state",
            return_value=None,
        ):
            with patch(
                "mvp.autotrade_mvp.cli._legacy_status",
                return_value={
                    "status": "running",
                    "symbol": "SIM",
                    "initial_cash": "1000",
                    "evidence_count": 1,
                    "fills": {},
                    "replay_verified": True,
                },
            ):
                with patch(
                    "mvp.autotrade_mvp.cli.get_economic_report",
                    side_effect=TypeError("malformed optional economics"),
                ):
                    with patch("sys.stdout", output):
                        self.assertEqual(
                            main(["--state-dir", "unused", "--accessible-status"]),
                            0,
                        )
        text = output.getvalue()
        self.assertIn("System state: Running", text)
        self.assertIn("Replay verification: passed", text)
        self.assertNotIn("Final equity:", text)
        self.assertIn("Economic edge: unproven", text)

    def test_hostile_mapping_keys_do_not_execute_equality_during_status_read(self):
        class TrapKey:
            def __init__(self, text):
                self.text = text
                self.armed = False

            def __hash__(self):
                return hash(self.text)

            def __eq__(self, other):
                if self.armed:
                    raise AssertionError("caller-controlled key equality executed")
                return False

        fills_key = TrapKey("fills")
        remaining_key = TrapKey("remaining")
        reconciled_key = TrapKey("reconciled")
        status = {
            "status": "running",
            fills_key: {},
            "symbol": "SIM",
            "state_format": "canonical_journal",
            "active_reservations": [
                {
                    remaining_key: {},
                    "state": "ACTIVE",
                }
            ],
        }
        economic_report = {reconciled_key: True}
        fills_key.armed = True
        remaining_key.armed = True
        reconciled_key.armed = True

        text = format_accessible_status(status, economic_report)
        self.assertIn("System state: Running", text)
        self.assertIn("Instrument: SIM", text)
        self.assertIn("Recorded fills: 0", text)
        self.assertIn("Reservation 1: unavailable; state: ACTIVE", text)
        self.assertIn("Economic reconciliation: not confirmed", text)


if __name__ == "__main__":
    unittest.main()
