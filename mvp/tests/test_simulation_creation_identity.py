"""Creation-identity regressions for the canonical simulation session."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.simulation_session import run_canonical_simulation


BUY = ["100", "101", "103"]
SOURCE_SHA = "a" * 40
NOW = "2026-09-30T12:00:00Z"
LATER = "2026-09-30T12:00:01Z"


class SimulationCreationIdentityTests(unittest.TestCase):
    @staticmethod
    def _events(directory: str):
        store = JournalStore(Path(directory) / "journal.sqlite3")
        sessions = store.load_events("canonical_simulation_session", "single-episode")
        submissions = store.load_events_by_aggregate_type("submission_attempt")
        return sessions, submissions

    def test_fresh_fault_profile_changes_session_and_attempt_identity(self):
        with TemporaryDirectory() as normal_dir, TemporaryDirectory() as fault_dir:
            normal = run_canonical_simulation(
                BUY, normal_dir, episode_id="creation-fault", source_sha=SOURCE_SHA,
                now=NOW, fault_after_send=False,
            )
            faulted = run_canonical_simulation(
                BUY, fault_dir, episode_id="creation-fault", source_sha=SOURCE_SHA,
                now=NOW, fault_after_send=True,
            )

            self.assertNotEqual(normal["creation_id"], faulted["creation_id"])
            self.assertNotEqual(normal["session_id"], faulted["session_id"])
            normal_sessions, normal_submissions = self._events(normal_dir)
            fault_sessions, fault_submissions = self._events(fault_dir)
            self.assertEqual(
                normal_sessions[0]["payload"]["creation_identity"]["transport_fault_profile"],
                "NONE",
            )
            self.assertEqual(
                fault_sessions[0]["payload"]["creation_identity"]["transport_fault_profile"],
                "AFTER_ACCEPT_RESPONSE_LOST",
            )
            self.assertNotEqual(normal_sessions[0]["event_id"], fault_sessions[0]["event_id"])
            self.assertTrue(normal_submissions)
            self.assertTrue(fault_submissions)
            self.assertNotEqual(normal_submissions[0]["event_id"], fault_submissions[0]["event_id"])

    def test_fresh_evidence_time_changes_session_and_attempt_identity(self):
        with TemporaryDirectory() as first_dir, TemporaryDirectory() as second_dir:
            first = run_canonical_simulation(
                BUY, first_dir, episode_id="creation-time", source_sha=SOURCE_SHA,
                now=NOW,
            )
            second = run_canonical_simulation(
                BUY, second_dir, episode_id="creation-time", source_sha=SOURCE_SHA,
                now=LATER,
            )

            self.assertEqual(first["evidence_time"], NOW)
            self.assertEqual(second["evidence_time"], LATER)
            self.assertNotEqual(first["creation_id"], second["creation_id"])
            self.assertNotEqual(first["session_id"], second["session_id"])
            first_sessions, first_submissions = self._events(first_dir)
            second_sessions, second_submissions = self._events(second_dir)
            self.assertNotEqual(first_sessions[0]["event_id"], second_sessions[0]["event_id"])
            self.assertNotEqual(first_submissions[0]["event_id"], second_submissions[0]["event_id"])

    def test_restart_uses_persisted_creation_identity_not_replay_knobs(self):
        with TemporaryDirectory() as directory:
            first = run_canonical_simulation(
                BUY, directory, episode_id="sticky-creation", source_sha=SOURCE_SHA,
                now=NOW, fault_after_send=True,
            )
            self.assertEqual(first["status"], "UNKNOWN")
            sessions_before, submissions_before = self._events(directory)

            resumed = run_canonical_simulation(
                BUY, directory, episode_id="sticky-creation", source_sha=SOURCE_SHA,
                now=LATER, fault_after_send=False,
            )

            self.assertTrue(resumed["resumed"])
            self.assertEqual(resumed["new_outbound_requests"], 0)
            self.assertEqual(resumed["session_id"], first["session_id"])
            self.assertEqual(resumed["creation_id"], first["creation_id"])
            self.assertEqual(resumed["evidence_time"], NOW)
            sessions_after, submissions_after = self._events(directory)
            self.assertEqual(sessions_after, sessions_before)
            self.assertEqual(submissions_after, submissions_before)


if __name__ == "__main__":
    unittest.main()
