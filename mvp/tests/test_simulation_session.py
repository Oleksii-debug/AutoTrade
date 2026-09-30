"""Product entrypoint checks for the canonical network-free simulation session."""

import json
import subprocess
import sys
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.pipeline import MovingAverageStrategy
from mvp.autotrade_mvp.simulation_session import (
    ACCOUNT, ENVIRONMENT, INSTRUMENT, PROVIDER, run_canonical_simulation,
)
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.accounting import book_external_cash_flow
from research.autotrade_research.artifacts.store import ArtifactStore


NOW = "2026-09-30T12:00:00Z"
BUY = ["100", "101", "103"]
HOLD = ["100", "101"]


class CanonicalSimulationSessionTests(unittest.TestCase):
    def test_orphaned_bootstrap_state_cannot_start_another_send(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            book = DurableProviderEconomicBook(
                store, provider_id=PROVIDER, account_id=ACCOUNT,
                environment=ENVIRONMENT,
            )
            book.append(book_external_cash_flow(
                transaction_id="seed-before-crash",
                cause_event_id="bootstrap-before-crash",
                currency="USD", amount="1000",
            ))
            result = run_canonical_simulation(BUY, directory, episode_id="orphan", now=NOW)
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertEqual(result["new_outbound_requests"], 0)
            self.assertFalse(result["reconciled"])
            self.assertEqual(store.load_events_by_aggregate_type("submission_attempt"), [])

    def test_buy_reconciles_durable_economics_and_resume_sends_nothing(self):
        with TemporaryDirectory() as directory:
            first = run_canonical_simulation(BUY, directory, episode_id="buy", now=NOW)
            self.assertEqual(first["status"], "FILL_RECONCILED_ORDER_UNCONFIRMED")
            self.assertEqual(first["environment"], "SIMULATION")
            self.assertTrue(first["reconciled"])
            self.assertEqual(first["cash"], "896.897")
            self.assertEqual(first["position"], "1")
            self.assertEqual(first["new_outbound_requests"], 1)
            self.assertIsNotNone(first["fill_id"])

            reopened = JournalStore(Path(directory) / "journal.sqlite3")
            book = DurableProviderEconomicBook(
                reopened, provider_id=PROVIDER, account_id=ACCOUNT,
                environment=ENVIRONMENT,
            )
            self.assertEqual(str(book.cash("USD")), first["cash"])
            self.assertEqual(str(book.position(INSTRUMENT)), first["position"])
            self.assertIsNotNone(reopened.get_event(first["reconciliation_event_id"]))
            sessions = reopened.load_events("canonical_simulation_session", "single-episode")
            self.assertEqual(
                [event["event_type"] for event in sessions],
                ["SimulationSessionStarted", "SimulationSessionCompleted"],
            )

            again = run_canonical_simulation(BUY, directory, episode_id="buy")
            self.assertTrue(again["resumed"])
            self.assertEqual(again["new_outbound_requests"], 0)
            self.assertEqual(again["fill_id"], first["fill_id"])
            self.assertEqual(again["reconciliation_event_id"], first["reconciliation_event_id"])
            self.assertEqual(
                reopened.load_events("canonical_simulation_session", "single-episode"),
                sessions,
            )
            with self.assertRaisesRegex(ValueError, "another simulation input"):
                run_canonical_simulation(HOLD, directory, episode_id="buy")

    def test_hold_has_no_submission_or_financial_fill_and_resumes(self):
        with TemporaryDirectory() as directory:
            first = run_canonical_simulation(HOLD, directory, episode_id="hold", now=NOW)
            self.assertEqual(first["status"], "HOLD")
            self.assertTrue(first["reconciled"])
            self.assertIsNone(first["order_id"])
            self.assertIsNone(first["fill_id"])
            self.assertEqual(first["new_outbound_requests"], 0)
            self.assertEqual(first["cash"], "1000")
            self.assertEqual(first["position"], "0")
            reopened = JournalStore(Path(directory) / "journal.sqlite3")
            self.assertEqual(reopened.load_events_by_aggregate_type("submission_attempt"), [])
            again = run_canonical_simulation(HOLD, directory, episode_id="hold")
            self.assertEqual(again["status"], "HOLD")
            self.assertTrue(again["resumed"])
            self.assertEqual(again["new_outbound_requests"], 0)

    def test_ambiguous_send_blocks_restart_without_duplicate_exposure(self):
        with TemporaryDirectory() as directory:
            first = run_canonical_simulation(
                BUY, directory, episode_id="ambiguous", now=NOW,
                fault_after_send=True,
            )
            self.assertEqual(first["status"], "UNKNOWN")
            self.assertFalse(first["reconciled"])
            self.assertEqual(first["new_outbound_requests"], 1)
            reopened = JournalStore(Path(directory) / "journal.sqlite3")
            submission = reopened.load_events_by_aggregate_type("submission_attempt")
            self.assertEqual(submission[-1]["event_type"], "SubmissionUnknown")
            book = DurableProviderEconomicBook(
                reopened, provider_id=PROVIDER, account_id=ACCOUNT,
                environment=ENVIRONMENT,
            )
            self.assertEqual(str(book.cash("USD")), "1000")
            self.assertEqual(str(book.position(INSTRUMENT)), "0")
            reservations = DurableReservationBook(
                reopened, environment=ENVIRONMENT, account_id=ACCOUNT,
                resolution_artifact_store=ArtifactStore(Path(directory) / "artifacts"),
                resolution_artifact_root=Path(directory) / "artifacts",
            )
            self.assertEqual(len(reservations.active()), 1)
            self.assertEqual(reservations.active()[0].state, "WORKING")

            again = run_canonical_simulation(BUY, directory, episode_id="ambiguous")
            self.assertEqual(again["status"], "UNKNOWN")
            self.assertTrue(again["resumed"])
            self.assertEqual(again["new_outbound_requests"], 0)
            self.assertFalse(again["reconciled"])
            self.assertEqual(
                reopened.load_events_by_aggregate_type("submission_attempt"),
                submission,
            )

    def test_cli_runs_canonical_session_and_returns_json(self):
        with TemporaryDirectory() as directory:
            command = [
                sys.executable, "-m", "mvp.autotrade_mvp.cli",
                "--canonical-simulation", "--state-dir", directory,
                "--episode-id", "cli", "--prices", "100,101,103",
                "--at", NOW,
            ]
            first = subprocess.run(command, check=True, capture_output=True, text=True)
            initial = json.loads(first.stdout)
            self.assertEqual(initial["status"], "FILL_RECONCILED_ORDER_UNCONFIRMED")
            second = subprocess.run(command, check=True, capture_output=True, text=True)
            resumed = json.loads(second.stdout)
            self.assertTrue(resumed["resumed"])
            self.assertEqual(resumed["new_outbound_requests"], 0)
            self.assertEqual(resumed["fill_id"], initial["fill_id"])


    def test_moving_average_verdict_is_invariant_to_decimal_context(self):
        prices = [
            Decimal("1"),
            Decimal("1.0000000001"),
            Decimal("1.00000000010001"),
        ]
        contexts = (
            (6, ROUND_FLOOR),
            (10, ROUND_CEILING),
            (28, ROUND_HALF_EVEN),
            (80, ROUND_FLOOR),
        )
        observed = []
        for precision, rounding in contexts:
            with self.subTest(precision=precision, rounding=rounding):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    decision = MovingAverageStrategy().decide(
                        prices, Decimal("1")
                    )
                observed.append((decision.side, str(decision.quantity), str(decision.price)))
        self.assertEqual(
            observed,
            [("BUY", "1", "1.00000000010001")] * len(contexts),
        )

    def test_buy_admission_identity_and_reservation_are_context_invariant(self):
        contexts = (
            (6, ROUND_FLOOR),
            (10, ROUND_CEILING),
            (28, ROUND_HALF_EVEN),
            (80, ROUND_FLOOR),
        )
        evidence = []
        for precision, rounding in contexts:
            with self.subTest(precision=precision, rounding=rounding):
                with TemporaryDirectory() as directory:
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        result = run_canonical_simulation(
                            BUY,
                            directory,
                            episode_id="decimal-context-invariant",
                            now=NOW,
                            fault_after_send=True,
                        )
                    store = JournalStore(Path(directory) / "journal.sqlite3")
                    admission = next(
                        event
                        for event in store.load_events("authority_state", "canonical")
                        if event["event_type"] == "AuthorityAdmissionRecorded"
                    )
                    risk = store.load_events_by_aggregate_type("risk_decision")
                    reservation = store.load_events_by_aggregate_type("reservation_book")
                    submission = store.load_events_by_aggregate_type("submission_attempt")
                    self.assertEqual(result["status"], "UNKNOWN")
                    self.assertEqual(result["decision"], "BUY")
                    self.assertEqual(result["new_outbound_requests"], 1)
                    self.assertEqual(len(risk), 1)
                    self.assertEqual(len(reservation), 1)
                    self.assertGreaterEqual(len(submission), 1)
                    evidence.append({
                        "intent_hash": admission["payload"]["intent_hash"],
                        "notional": admission["payload"]["notional"],
                        "reservation_requirements": (
                            reservation[0]["payload"]["request"]["requirements"]
                        ),
                        "risk_reservation_requirements": (
                            risk[0]["payload"]["reservation_requirements"]
                        ),
                        "submission_request": submission[0]["payload"]["request"],
                        "outbound_requests": result["new_outbound_requests"],
                    })
        self.assertTrue(all(item == evidence[0] for item in evidence[1:]))
        self.assertEqual(evidence[0]["notional"], "103")
        self.assertEqual(
            evidence[0]["reservation_requirements"],
            {"CASH:USD": "103.103"},
        )

    def test_fee_inclusive_output_envelope_failure_precedes_state_mutation(self):
        prices = [
            "9" * 255 + "7",
            "9" * 255 + "8",
            "9" * 256,
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with self.assertRaisesRegex(
                ValueError,
                "simulation financial arithmetic exceeds exact decimal resource envelope",
            ):
                run_canonical_simulation(
                    prices,
                    root,
                    episode_id="fee-output-envelope",
                    now=NOW,
                )
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
