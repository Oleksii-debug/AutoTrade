"""Crash recovery for canonical simulation submission pre-wire states."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.simulation_session import run_canonical_simulation


BUY = ["100", "101", "103"]
NOW = "2026-09-30T12:00:00Z"
BEFORE_LEASE = "2026-09-30T12:00:30Z"
AFTER_LEASE = "2026-09-30T12:01:01Z"


class SimulationPreparedRecoveryTests(unittest.TestCase):
    def test_prepared_only_restart_waits_for_lease_then_durably_becomes_unknown(self):
        original_append = GuardedDispatcher._append

        def crash_after_prepared(self, **kwargs):
            result = original_append(self, **kwargs)
            if kwargs["event_type"] == "SubmissionPrepared":
                raise SystemExit("simulated process death after Prepared")
            return result

        with TemporaryDirectory() as directory:
            with patch.object(
                GuardedDispatcher, "_append", crash_after_prepared
            ):
                with self.assertRaisesRegex(SystemExit, "after Prepared"):
                    run_canonical_simulation(
                        BUY,
                        directory,
                        episode_id="prepared-crash",
                        now=NOW,
                    )

            store = JournalStore(Path(directory) / "journal.sqlite3")
            before = store.load_events_by_aggregate_type("submission_attempt")
            self.assertEqual(
                [event["event_type"] for event in before],
                ["SubmissionPrepared"],
            )

            active = run_canonical_simulation(
                BUY,
                directory,
                episode_id="prepared-crash",
                now=BEFORE_LEASE,
            )
            self.assertEqual(active["status"], "IN_PROGRESS")
            self.assertEqual(
                active["reason"], "prepared_owner_lease_active"
            )
            self.assertEqual(active["new_outbound_requests"], 0)
            self.assertEqual(
                store.load_events_by_aggregate_type("submission_attempt"),
                before,
            )

            recovered = run_canonical_simulation(
                BUY,
                directory,
                episode_id="prepared-crash",
                now=AFTER_LEASE,
            )
            self.assertEqual(recovered["status"], "UNKNOWN")
            self.assertEqual(
                recovered["reason"],
                "prepared_owner_lease_expired_without_send_evidence",
            )
            self.assertTrue(recovered["resumed"])
            self.assertEqual(recovered["new_outbound_requests"], 0)
            self.assertEqual(
                [
                    event["event_type"]
                    for event in store.load_events_by_aggregate_type(
                        "submission_attempt"
                    )
                ],
                ["SubmissionPrepared", "SubmissionUnknown"],
            )

            again = run_canonical_simulation(
                BUY,
                directory,
                episode_id="prepared-crash",
                now="2026-09-30T12:02:00Z",
            )
            self.assertEqual(again["status"], "UNKNOWN")
            self.assertEqual(again["new_outbound_requests"], 0)
            self.assertEqual(
                [
                    event["event_type"]
                    for event in store.load_events_by_aggregate_type(
                        "submission_attempt"
                    )
                ],
                ["SubmissionPrepared", "SubmissionUnknown"],
            )

    def test_sending_only_restart_durably_becomes_unknown_without_transport(self):
        original_append = GuardedDispatcher._append

        def crash_after_sending(self, **kwargs):
            result = original_append(self, **kwargs)
            if kwargs["event_type"] == "SubmissionSending":
                raise SystemExit("simulated process death after Sending")
            return result

        with TemporaryDirectory() as directory:
            with patch.object(
                GuardedDispatcher, "_append", crash_after_sending
            ):
                with self.assertRaisesRegex(SystemExit, "after Sending"):
                    run_canonical_simulation(
                        BUY,
                        directory,
                        episode_id="sending-crash",
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

            recovered = run_canonical_simulation(
                BUY,
                directory,
                episode_id="sending-crash",
                now=AFTER_LEASE,
            )
            self.assertEqual(recovered["status"], "UNKNOWN")
            self.assertEqual(
                recovered["reason"],
                "recovered_after_send_barrier_without_terminal_result",
            )
            self.assertEqual(recovered["new_outbound_requests"], 0)
            self.assertEqual(
                [
                    event["event_type"]
                    for event in store.load_events_by_aggregate_type(
                        "submission_attempt"
                    )
                ],
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionUnknown",
                ],
            )


if __name__ == "__main__":
    unittest.main()
