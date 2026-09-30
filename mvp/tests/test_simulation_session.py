"""Product entrypoint checks for the canonical network-free simulation session."""

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.simulation_session as simulation_session
from mvp.autotrade_mvp.accounting import book_external_cash_flow
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.exact_decimal import ExactDecimalError
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.pipeline import MovingAverageStrategy
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.simulation_session import (
    ACCOUNT, ENVIRONMENT, INSTRUMENT, PROVIDER, run_canonical_simulation,
)
from research.autotrade_research.artifacts.store import ArtifactStore


NOW = "2026-09-30T12:00:00Z"
BUY = ["100", "101", "103"]
HOLD = ["100", "101"]
SOURCE_SHA = "a" * 40
OTHER_SOURCE_SHA = "b" * 40
ROUNDINGS = (ROUND_FLOOR, ROUND_CEILING, ROUND_HALF_EVEN)


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
            result = run_canonical_simulation(
                BUY, directory, episode_id="orphan", source_sha=SOURCE_SHA, now=NOW
            )
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertEqual(result["new_outbound_requests"], 0)
            self.assertFalse(result["reconciled"])
            self.assertEqual(result["source_sha"], SOURCE_SHA)
            self.assertEqual(store.load_events_by_aggregate_type("submission_attempt"), [])

    def test_buy_reconciles_durable_economics_and_resume_sends_nothing(self):
        with TemporaryDirectory() as directory:
            first = run_canonical_simulation(
                BUY, directory, episode_id="buy", source_sha=SOURCE_SHA, now=NOW
            )
            self.assertEqual(first["status"], "FILL_RECONCILED_ORDER_UNCONFIRMED")
            self.assertEqual(first["environment"], "SIMULATION")
            self.assertEqual(first["source_sha"], SOURCE_SHA)
            self.assertTrue(first["protocol_id"].startswith("sha256:"))
            self.assertTrue(first["session_id"].startswith("sha256:"))
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
            started = sessions[0]["payload"]
            self.assertEqual(started["source_sha"], SOURCE_SHA)
            self.assertEqual(started["protocol_id"], first["protocol_id"])
            self.assertEqual(started["session_id"], first["session_id"])
            self.assertEqual(started["protocol"]["source_sha"], SOURCE_SHA)
            self.assertEqual(
                started["protocol"]["financial_scope"]["instrument"], INSTRUMENT
            )
            self.assertEqual(
                started["protocol"]["strategy"]["strategy_id"], "moving-average"
            )

            again = run_canonical_simulation(
                BUY, directory, episode_id="buy", source_sha=SOURCE_SHA
            )
            self.assertTrue(again["resumed"])
            self.assertEqual(again["new_outbound_requests"], 0)
            self.assertEqual(again["fill_id"], first["fill_id"])
            self.assertEqual(again["session_id"], first["session_id"])
            self.assertEqual(again["reconciliation_event_id"], first["reconciliation_event_id"])
            self.assertEqual(
                reopened.load_events("canonical_simulation_session", "single-episode"),
                sessions,
            )
            with self.assertRaisesRegex(ValueError, "another simulation input"):
                run_canonical_simulation(
                    HOLD, directory, episode_id="buy", source_sha=SOURCE_SHA
                )

    def test_hold_has_no_submission_or_financial_fill_and_resumes(self):
        with TemporaryDirectory() as directory:
            first = run_canonical_simulation(
                HOLD, directory, episode_id="hold", source_sha=SOURCE_SHA, now=NOW
            )
            self.assertEqual(first["status"], "HOLD")
            self.assertTrue(first["reconciled"])
            self.assertIsNone(first["order_id"])
            self.assertIsNone(first["fill_id"])
            self.assertEqual(first["new_outbound_requests"], 0)
            self.assertEqual(first["cash"], "1000")
            self.assertEqual(first["position"], "0")
            reopened = JournalStore(Path(directory) / "journal.sqlite3")
            self.assertEqual(reopened.load_events_by_aggregate_type("submission_attempt"), [])
            again = run_canonical_simulation(
                HOLD, directory, episode_id="hold", source_sha=SOURCE_SHA
            )
            self.assertEqual(again["status"], "HOLD")
            self.assertTrue(again["resumed"])
            self.assertEqual(again["new_outbound_requests"], 0)

    def test_ambiguous_send_blocks_restart_without_duplicate_exposure(self):
        with TemporaryDirectory() as directory:
            first = run_canonical_simulation(
                BUY, directory, episode_id="ambiguous", source_sha=SOURCE_SHA,
                now=NOW, fault_after_send=True,
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

            again = run_canonical_simulation(
                BUY, directory, episode_id="ambiguous", source_sha=SOURCE_SHA
            )
            self.assertEqual(again["status"], "UNKNOWN")
            self.assertTrue(again["resumed"])
            self.assertEqual(again["new_outbound_requests"], 0)
            self.assertFalse(again["reconciled"])
            self.assertEqual(
                reopened.load_events_by_aggregate_type("submission_attempt"),
                submission,
            )

    def test_source_sha_change_is_rejected_before_another_outbound_request(self):
        with TemporaryDirectory() as directory:
            first = run_canonical_simulation(
                BUY, directory, episode_id="build-bound", source_sha=SOURCE_SHA, now=NOW
            )
            self.assertEqual(first["new_outbound_requests"], 1)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            submission_before = store.load_events_by_aggregate_type("submission_attempt")
            sessions_before = store.load_events(
                "canonical_simulation_session", "single-episode"
            )

            with self.assertRaisesRegex(ValueError, "protocol/build identity"):
                run_canonical_simulation(
                    BUY, directory, episode_id="build-bound",
                    source_sha=OTHER_SOURCE_SHA, now=NOW,
                )

            self.assertEqual(
                store.load_events_by_aggregate_type("submission_attempt"),
                submission_before,
            )
            self.assertEqual(
                store.load_events("canonical_simulation_session", "single-episode"),
                sessions_before,
            )

    def test_protocol_change_is_rejected_before_another_outbound_request(self):
        with TemporaryDirectory() as directory:
            first = run_canonical_simulation(
                BUY, directory, episode_id="protocol-bound", source_sha=SOURCE_SHA, now=NOW
            )
            self.assertEqual(first["new_outbound_requests"], 1)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            submission_before = store.load_events_by_aggregate_type("submission_attempt")

            with patch.object(
                simulation_session,
                "CANONICAL_SIMULATION_PROTOCOL_VERSION",
                "2.0.1",
            ):
                with self.assertRaisesRegex(ValueError, "protocol/build identity"):
                    run_canonical_simulation(
                        BUY, directory, episode_id="protocol-bound",
                        source_sha=SOURCE_SHA, now=NOW,
                    )

            self.assertEqual(
                store.load_events_by_aggregate_type("submission_attempt"),
                submission_before,
            )

    def test_fresh_builds_cannot_alias_session_or_event_identity(self):
        observed = []
        for source_sha in (SOURCE_SHA, OTHER_SOURCE_SHA):
            with TemporaryDirectory() as directory:
                result = run_canonical_simulation(
                    HOLD, directory, episode_id="same-episode",
                    source_sha=source_sha, now=NOW,
                )
                store = JournalStore(Path(directory) / "journal.sqlite3")
                events = store.load_events(
                    "canonical_simulation_session", "single-episode"
                )
                observed.append((
                    result["protocol_id"],
                    result["session_id"],
                    tuple(event["event_id"] for event in events),
                    tuple(event["payload_hash"] for event in events),
                ))
        self.assertNotEqual(observed[0][0], observed[1][0])
        self.assertNotEqual(observed[0][1], observed[1][1])
        self.assertNotEqual(observed[0][2], observed[1][2])
        self.assertNotEqual(observed[0][3], observed[1][3])

    def test_source_sha_is_canonical_and_fails_before_state_creation(self):
        for value in ("", "A" * 40, "a" * 39, "a" * 41, "not-a-sha"):
            with self.subTest(value=value):
                with TemporaryDirectory() as parent:
                    state_dir = Path(parent) / "session"
                    with self.assertRaisesRegex(ValueError, "source_sha"):
                        run_canonical_simulation(
                            HOLD, state_dir, episode_id="bad-sha",
                            source_sha=value, now=NOW,
                        )
                    self.assertFalse(state_dir.exists())

    def test_strategy_decisions_are_identical_across_hostile_decimal_contexts(self):
        cases = (
            (["100", "101", "103"], "BUY"),
            (["1", "1.0000000001", "1.00000000010001"], "BUY"),
            (["103", "101", "100"], "SELL"),
            (["100", "100", "100"], "HOLD"),
        )
        for precision in (1, 2, 6, 10, 28, 80):
            for rounding in ROUNDINGS:
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        for raw_prices, expected in cases:
                            decision = MovingAverageStrategy().decide(
                                [Decimal(value) for value in raw_prices], Decimal("1")
                            )
                            self.assertEqual(decision.side, expected)

    def test_buy_durable_identity_and_economics_are_context_independent(self):
        expected_signature = None
        for precision in (6, 10, 28, 80):
            for rounding in ROUNDINGS:
                with self.subTest(precision=precision, rounding=rounding):
                    with TemporaryDirectory() as directory:
                        with localcontext() as context:
                            context.prec = precision
                            context.rounding = rounding
                            result = run_canonical_simulation(
                                BUY, directory, episode_id="context-buy",
                                source_sha=SOURCE_SHA, now=NOW,
                            )
                        store = JournalStore(Path(directory) / "journal.sqlite3")
                        authority_events = store.load_events("authority_state", "canonical")
                        admission = next(
                            event for event in authority_events
                            if event["event_type"] == "AuthorityAdmissionRecorded"
                        )
                        reservation = next(
                            event for event in store.load_events_by_aggregate_type("reservation_book")
                            if event["payload"]["operation"] == "RESERVE"
                        )
                        submission = store.load_events_by_aggregate_type("submission_attempt")
                        sessions = store.load_events(
                            "canonical_simulation_session", "single-episode"
                        )
                        self.assertEqual(admission["payload"]["notional"], "103")
                        self.assertEqual(admission["payload"]["outcome"], "ADMITTED")
                        self.assertEqual(
                            reservation["payload"]["request"]["requirements"]["CASH:USD"],
                            "103.103",
                        )
                        self.assertEqual(result["cash"], "896.897")
                        self.assertEqual(result["position"], "1")
                        self.assertEqual(result["new_outbound_requests"], 1)
                        signature = (
                            result["status"],
                            result["cash"],
                            result["position"],
                            result["new_outbound_requests"],
                            result["protocol_id"],
                            result["session_id"],
                            admission["event_id"],
                            admission["payload"]["intent_hash"],
                            admission["payload"]["notional"],
                            admission["payload"]["outcome"],
                            reservation["payload_hash"],
                            tuple(
                                (event["event_type"], event["event_id"], event["payload_hash"])
                                for event in submission
                            ),
                            tuple(
                                (event["event_type"], event["event_id"], event["payload_hash"])
                                for event in sessions
                            ),
                        )
                        if expected_signature is None:
                            expected_signature = signature
                        else:
                            self.assertEqual(signature, expected_signature)

    def test_low_precision_buy_keeps_exact_notional_and_active_reservation(self):
        for precision in (1, 2):
            for rounding in ROUNDINGS:
                with self.subTest(precision=precision, rounding=rounding):
                    with TemporaryDirectory() as directory:
                        with localcontext() as context:
                            context.prec = precision
                            context.rounding = rounding
                            result = run_canonical_simulation(
                                BUY, directory, episode_id="low-precision",
                                source_sha=SOURCE_SHA, now=NOW,
                                fault_after_send=True,
                            )
                        store = JournalStore(Path(directory) / "journal.sqlite3")
                        admission = next(
                            event for event in store.load_events("authority_state", "canonical")
                            if event["event_type"] == "AuthorityAdmissionRecorded"
                        )
                        reservation = next(
                            event for event in store.load_events_by_aggregate_type("reservation_book")
                            if event["payload"]["operation"] == "RESERVE"
                        )
                        self.assertEqual(admission["payload"]["notional"], "103")
                        self.assertEqual(admission["payload"]["outcome"], "ADMITTED")
                        self.assertEqual(
                            reservation["payload"]["request"]["requirements"]["CASH:USD"],
                            "103.103",
                        )
                        self.assertEqual(result["status"], "UNKNOWN")
                        self.assertEqual(result["new_outbound_requests"], 1)

    def test_exact_resource_failure_precedes_session_mutation(self):
        with TemporaryDirectory() as parent:
            state_dir = Path(parent) / "session"
            with self.assertRaises(ExactDecimalError):
                run_canonical_simulation(
                    ["1", "1", "1e256"], state_dir,
                    episode_id="resource-envelope", source_sha=SOURCE_SHA, now=NOW,
                )
            self.assertFalse(state_dir.exists())

    def test_cli_runs_canonical_session_and_returns_json(self):
        with TemporaryDirectory() as directory:
            command = [
                sys.executable, "-m", "mvp.autotrade_mvp.cli",
                "--canonical-simulation", "--state-dir", directory,
                "--episode-id", "cli", "--source-sha", SOURCE_SHA,
                "--prices", "100,101,103", "--at", NOW,
            ]
            first = subprocess.run(command, check=True, capture_output=True, text=True)
            initial = json.loads(first.stdout)
            self.assertEqual(initial["status"], "FILL_RECONCILED_ORDER_UNCONFIRMED")
            self.assertEqual(initial["source_sha"], SOURCE_SHA)
            second = subprocess.run(command, check=True, capture_output=True, text=True)
            resumed = json.loads(second.stdout)
            self.assertTrue(resumed["resumed"])
            self.assertEqual(resumed["new_outbound_requests"], 0)
            self.assertEqual(resumed["fill_id"], initial["fill_id"])
            self.assertEqual(resumed["session_id"], initial["session_id"])

    def test_cli_requires_source_sha_for_canonical_simulation(self):
        with TemporaryDirectory() as directory:
            command = [
                sys.executable, "-m", "mvp.autotrade_mvp.cli",
                "--canonical-simulation", "--state-dir", directory,
                "--episode-id", "cli", "--prices", "100,101",
            ]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("requires --source-sha", result.stderr)


if __name__ == "__main__":
    unittest.main()
