"""End-to-end operator reads of durable simulation, including crash boundaries."""

from contextlib import closing
from decimal import localcontext, ROUND_UP, ROUND_DOWN
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import simulation_session
from mvp.autotrade_mvp.accounting import book_external_cash_flow
from mvp.autotrade_mvp.cli import get_status, main
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.pipeline import run_vertical_slice
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.simulation_session import ACCOUNT, ENVIRONMENT, PROVIDER, run_canonical_simulation
from mvp.autotrade_mvp.simulation_status import inspect_canonical_simulation
from research.autotrade_research.artifacts.resource_lock import ResourceLock


NOW = "2026-10-01T18:30:00Z"
BUY = ["100", "101", "103"]


def run(directory, prices=BUY, **kwargs):
    return run_canonical_simulation(prices, directory, episode_id="operator", now=NOW, **kwargs)


def dump(directory):
    with closing(sqlite3.connect(Path(directory) / "journal.sqlite3")) as connection:
        return "\n".join(connection.iterdump())


def command(directory, *args):
    output, error = io.StringIO(), io.StringIO()
    with patch("sys.stdout", output), patch("sys.stderr", error):
        code = main(["--state-dir", str(directory), *args])
    return code, output.getvalue(), error.getvalue()


class SimulationOperatorTests(unittest.TestCase):
    def test_missing_directory_is_not_created_by_status(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "never-started"
            self.assertEqual(get_status(root), {"status": "not_started"})
            self.assertFalse(root.exists())

    def test_buy_status_uses_journal_and_retains_unconfirmed_order(self):
        with TemporaryDirectory() as directory:
            result = run(directory)
            status = get_status(directory)
            self.assertEqual(status["status"], "awaiting_order_reconciliation")
            self.assertEqual(status["session_status"], result["status"])
            self.assertEqual(status["cash"], "896.897")
            self.assertEqual(status["position"], "1")
            self.assertTrue(status["reconciled"])
            self.assertTrue(status["replay_verified"])
            self.assertEqual(status["new_outbound_requests"], 0)
            self.assertEqual(status["active_reservations"][0]["remaining"], {"CASH:USD": "0"})
            self.assertEqual(status["active_reservations"][0]["state"], "WORKING")

    def test_buy_report_does_not_invent_a_mark_or_pnl(self):
        with TemporaryDirectory() as directory:
            run(directory)
            code, output, error = command(directory, "--economic-report")
            self.assertEqual((code, error), (0, ""))
            report = json.loads(output)
            self.assertEqual(report["total_fees"], "0.103")
            self.assertEqual(report["turnover"], "103")
            self.assertEqual(report["trade_count"], 1)
            self.assertIsNone(report["net_pnl"])
            self.assertIsNone(report["final_equity"])
            self.assertEqual(report["valuation_status"], "MARK_UNAVAILABLE")
            self.assertEqual(report["economic_edge_claim"], "UNPROVEN_SIMULATION_ONLY")

    def test_hold_is_completed_with_cash_only_valuation(self):
        with TemporaryDirectory() as directory:
            run(directory, ["100", "101"])
            read = inspect_canonical_simulation(directory)
            self.assertEqual(read["status"]["status"], "completed")
            self.assertEqual(read["status"]["session_status"], "HOLD")
            self.assertEqual(read["status"]["active_reservations"], [])
            self.assertEqual(read["economic_report"]["final_equity"], "1000")
            self.assertEqual(read["economic_report"]["net_pnl"], "0")
            self.assertEqual(read["economic_report"]["trade_count"], 0)

    def test_risk_rejected_is_completed_without_exposure(self):
        with TemporaryDirectory() as directory:
            run(directory, ["1000", "1001", "1003"])
            status = get_status(directory)
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["session_status"], "RISK_REJECTED")
            self.assertEqual(status["fills"], {})
            self.assertEqual(status["position"], "0")

    def test_unknown_retains_reservation_and_reports_no_fill(self):
        with TemporaryDirectory() as directory:
            run(directory, fault_after_send=True)
            status = get_status(directory)
            self.assertEqual(status["status"], "needs_recovery")
            self.assertEqual(status["session_status"], "UNKNOWN")
            self.assertFalse(status["reconciled"])
            self.assertEqual(status["fills"], {})
            self.assertEqual(status["active_reservations"][0]["remaining"], {"CASH:USD": "103.103"})
            self.assertEqual(status["new_outbound_requests"], 0)

    def test_unknown_cannot_produce_an_economic_report(self):
        with TemporaryDirectory() as directory:
            run(directory, fault_after_send=True)
            before = dump(directory)
            code, output, error = command(directory, "--economic-report")
            self.assertEqual((code, output), (2, ""))
            self.assertNotIn("Traceback", error)
            self.assertEqual(dump(directory), before)

    def test_crash_after_started_before_admission_is_not_a_completed_hold(self):
        with TemporaryDirectory() as directory:
            with patch.object(simulation_session.AuthorityService, "register_policy", side_effect=RuntimeError("crash")):
                with self.assertRaises(RuntimeError):
                    run(directory)
            status = get_status(directory)
            self.assertEqual(status["status"], "needs_recovery")
            self.assertFalse(status["reconciled"])
            self.assertEqual(status["fills"], {})

    def test_crash_after_economic_commit_is_not_a_confirmed_fill(self):
        with TemporaryDirectory() as directory:
            original = simulation_session._reconcile
            def crash(provider, book, now, **kwargs):
                if kwargs.get("fill") is not None:
                    raise RuntimeError("post-fill reconciliation crash")
                return original(provider, book, now, **kwargs)
            with patch.object(simulation_session, "_reconcile", crash):
                with self.assertRaises(RuntimeError):
                    run(directory)
            status = get_status(directory)
            self.assertEqual(status["status"], "needs_recovery")
            self.assertEqual(status["cash"], "896.897")
            self.assertEqual(status["position"], "1")
            self.assertFalse(status["reconciled"])
            self.assertEqual(status["fills"], {})

    def test_orphaned_bootstrap_is_recovery_state(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book = DurableProviderEconomicBook(store, provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT)
            book.append(book_external_cash_flow(transaction_id="seed", cause_event_id="seed", currency="USD", amount="1000"))
            status = get_status(directory)
            self.assertEqual(status["status"], "needs_recovery")
            self.assertIsNone(status["episode_id"])
            self.assertEqual(status["reason"], "orphaned_durable_state_requires_reconciliation")

    def test_all_reads_preserve_every_durable_table_and_cannot_dispatch(self):
        with TemporaryDirectory() as directory:
            run(directory)
            before = dump(directory)
            with patch.object(simulation_session.GuardedDispatcher, "dispatch", side_effect=AssertionError("read sent an order")):
                for mode in ("--status", "--accessible-status", "--economic-report", "--history"):
                    with self.subTest(mode=mode):
                        self.assertEqual(command(directory, mode)[0], 0)
                        self.assertEqual(dump(directory), before)

    def test_history_is_bounded_ordered_and_payload_free(self):
        with TemporaryDirectory() as directory:
            run(directory, fault_after_send=True)
            code, output, _ = command(directory, "--history", "--history-limit", "2")
            self.assertEqual(code, 0)
            history = json.loads(output)
            self.assertEqual(len(history["events"]), 2)
            self.assertEqual(history["events"][-1]["event_type"], "SubmissionUnknown")
            self.assertEqual(history["events"][-1]["journal_sequence"], history["journal_sequence"])
            for word in ("owner_token", "request_hash", "payload", "response", "authority_policy"):
                self.assertNotIn(word, output)

    def test_journal_advance_discards_mixed_cut(self):
        with TemporaryDirectory() as directory:
            run(directory)
            original = JournalStore.current_journal_sequence
            calls = 0
            def advancing(store):
                nonlocal calls
                calls += 1
                return original(store) + (1 if calls > 1 else 0)
            with patch.object(JournalStore, "current_journal_sequence", advancing):
                status = get_status(directory)
            self.assertEqual(status["status"], "busy")
            self.assertNotIn("cash", status)

    def test_active_writer_returns_busy_without_waiting_or_reading_partial_state(self):
        with TemporaryDirectory() as directory:
            run(directory)
            before = dump(directory)
            with ResourceLock(Path(directory) / ".canonical-simulation.lock"):
                code, output, error = command(directory, "--accessible-status")
                self.assertEqual((code, error), (2, ""))
                self.assertIn("State is changing; read again", output)
                self.assertNotIn("Cash (USD)", output)
            self.assertEqual(dump(directory), before)

    def test_later_journal_activity_cannot_inherit_historical_reconciliation(self):
        with TemporaryDirectory() as directory:
            run(directory)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            store.append_event({"event_id": "later-host-event", "event_type": "HostStopped",
                                "aggregate_type": "host", "aggregate_id": "local-simulation",
                                "aggregate_version": "1", "payload": {}, "payload_hash": payload_digest({}),
                                "committed_at": NOW})
            status = get_status(directory)
            self.assertEqual(status["status"], "needs_recovery")
            self.assertFalse(status["reconciled"])
            self.assertEqual(status["reason"], "journal_advanced_after_session_completion")
            self.assertEqual(command(directory, "--economic-report")[0], 2)

    def test_completed_summary_cannot_override_replayed_cash(self):
        self._invalid_completion({"cash": "999999"})

    def test_completed_summary_cannot_override_replayed_position(self):
        self._invalid_completion({"position": "0"})

    def test_completed_summary_cannot_cross_episode_identity(self):
        self._invalid_completion({"episode_id": "another-episode"})

    def test_completed_summary_cannot_cross_environment(self):
        self._invalid_completion({"environment": "LIVE"})

    def test_completed_summary_cannot_claim_reconciliation_without_evidence(self):
        self._invalid_completion({"reconciliation_event_id": "missing"})

    def test_completed_summary_cannot_treat_acknowledgement_as_a_fill(self):
        self._invalid_completion({"fill_id": "acknowledgement-only"})

    def test_completed_summary_cannot_lie_about_decision(self):
        self._invalid_completion({"decision": "HOLD"})

    def test_completed_summary_cannot_self_assert_terminal_order(self):
        self._invalid_completion({"status": "FILLED"})

    def test_completed_summary_cannot_use_boolean_money(self):
        self._invalid_completion({"cash": True})

    def test_completed_summary_cannot_use_unbounded_money(self):
        self._invalid_completion({"cash": "1e999999999"})

    def _invalid_completion(self, changes):
        original = simulation_session._event
        def altered(store, kind, episode_id, payload, now):
            if kind == "SimulationSessionCompleted":
                payload = {**payload, **changes}
            return original(store, kind, episode_id, payload, now)
        with TemporaryDirectory() as directory:
            with patch.object(simulation_session, "_event", altered):
                run(directory)
            before = dump(directory)
            self.assertEqual(get_status(directory)["status"], "corrupt")
            self.assertEqual(dump(directory), before)

    def test_corrupt_journal_does_not_fall_back_to_valid_legacy_checkpoint(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 103], directory)
            (Path(directory) / "journal.sqlite3").write_bytes(b"not a database")
            self.assertEqual(get_status(directory)["status"], "corrupt")
            self.assertEqual(command(directory, "--economic-report")[0], 2)

    def test_journal_payload_tamper_is_reported_as_corrupt(self):
        with TemporaryDirectory() as directory:
            run(directory)
            with closing(sqlite3.connect(Path(directory) / "journal.sqlite3")) as connection:
                connection.execute("UPDATE events SET payload_json='{}' WHERE event_type='SimulationSessionCompleted'")
                connection.commit()
            self.assertEqual(get_status(directory)["status"], "corrupt")

    def test_older_schema_is_not_migrated_by_status(self):
        with TemporaryDirectory() as directory:
            run(directory)
            with closing(sqlite3.connect(Path(directory) / "journal.sqlite3")) as connection:
                connection.execute("DELETE FROM schema_migrations WHERE version=?", (JournalStore.SCHEMA_VERSION,))
                connection.commit()
            before = dump(directory)
            self.assertEqual(get_status(directory)["status"], "corrupt")
            self.assertEqual(dump(directory), before)

    def test_symlink_journal_is_not_followed(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as target:
            run(target)
            try:
                (Path(directory) / "journal.sqlite3").symlink_to(Path(target) / "journal.sqlite3")
            except OSError:
                self.skipTest("platform does not allow symlink creation")
            before = dump(target)
            self.assertEqual(get_status(directory)["status"], "corrupt")
            self.assertEqual(dump(target), before)

    def test_path_with_spaces_and_uri_punctuation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "state with spaces # percent%"
            run(root)
            self.assertEqual(get_status(root)["cash"], "896.897")

    def test_accessible_buy_states_units_unknown_valuation_and_required_action(self):
        with TemporaryDirectory() as directory:
            run(directory)
            code, output, error = command(directory, "--accessible-status")
            self.assertEqual((code, error), (0, ""))
            for text in ("Cash (USD): 896.897", "Position (shares): 1", "Awaiting order reconciliation",
                         "no retained current market mark", "Economic edge: unproven"):
                self.assertIn(text, output)
            self.assertNotIn("\x1b", output)
            self.assertNotIn("{", output)

    def test_accessible_unknown_states_reserved_cash_and_recovery(self):
        with TemporaryDirectory() as directory:
            run(directory, fault_after_send=True)
            code, output, _ = command(directory, "--accessible-status")
            self.assertEqual(code, 0)
            self.assertIn("Reserved CASH:USD: 103.103", output)
            self.assertIn("Session outcome: UNKNOWN", output)
            self.assertIn("Action required: recovery or reconciliation", output)
            self.assertNotIn("Economic reconciliation: passed", output)

    def test_accessible_corrupt_is_copyable_and_returns_failure_code(self):
        with TemporaryDirectory() as directory:
            (Path(directory) / "journal.sqlite3").write_bytes(b"bad")
            code, output, error = command(directory, "--accessible-status")
            self.assertEqual((code, error), (2, ""))
            self.assertIn("Corrupt or unreadable state", output)
            self.assertNotIn("Traceback", output)

    def test_legacy_and_canonical_directories_cannot_be_combined_by_cli(self):
        with TemporaryDirectory() as directory:
            run(directory)
            before = dump(directory)
            self.assertEqual(command(directory, "--multi-episode", "--prices", "100,101,103")[0], 2)
            self.assertEqual(dump(directory), before)
            self.assertFalse((Path(directory) / "checkpoint.json").exists())
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 103], directory)
            before = dump(directory)
            self.assertEqual(command(directory, "--canonical-simulation")[0], 2)
            self.assertEqual(dump(directory), before)

    def test_conflicting_modes_fail_before_any_state_creation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "unstarted"
            for args in (("--status", "--economic-report"), ("--history", "--canonical-simulation"),
                         ("--multi-episode", "--accessible-status")):
                with self.subTest(args=args), patch("sys.stderr", io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        main(["--state-dir", str(root), *args])
                    self.assertEqual(raised.exception.code, 2)
                    self.assertFalse(root.exists())

    def test_fault_and_time_options_cannot_be_silently_ignored(self):
        for args in (("--status", "--fault-after-send"), ("--at", NOW)):
            with self.subTest(args=args), patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(list(args))

    def test_history_limit_is_bounded_before_state_open(self):
        for limit in ("0", "-1", "1001"):
            with self.subTest(limit=limit), patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(["--history", "--history-limit", limit])

    def test_malformed_legacy_json_is_corrupt_without_traceback(self):
        for text in ("[]", "null", '{"initial_cash":NaN}', '{"symbol":"SIM","symbol":"OTHER"}'):
            with self.subTest(text=text), TemporaryDirectory() as directory:
                (Path(directory) / "checkpoint.json").write_text(text, encoding="utf-8")
                self.assertEqual(get_status(directory)["status"], "corrupt")

    def test_invalid_prices_return_an_operator_error_without_starting_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "not-started"
            code, output, error = command(root, "--canonical-simulation", "--prices", "NaN,1")
            self.assertEqual((code, output), (2, ""))
            self.assertNotIn("Traceback", error)
            self.assertFalse(root.exists())

    def test_different_decimal_contexts_do_not_round_operator_cash(self):
        with TemporaryDirectory() as directory:
            run(directory)
            for rounding in (ROUND_UP, ROUND_DOWN):
                with self.subTest(rounding=rounding), localcontext() as context:
                    context.prec = 2
                    context.rounding = rounding
                    status = get_status(directory)
                    self.assertEqual(status.get("cash"), "896.897")


if __name__ == "__main__":
    unittest.main()
