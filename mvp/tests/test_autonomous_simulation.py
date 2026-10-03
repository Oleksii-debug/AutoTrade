"""Provider-free whole-loop acceptance, recovery and adversarial regressions."""
from decimal import Decimal, localcontext, Inexact, Rounded, ROUND_CEILING
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.accounting import book_external_cash_flow
from mvp.autotrade_mvp.authority import AuthoritativeRiskSnapshot, AuthorityConflict
from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.risk import RiskPolicy
from mvp.autotrade_mvp.simulated_provider import SimulatedProvider
from mvp.autotrade_mvp.simulation_session import (
    run_autonomous_simulation, ACCOUNT, PROVIDER, ENVIRONMENT, INSTRUMENT,
)
from mvp.autotrade_mvp.zero_network import deny_python_network

NOW = "2026-10-03T00:00:00Z"
PRICES = ["100", "101", "103", "102", "100", "100", "101", "103"]


def run(directory, prices=PRICES, **kwargs):
    return run_autonomous_simulation(list(prices), directory, run_id="acceptance", now=NOW, **kwargs)


class AutonomousSimulationTests(unittest.TestCase):
    def test_full_buy_reduce_buy_loop_uses_canonical_oms_and_accounting(self):
        with TemporaryDirectory() as d:
            result = run(d)
            self.assertEqual(result["status"], "COMPLETED")
            self.assertEqual(result["mode"], "ZERO")
            self.assertEqual(result["completed_episodes"], 8)
            self.assertEqual(result["new_outbound_requests"], 3)
            self.assertEqual(result["cash"], "895.696")
            self.assertEqual(result["position"], "1")
            self.assertEqual(result["economic_edge_status"], "INCONCLUSIVE")
            self.assertEqual(result["decisions"][4]["decision"], "REDUCE")
            store = JournalStore(Path(d) / "journal.sqlite3")
            oms = DurableOrderBookProjection(store, provider_id=PROVIDER, account_id=ACCOUNT,
                environment=ENVIRONMENT, host_id="test", owner_epoch="1")
            self.assertEqual([s.state for s in oms.snapshots], ["FILLED"] * 3)
            self.assertEqual(len(oms.effective_fills()), 3)
            book = DurableProviderEconomicBook(store, provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT)
            self.assertEqual(book.position(INSTRUMENT), Decimal("1"))
            risks = store.load_events_by_aggregate_type("risk_decision")
            for event in risks:
                evidence = event["payload"]["authoritative_risk_snapshot"]
                self.assertIn("resolved_risk_policy", evidence)
                self.assertTrue(evidence["evidence_refs"]["MARKET"].startswith("valuation:sha256:"))
                self.assertEqual(evidence["resolved_risk_policy"]["resolved_journal_sequence_cut"], evidence["journal_sequence_cut"])

    def test_consecutive_buy_signals_do_not_add_duplicate_exposure(self):
        with TemporaryDirectory() as d:
            result = run(d, [str(x) for x in range(100, 120)])
            self.assertEqual(result["position"], "1")
            self.assertEqual(result["new_outbound_requests"], 1)
            self.assertGreater(sum(x["decision"] == "NO_TRADE" for x in result["decisions"]), 10)

    def test_120_episode_unattended_scenario_and_resume_match_fresh_execution(self):
        prices = (["100", "101", "103", "102", "100", "98", "100", "103"] * 15)
        with TemporaryDirectory() as continuous, TemporaryDirectory() as restarted:
            full = run(continuous, prices)
            partial = run(restarted, prices, stop_after_episodes=41)
            self.assertEqual(partial["status"], "PAUSED")
            resumed = run(restarted, prices)
            self.assertEqual(resumed["status"], "COMPLETED")
            self.assertEqual(resumed["completed_episodes"], 120)
            self.assertGreater(full["new_outbound_requests"], 20)
            self.assertEqual(resumed["decisions"], full["decisions"])
            self.assertEqual(resumed["cash"], full["cash"])
            self.assertEqual(resumed["position"], full["position"])
            self.assertEqual(partial["new_outbound_requests"] + resumed["new_outbound_requests"], full["new_outbound_requests"])
            again = run(restarted, prices)
            self.assertEqual(again["new_outbound_requests"], 0)

    def test_ambient_decimal_context_does_not_change_end_to_end_decisions(self):
        with TemporaryDirectory() as first, TemporaryDirectory() as second:
            reference = run(first)
            with localcontext() as c:
                c.prec = 2
                c.rounding = ROUND_CEILING
                c.traps[Inexact] = c.traps[Rounded] = True
                other = run(second)
            self.assertEqual(reference["decisions"], other["decisions"])

    def test_unknown_lost_response_holds_reservation_and_never_retries(self):
        with TemporaryDirectory() as d:
            first = run(d, fault_at_episode=3)
            self.assertEqual(first["status"], "UNKNOWN")
            self.assertEqual(first["new_outbound_requests"], 1)
            store = JournalStore(Path(d) / "journal.sqlite3")
            before = store.current_journal_sequence()
            reservations = DurableReservationBook(store, environment=ENVIRONMENT, account_id=ACCOUNT)
            self.assertEqual(reservations.active()[0].state, "UNKNOWN")
            self.assertEqual(reservations.total_reserved("CASH:USD"), Decimal("103.103"))
            with patch.object(SimulatedProvider, "transport_send", side_effect=AssertionError("no spontaneous resend")):
                resumed = run(d, fault_at_episode=3)
            self.assertEqual(resumed["status"], "UNKNOWN")
            self.assertEqual(resumed["new_outbound_requests"], 0)
            self.assertEqual(store.current_journal_sequence(), before)

    def test_crash_in_sending_is_sticky_unknown_without_simulator_or_send(self):
        original = GuardedDispatcher._append
        def crash(dispatcher, **kwargs):
            value = original(dispatcher, **kwargs)
            if kwargs["event_type"] == "SubmissionSending":
                raise RuntimeError("crash during SENDING")
            return value
        with TemporaryDirectory() as d:
            with patch.object(GuardedDispatcher, "_append", crash):
                first = run(d)
                self.assertEqual(first["status"], "UNKNOWN")
            store = JournalStore(Path(d) / "journal.sqlite3")
            before = store.current_journal_sequence()
            with patch.object(SimulatedProvider, "from_state", side_effect=AssertionError("UNKNOWN cannot create fresh simulated truth")):
                resumed = run(d)
            self.assertEqual(resumed["status"], "UNKNOWN")
            self.assertEqual(resumed["new_outbound_requests"], 0)
            self.assertEqual(store.current_journal_sequence(), before)

    def test_acknowledgement_without_fill_cannot_complete_or_book_money(self):
        original = SimulatedProvider.activity_fills
        def missing(provider):
            return [] if provider.outbound_request_count else original(provider)
        with TemporaryDirectory() as d:
            with patch.object(SimulatedProvider, "activity_fills", missing):
                with self.assertRaisesRegex(ValueError, "ACK is not fill"):
                    run(d)
            store = JournalStore(Path(d) / "journal.sqlite3")
            economic = DurableProviderEconomicBook(store, provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT)
            self.assertEqual(economic.cash("USD"), Decimal("1000"))
            self.assertEqual(economic.position(INSTRUMENT), Decimal("0"))
            self.assertEqual(run(d)["status"], "UNKNOWN")

    def test_emergency_stops_new_orders_and_keeps_next_decisions_running(self):
        with TemporaryDirectory() as d:
            result = run(d, emergency_at_episode=4)
            self.assertEqual(result["completed_episodes"], 8)
            self.assertEqual(result["new_outbound_requests"], 1)
            self.assertTrue(all(x["emergency"] for x in result["decisions"][3:]))
            self.assertTrue(all(x["status"] == "NO_TRADE" for x in result["decisions"][3:]))

    def test_rejected_hard_risk_proposals_continue_without_transport(self):
        import mvp.autotrade_mvp.simulation_session as session
        original = session._risk_policy()
        from dataclasses import replace
        stricter = replace(original, max_single_notional=Decimal("1"))
        with TemporaryDirectory() as d, patch.object(session, "_risk_policy", return_value=stricter):
            result = run(d)
            self.assertEqual(result["status"], "COMPLETED")
            self.assertEqual(result["new_outbound_requests"], 0)
            self.assertGreater(sum(x["status"] == "RISK_REJECTED" for x in result["decisions"]), 1)
            self.assertEqual(result["position"], "0")

    def test_zero_mode_denies_dns_connect_udp_and_suppressed_attempts(self):
        def udp():
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
                connection.sendto(b"x", ("127.0.0.1", 1))
        for action in (lambda: socket.getaddrinfo("localhost", 80),
                       lambda: socket.create_connection(("127.0.0.1", 1)),
                       udp):
            with self.subTest(action=action), self.assertRaisesRegex(RuntimeError, "ZERO mode"):
                with deny_python_network():
                    try:
                        action()
                    except RuntimeError:
                        pass

    def test_zero_loop_does_not_touch_model_gateway(self):
        with TemporaryDirectory() as d, patch("mvp.autotrade_mvp.model_gateway.route_model", side_effect=AssertionError("ZERO model invocation")):
            self.assertEqual(run(d)["status"], "COMPLETED")

    @unittest.skipUnless(hasattr(socket.socket, "sendmsg"), "sendmsg is platform specific")
    def test_zero_denies_unconnected_udp_sendmsg_before_io(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            with self.assertRaisesRegex(RuntimeError, "blocked network attempt"):
                with deny_python_network():
                    try:
                        connection.sendmsg([b"x"], [], 0, ("127.0.0.1", 1))
                    except RuntimeError:
                        pass

    def test_frozen_protocol_rejects_price_and_time_changes(self):
        with TemporaryDirectory() as d:
            run(d, stop_after_episodes=2)
            for changes in ({"prices": ["100", "102", "104"]}, {"now": "2026-10-03T00:00:01Z"}):
                with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "identity changed"):
                    args = dict(prices=PRICES, state_dir=d, run_id="acceptance", now=NOW)
                    args.update(changes)
                    run_autonomous_simulation(**args)

    def test_conflicting_canonical_economics_prevents_resume(self):
        with TemporaryDirectory() as d:
            run(d, stop_after_episodes=3)
            store = JournalStore(Path(d) / "journal.sqlite3")
            book = DurableProviderEconomicBook(store, provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT)
            book.append(book_external_cash_flow(transaction_id="foreign", cause_event_id="foreign", currency="USD", amount="1"))
            with self.assertRaisesRegex(ValueError, "conflicts with canonical economic"):
                run(d)

    def test_resume_rejects_changed_quantitative_policy_before_journal_mutation(self):
        from dataclasses import replace
        import mvp.autotrade_mvp.simulation_session as session
        with TemporaryDirectory() as d:
            run(d, stop_after_episodes=2)
            store = JournalStore(Path(d) / "journal.sqlite3")
            cut = store.current_journal_sequence()
            policy = replace(session._risk_policy(), max_single_notional=Decimal("999"))
            with patch.object(session, "_risk_policy", return_value=policy):
                with self.assertRaisesRegex(ValueError, "identity changed"):
                    run(d)
            self.assertEqual(store.current_journal_sequence(), cut)
            self.assertEqual(run(d, stop_after_episodes=2)["status"], "PAUSED")

    def test_operator_status_economics_and_accessible_read_are_durable_and_do_not_send(self):
        from mvp.autotrade_mvp.cli import get_status, get_economic_report
        from mvp.autotrade_mvp.accessibility import format_accessible_status
        with TemporaryDirectory() as d:
            run(d)
            store = JournalStore(Path(d) / "journal.sqlite3")
            before = store.current_journal_sequence()
            with patch.object(SimulatedProvider, "transport_send", side_effect=AssertionError("read cannot send")):
                status = get_status(d)
                report = get_economic_report(d)
                text = format_accessible_status(status, report)
            self.assertEqual(status["session_status"], "COMPLETED")
            self.assertEqual(status["completed_episodes"], 8)
            self.assertEqual(report["net_pnl"], "-1.304")
            self.assertIn("Autonomous episodes: 8 of 8", text)
            self.assertIn("Model mode: ZERO", text)
            self.assertEqual(store.current_journal_sequence(), before)


    def test_cli_pause_resume_and_unknown_exit_code(self):
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        import json
        from mvp.autotrade_mvp.cli import main
        with TemporaryDirectory() as d:
            args = ["--autonomous-simulation", "--prices", ",".join(PRICES),
                    "--state-dir", d, "--episode-id", "cli", "--at", NOW]
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(args + ["--stop-after-episodes", "3"]), 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "PAUSED")
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(args), 0)
            self.assertEqual(json.loads(output.getvalue())["completed_episodes"], 8)
        with TemporaryDirectory() as d, redirect_stdout(StringIO()) as output:
            args = ["--autonomous-simulation", "--prices", ",".join(PRICES),
                    "--state-dir", d, "--episode-id", "unknown", "--at", NOW, "--fault-episode", "3"]
            self.assertEqual(main(args), 2)
            self.assertEqual(json.loads(output.getvalue())["status"], "UNKNOWN")


    def test_pending_oms_order_blocks_next_episode(self):
        with TemporaryDirectory() as d:
            run(d, stop_after_episodes=2)
            store = JournalStore(Path(d) / "journal.sqlite3")
            oms = DurableOrderBookProjection(store, provider_id=PROVIDER, account_id=ACCOUNT,
                environment=ENVIRONMENT, host_id="test", owner_epoch="1")
            oms.create_order(event_key="foreign", client_order_id="foreign", instrument=INSTRUMENT,
                side="BUY", requested_quantity="1", committed_at=NOW)
            with self.assertRaisesRegex(ValueError, "OMS obligations"):
                run(d)


if __name__ == "__main__":
    unittest.main()
