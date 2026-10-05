"""Composition regression for an expired durable Prepared simulation attempt."""

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
BUY = ["100", "101", "103"]


class SimulationExpiredPreparedRecoveryTests(unittest.TestCase):
    def test_expired_prepared_restart_terminalizes_zero_wire_blocked(self):
        class ProcessDeath(BaseException):
            pass

        def crash_before_final_guard(
            _provider,
            _client_order_id,
            _request,
            _final_guard,
        ):
            raise ProcessDeath("crash-before-final-send-guard")

        episode_id = "prepared-expiry-zero-wire"
        with TemporaryDirectory() as directory:
            with patch.object(
                simulation_session.SimulatedProvider,
                "transport_send",
                new=crash_before_final_guard,
            ):
                with self.assertRaisesRegex(
                    ProcessDeath,
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
            self.assertEqual(
                [
                    event["event_type"]
                    for event in store.load_events_by_aggregate_type(
                        "submission_attempt"
                    )
                ],
                ["SubmissionPrepared", "SubmissionBlocked"],
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
