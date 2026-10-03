"""Zero-wire terminal recovery for canonical simulation BLOCKED outcomes."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.authority import AuthorityService
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp import simulation_session
from mvp.autotrade_mvp.simulation_session import (
    ACCOUNT,
    ENVIRONMENT,
    run_canonical_simulation,
)


NOW = "2026-10-03T17:50:00Z"
BUY = ["100", "101", "103"]


class SimulationZeroWireTerminalTests(unittest.TestCase):
    def _block_dispatch(self):
        return patch.object(
            AuthorityService,
            "dispatch_allowed",
            return_value=(False, "test_pre_send_block"),
        )

    def _reservations(self, directory: str) -> DurableReservationBook:
        return DurableReservationBook(
            JournalStore(Path(directory) / "journal.sqlite3"),
            environment=ENVIRONMENT,
            account_id=ACCOUNT,
        )

    def test_blocked_is_atomic_terminal_and_releases_reservation(self):
        with TemporaryDirectory() as directory:
            with self._block_dispatch():
                first = run_canonical_simulation(
                    BUY,
                    directory,
                    episode_id="blocked-terminal",
                    now=NOW,
                )

            self.assertEqual(first["status"], "BLOCKED")
            self.assertFalse(first["resumed"])
            self.assertTrue(first["reconciled"])
            self.assertEqual(first["new_outbound_requests"], 0)

            store = JournalStore(Path(directory) / "journal.sqlite3")
            submission = store.load_events_by_aggregate_type("submission_attempt")
            self.assertEqual(
                [event["event_type"] for event in submission],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            sessions = store.load_events(
                "canonical_simulation_session",
                "single-episode",
            )
            self.assertEqual(
                [event["event_type"] for event in sessions],
                ["SimulationSessionStarted", "SimulationSessionCompleted"],
            )
            reservations = self._reservations(directory)
            self.assertEqual(reservations.active(), ())
            terminal = reservations.get(
                simulation_session._uuid("reservation", "blocked-terminal")
            )
            self.assertEqual(terminal.state, "REJECTED")
            self.assertTrue(
                terminal.resolution_evidence.startswith(
                    "journal:submission-blocked:"
                )
            )
            self.assertTrue(all(value == 0 for value in terminal.remaining.values()))

            again = run_canonical_simulation(
                BUY,
                directory,
                episode_id="blocked-terminal",
            )
            self.assertEqual(again["status"], "BLOCKED")
            self.assertTrue(again["resumed"])
            self.assertEqual(again["new_outbound_requests"], 0)
            self.assertEqual(
                store.load_events_by_aggregate_type("submission_attempt"),
                submission,
            )
            self.assertEqual(
                store.load_events(
                    "canonical_simulation_session",
                    "single-episode",
                ),
                sessions,
            )

    def test_crash_before_atomic_terminal_leaves_both_sides_nonterminal_then_recovers(self):
        episode_id = "blocked-crash"
        terminal_command = simulation_session._uuid(
            "blocked-terminal-command",
            episode_id,
        )
        real_commit = JournalStore.commit_command

        def crash_terminal(store, **kwargs):
            if kwargs.get("command_id") == terminal_command:
                raise RuntimeError("crash-before-zero-wire-terminal")
            return real_commit(store, **kwargs)

        with TemporaryDirectory() as directory:
            with self._block_dispatch(), patch.object(
                JournalStore,
                "commit_command",
                new=crash_terminal,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "crash-before-zero-wire-terminal",
                ):
                    run_canonical_simulation(
                        BUY,
                        directory,
                        episode_id=episode_id,
                        now=NOW,
                    )

            store = JournalStore(Path(directory) / "journal.sqlite3")
            self.assertEqual(
                [
                    event["event_type"]
                    for event in store.load_events(
                        "canonical_simulation_session",
                        "single-episode",
                    )
                ],
                ["SimulationSessionStarted"],
            )
            self.assertEqual(
                [
                    event["event_type"]
                    for event in store.load_events_by_aggregate_type(
                        "submission_attempt"
                    )
                ],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            before = self._reservations(directory)
            self.assertEqual(len(before.active()), 1)
            self.assertEqual(before.active()[0].state, "WORKING")

            recovered = run_canonical_simulation(
                BUY,
                directory,
                episode_id=episode_id,
            )
            self.assertEqual(recovered["status"], "BLOCKED")
            self.assertTrue(recovered["resumed"])
            self.assertEqual(recovered["new_outbound_requests"], 0)

            after = self._reservations(directory)
            self.assertEqual(after.active(), ())
            self.assertEqual(
                after.get(
                    simulation_session._uuid("reservation", episode_id)
                ).state,
                "REJECTED",
            )
            self.assertEqual(
                [
                    event["event_type"]
                    for event in store.load_events(
                        "canonical_simulation_session",
                        "single-episode",
                    )
                ],
                ["SimulationSessionStarted", "SimulationSessionCompleted"],
            )

    def test_completed_blocked_cannot_replay_with_working_reservation(self):
        episode_id = "blocked-forged-terminal"
        terminal_command = simulation_session._uuid(
            "blocked-terminal-command",
            episode_id,
        )
        real_commit = JournalStore.commit_command

        def crash_terminal(store, **kwargs):
            if kwargs.get("command_id") == terminal_command:
                raise RuntimeError("crash-before-zero-wire-terminal")
            return real_commit(store, **kwargs)

        with TemporaryDirectory() as directory:
            with self._block_dispatch(), patch.object(
                JournalStore,
                "commit_command",
                new=crash_terminal,
            ):
                with self.assertRaises(RuntimeError):
                    run_canonical_simulation(
                        BUY,
                        directory,
                        episode_id=episode_id,
                        now=NOW,
                    )

            store = JournalStore(Path(directory) / "journal.sqlite3")
            checkpoint = store.load_events_by_aggregate_type(
                "account_reconciliation"
            )[0]
            fake = {
                "status": "BLOCKED",
                "decision": "BUY",
                "environment": ENVIRONMENT,
                "episode_id": episode_id,
                "reason": "test_pre_send_block",
                "cash": "1000",
                "position": "0",
                "reconciled": True,
                "order_id": simulation_session.stable_client_order_id(
                    "simulated",
                    simulation_session._uuid("intent", episode_id),
                    environment=ENVIRONMENT,
                    account_id=ACCOUNT,
                ),
                "fill_id": None,
                "reconciliation_event_id": checkpoint["event_id"],
                "new_outbound_requests": 0,
            }
            simulation_session._event(
                store,
                "SimulationSessionCompleted",
                episode_id,
                fake,
                NOW,
            )
            with self.assertRaisesRegex(
                ValueError,
                "reservation disposition is inconsistent",
            ):
                run_canonical_simulation(
                    BUY,
                    directory,
                    episode_id=episode_id,
                )


if __name__ == "__main__":
    unittest.main()
