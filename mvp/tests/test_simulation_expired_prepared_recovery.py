"""Composition regressions for expired durable Prepared simulation attempts."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp import simulation_session
from mvp.autotrade_mvp.simulation_session import (
    ACCOUNT,
    ENVIRONMENT,
    run_canonical_simulation,
)


NOW = "2026-10-03T17:50:00Z"
AFTER_LEASE = "2026-10-03T17:51:01Z"
LATER_RESTART = "2026-10-03T17:52:02Z"
BUY = ["100", "101", "103"]


class SimulationExpiredPreparedRecoveryTests(unittest.TestCase):
    @staticmethod
    def _crash_before_final_guard(
        _provider,
        _client_order_id,
        _request,
        _final_guard,
    ):
        class ProcessDeath(BaseException):
            pass

        raise ProcessDeath("crash-before-final-send-guard")

    def _leave_prepared(self, directory: str, *, episode_id: str) -> JournalStore:
        with patch.object(
            simulation_session.SimulatedProvider,
            "transport_send",
            new=self._crash_before_final_guard,
        ):
            with self.assertRaisesRegex(
                BaseException,
                "crash-before-final-send-guard",
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
            ["SubmissionPrepared"],
        )
        reservations = DurableReservationBook(
            store,
            environment=ENVIRONMENT,
            account_id=ACCOUNT,
        )
        self.assertEqual(len(reservations.active()), 1)
        self.assertEqual(reservations.active()[0].state, "WORKING")
        return store

    def test_expired_prepared_restart_terminalizes_zero_wire_blocked(self):
        episode_id = "prepared-expiry-zero-wire"
        with TemporaryDirectory() as directory:
            store = self._leave_prepared(directory, episode_id=episode_id)

            recovered = run_canonical_simulation(
                BUY,
                directory,
                episode_id=episode_id,
                now=AFTER_LEASE,
            )

            self.assertEqual(recovered["status"], "BLOCKED")
            self.assertEqual(
                recovered["reason"],
                "prepared_owner_lease_expired_before_send",
            )
            self.assertTrue(recovered["resumed"])
            self.assertTrue(recovered["reconciled"])
            self.assertEqual(recovered["new_outbound_requests"], 0)
            attempt_events = store.load_events_by_aggregate_type(
                "submission_attempt"
            )
            self.assertEqual(
                [event["event_type"] for event in attempt_events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertEqual(
                attempt_events[-1]["committed_at"],
                AFTER_LEASE,
                "lease-expiry BLOCKED evidence must use the recovery instant",
            )
            session_events = store.load_events(
                "canonical_simulation_session",
                "single-episode",
            )
            self.assertEqual(
                [event["event_type"] for event in session_events],
                ["SimulationSessionStarted", "SimulationSessionCompleted"],
            )
            self.assertEqual(
                session_events[-1]["committed_at"],
                attempt_events[-1]["committed_at"],
                "terminal session must inherit its durable BLOCKED evidence time",
            )
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT,
            )
            self.assertEqual(reservations.active(), ())
            self.assertEqual(
                reservations.get(
                    simulation_session._uuid("reservation", episode_id)
                ).state,
                "REJECTED",
            )

            replayed = run_canonical_simulation(
                BUY,
                directory,
                episode_id=episode_id,
                now=LATER_RESTART,
            )
            self.assertEqual(replayed["status"], "BLOCKED")
            self.assertTrue(replayed["resumed"])
            self.assertEqual(replayed["new_outbound_requests"], 0)
            replay_events = store.load_events(
                "canonical_simulation_session",
                "single-episode",
            )
            self.assertEqual(len(replay_events), 2)
            self.assertEqual(
                replay_events[-1]["committed_at"],
                AFTER_LEASE,
                "completed replay must preserve the original durable terminal instant",
            )

    def test_restart_after_blocked_before_terminal_uses_durable_blocked_time(self):
        class ProcessDeathAfterBlocked(BaseException):
            pass

        episode_id = "prepared-expiry-terminal-crash"
        with TemporaryDirectory() as directory:
            store = self._leave_prepared(directory, episode_id=episode_id)

            with patch.object(
                simulation_session,
                "_finalize_zero_wire_blocked",
                side_effect=ProcessDeathAfterBlocked(
                    "crash-after-blocked-before-terminal"
                ),
            ):
                with self.assertRaisesRegex(
                    ProcessDeathAfterBlocked,
                    "crash-after-blocked-before-terminal",
                ):
                    run_canonical_simulation(
                        BUY,
                        directory,
                        episode_id=episode_id,
                        now=AFTER_LEASE,
                    )

            attempt_events = store.load_events_by_aggregate_type(
                "submission_attempt"
            )
            self.assertEqual(
                [event["event_type"] for event in attempt_events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            blocked_at = attempt_events[-1]["committed_at"]
            self.assertEqual(blocked_at, AFTER_LEASE)
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
            reservations = DurableReservationBook(
                store,
                environment=ENVIRONMENT,
                account_id=ACCOUNT,
            )
            self.assertEqual(len(reservations.active()), 1)
            self.assertEqual(reservations.active()[0].state, "WORKING")

            recovered = run_canonical_simulation(
                BUY,
                directory,
                episode_id=episode_id,
                now=LATER_RESTART,
            )

            self.assertEqual(recovered["status"], "BLOCKED")
            self.assertEqual(recovered["new_outbound_requests"], 0)
            session_events = store.load_events(
                "canonical_simulation_session",
                "single-episode",
            )
            self.assertEqual(
                [event["event_type"] for event in session_events],
                ["SimulationSessionStarted", "SimulationSessionCompleted"],
            )
            self.assertEqual(
                session_events[-1]["committed_at"],
                blocked_at,
                "later restart must not rewrite the causal terminal instant",
            )
            self.assertNotEqual(session_events[-1]["committed_at"], LATER_RESTART)
            reservations.refresh()
            self.assertEqual(reservations.active(), ())
            self.assertEqual(
                reservations.get(
                    simulation_session._uuid("reservation", episode_id)
                ).state,
                "REJECTED",
            )


if __name__ == "__main__":
    unittest.main()
