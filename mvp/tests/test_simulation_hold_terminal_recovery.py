"""Deterministic zero-wire HOLD completion and replay tests."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp import simulation_session
from mvp.autotrade_mvp.simulation_session import (
    ENVIRONMENT,
    run_canonical_simulation,
)


NOW = "2026-10-03T18:05:00Z"
HOLD = ["100", "100", "100"]


class SimulationHoldTerminalRecoveryTests(unittest.TestCase):
    def _crash_before_completed(self):
        real_event = simulation_session._event

        def crash(store, kind, episode_id, payload, now, **kwargs):
            if kind == "SimulationSessionCompleted":
                raise RuntimeError("crash-before-hold-completed")
            return real_event(store, kind, episode_id, payload, now, **kwargs)

        return patch.object(simulation_session, "_event", new=crash)

    def test_started_hold_recovers_as_hold_without_submission(self):
        episode_id = "hold-started-crash"
        with TemporaryDirectory() as directory:
            with self._crash_before_completed():
                with self.assertRaisesRegex(
                    RuntimeError,
                    "crash-before-hold-completed",
                ):
                    run_canonical_simulation(
                        HOLD,
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
                store.load_events_by_aggregate_type("submission_attempt"),
                [],
            )
            self.assertEqual(
                store.load_events_by_aggregate_type("reservation_book"),
                [],
            )

            recovered = run_canonical_simulation(
                HOLD,
                directory,
                episode_id=episode_id,
            )
            self.assertEqual(recovered["status"], "HOLD")
            self.assertTrue(recovered["resumed"])
            self.assertTrue(recovered["reconciled"])
            self.assertEqual(recovered["new_outbound_requests"], 0)
            sessions = store.load_events(
                "canonical_simulation_session",
                "single-episode",
            )
            self.assertEqual(
                [event["event_type"] for event in sessions],
                ["SimulationSessionStarted", "SimulationSessionCompleted"],
            )

            again = run_canonical_simulation(
                HOLD,
                directory,
                episode_id=episode_id,
            )
            self.assertEqual(again["status"], "HOLD")
            self.assertTrue(again["resumed"])
            self.assertEqual(again["new_outbound_requests"], 0)
            self.assertEqual(
                store.load_events(
                    "canonical_simulation_session",
                    "single-episode",
                ),
                sessions,
            )

    def test_completed_hold_revalidates_reconciliation_identity(self):
        episode_id = "hold-reconciliation-tamper"
        with TemporaryDirectory() as directory:
            with self._crash_before_completed():
                with self.assertRaises(RuntimeError):
                    run_canonical_simulation(
                        HOLD,
                        directory,
                        episode_id=episode_id,
                        now=NOW,
                    )

            store = JournalStore(Path(directory) / "journal.sqlite3")
            fake = {
                "status": "HOLD",
                "decision": "HOLD",
                "environment": ENVIRONMENT,
                "episode_id": episode_id,
                "cash": "1000",
                "position": "0",
                "reconciled": True,
                "order_id": None,
                "fill_id": None,
                "reconciliation_event_id": "forged-reconciliation-event",
                "new_outbound_requests": 0,
            }
            fake.update(simulation_session._started_identity(store, episode_id=episode_id))
            simulation_session._event(
                store,
                "SimulationSessionCompleted",
                episode_id,
                fake,
                NOW,
            )
            with self.assertRaisesRegex(
                ValueError,
                "completed HOLD session does not match durable zero-wire facts",
            ):
                run_canonical_simulation(
                    HOLD,
                    directory,
                    episode_id=episode_id,
                )

    def test_started_hold_rejects_foreign_command_state(self):
        episode_id = "hold-foreign-command"
        with TemporaryDirectory() as directory:
            with self._crash_before_completed():
                with self.assertRaises(RuntimeError):
                    run_canonical_simulation(
                        HOLD,
                        directory,
                        episode_id=episode_id,
                        now=NOW,
                    )

            store = JournalStore(Path(directory) / "journal.sqlite3")
            store.record_command(
                command_id="foreign-after-started",
                actor="foreign",
                environment=ENVIRONMENT,
                idempotency_key="foreign-after-started",
                request={"operation": "foreign"},
                result={"ok": True},
                state_version=0,
            )
            before_sequence = store.current_journal_sequence()
            with self.assertRaisesRegex(
                ValueError,
                "HOLD terminal durable state is not exact",
            ):
                run_canonical_simulation(
                    HOLD,
                    directory,
                    episode_id=episode_id,
                )
            self.assertEqual(
                store.current_journal_sequence(),
                before_sequence,
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


if __name__ == "__main__":
    unittest.main()
