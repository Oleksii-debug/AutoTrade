"""Prepared-only zero-wire recovery for canonical simulation."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.simulated_provider import TRANSPORT_SEND_CONTRACT
from mvp.autotrade_mvp import simulation_session
from mvp.autotrade_mvp.simulation_session import (
    ACCOUNT,
    ENVIRONMENT,
    run_canonical_simulation,
)


NOW = "2026-10-03T18:12:00Z"
BUY = ["100", "101", "103"]


class SimulationPreparedZeroWireRecoveryTests(unittest.TestCase):
    def _reservations(self, directory: str) -> DurableReservationBook:
        return DurableReservationBook(
            JournalStore(Path(directory) / "journal.sqlite3"),
            environment=ENVIRONMENT,
            account_id=ACCOUNT,
        )

    def _crash_after_append(self, event_type: str, error):
        real_append = GuardedDispatcher._append

        def crash(dispatcher, **kwargs):
            result = real_append(dispatcher, **kwargs)
            if kwargs.get("event_type") == event_type:
                raise error
            return result

        return patch.object(GuardedDispatcher, "_append", new=crash)

    def test_prepared_only_crash_recovers_terminal_without_send(self):
        episode_id = "prepared-only-recovery"
        with TemporaryDirectory() as directory:
            with self._crash_after_append(
                "SubmissionPrepared",
                RuntimeError("crash-after-prepared"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "crash-after-prepared",
                ):
                    run_canonical_simulation(
                        BUY,
                        directory,
                        episode_id=episode_id,
                        now=NOW,
                    )

            store = JournalStore(Path(directory) / "journal.sqlite3")
            submission = store.load_events_by_aggregate_type("submission_attempt")
            self.assertEqual(
                [event["event_type"] for event in submission],
                ["SubmissionPrepared"],
            )
            self.assertEqual(
                submission[0]["payload"]["submission_scope"],
                {"transport_contract": TRANSPORT_SEND_CONTRACT},
            )
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
            before = self._reservations(directory)
            self.assertEqual(len(before.active()), 1)
            self.assertEqual(before.active()[0].state, "WORKING")

            recovered = run_canonical_simulation(
                BUY,
                directory,
                episode_id=episode_id,
            )
            self.assertEqual(recovered["status"], "BLOCKED")
            self.assertEqual(
                recovered["reason"],
                simulation_session._PREPARED_ZERO_WIRE_REASON,
            )
            self.assertTrue(recovered["resumed"])
            self.assertTrue(recovered["reconciled"])
            self.assertEqual(recovered["new_outbound_requests"], 0)

            after = self._reservations(directory)
            self.assertEqual(after.active(), ())
            terminal = after.get(
                simulation_session._uuid("reservation", episode_id)
            )
            self.assertEqual(terminal.state, "REJECTED")
            self.assertTrue(
                terminal.resolution_evidence.startswith(
                    "journal:submission-prepared:"
                )
            )
            sessions = store.load_events(
                "canonical_simulation_session",
                "single-episode",
            )
            self.assertEqual(
                [event["event_type"] for event in sessions],
                ["SimulationSessionStarted", "SimulationSessionCompleted"],
            )

            replay = run_canonical_simulation(
                BUY,
                directory,
                episode_id=episode_id,
            )
            self.assertEqual(replay["status"], "BLOCKED")
            self.assertEqual(
                replay["reason"],
                simulation_session._PREPARED_ZERO_WIRE_REASON,
            )
            self.assertTrue(replay["resumed"])
            self.assertEqual(replay["new_outbound_requests"], 0)
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

    def test_crash_before_prepared_terminal_batch_is_atomic(self):
        episode_id = "prepared-terminal-crash"
        with TemporaryDirectory() as directory:
            with self._crash_after_append(
                "SubmissionPrepared",
                RuntimeError("crash-after-prepared"),
            ):
                with self.assertRaises(RuntimeError):
                    run_canonical_simulation(
                        BUY,
                        directory,
                        episode_id=episode_id,
                        now=NOW,
                    )

            terminal_command = simulation_session._uuid(
                "prepared-terminal-command",
                episode_id,
            )
            real_commit = JournalStore.commit_command

            def crash_terminal(store, **kwargs):
                if kwargs.get("command_id") == terminal_command:
                    raise RuntimeError("crash-before-prepared-terminal")
                return real_commit(store, **kwargs)

            with patch.object(
                JournalStore,
                "commit_command",
                new=crash_terminal,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "crash-before-prepared-terminal",
                ):
                    run_canonical_simulation(
                        BUY,
                        directory,
                        episode_id=episode_id,
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
                ["SubmissionPrepared"],
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

    def test_sending_tail_remains_unknown_and_reservation_stays_active(self):
        episode_id = "sending-is-not-prepared-zero-wire"
        with TemporaryDirectory() as directory:
            with self._crash_after_append(
                "SubmissionSending",
                SystemExit("crash-after-sending"),
            ):
                with self.assertRaisesRegex(
                    SystemExit,
                    "crash-after-sending",
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
                    for event in store.load_events_by_aggregate_type(
                        "submission_attempt"
                    )
                ],
                ["SubmissionPrepared", "SubmissionSending"],
            )
            before = self._reservations(directory)
            self.assertEqual(len(before.active()), 1)
            self.assertEqual(before.active()[0].state, "WORKING")

            recovered = run_canonical_simulation(
                BUY,
                directory,
                episode_id=episode_id,
            )
            self.assertEqual(recovered["status"], "UNKNOWN")
            self.assertTrue(recovered["resumed"])
            self.assertFalse(recovered["reconciled"])
            self.assertEqual(recovered["new_outbound_requests"], 0)
            after = self._reservations(directory)
            self.assertEqual(len(after.active()), 1)
            self.assertEqual(after.active()[0].state, "WORKING")
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

    def test_wrong_prepared_transport_contract_fails_closed_without_release(self):
        episode_id = "prepared-wrong-transport-contract"
        with TemporaryDirectory() as directory:
            with patch.object(
                simulation_session,
                "TRANSPORT_SEND_CONTRACT",
                "forged-simulated-transport-contract",
            ), self._crash_after_append(
                "SubmissionPrepared",
                RuntimeError("crash-after-prepared"),
            ):
                with self.assertRaises(RuntimeError):
                    run_canonical_simulation(
                        BUY,
                        directory,
                        episode_id=episode_id,
                        now=NOW,
                    )

            with self.assertRaisesRegex(
                ValueError,
                "not bound to canonical simulated transport",
            ):
                run_canonical_simulation(
                    BUY,
                    directory,
                    episode_id=episode_id,
                )

            store = JournalStore(Path(directory) / "journal.sqlite3")
            self.assertEqual(
                [
                    event["event_type"]
                    for event in store.load_events_by_aggregate_type(
                        "submission_attempt"
                    )
                ],
                ["SubmissionPrepared"],
            )
            reservations = self._reservations(directory)
            self.assertEqual(len(reservations.active()), 1)
            self.assertEqual(reservations.active()[0].state, "WORKING")
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


if __name__ == "__main__":
    unittest.main()
