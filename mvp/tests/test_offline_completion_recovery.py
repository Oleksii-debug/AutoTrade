"""Independent process-crash acceptance for the integrated ZERO terminal cut."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.simulated_provider import SimulatedProvider
from mvp.autotrade_mvp.simulation_runtime_checkpoint import (
    COMPLETION_RECEIPT_FIELD, checkpoint_path,
)
from mvp.tests.test_autonomous_simulation import run

ROOT = Path(__file__).resolve().parents[2]


class OfflineCompletionRecoveryTests(unittest.TestCase):
    def test_real_process_exit_after_terminal_commit_reopens_without_duplicate_fill(self):
        prices = ["100", "101", "103"]
        with TemporaryDirectory(prefix="Автотрейд пробіл ") as directory, TemporaryDirectory() as reference:
            expected = run(reference, prices)
            program = '''
import os, sys
from mvp.autotrade_mvp import simulation_session as session
original = session._loop_event
def hard_exit(store, run_id, kind, key, payload, now, **kwargs):
    value = original(store, run_id, kind, key, payload, now, **kwargs)
    if kind == "AutonomousEpisodeCompleted" and payload["episode"] == 3:
        os._exit(23)
    return value
session._loop_event = hard_exit
session.run_autonomous_simulation(["100", "101", "103"], sys.argv[1],
    run_id="acceptance", now="2026-10-03T00:00:00Z")
'''
            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT / "research") + os.pathsep + str(ROOT)
            child = subprocess.run([sys.executable, "-c", program, directory],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(child.returncode, 23, child.stderr)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            before = store.whole_store_state_cut()
            self.assertEqual(store.pending_outbox_count(), 1)
            with patch.object(SimulatedProvider, "transport_send", side_effect=AssertionError("no resend")):
                recovered = run(directory, prices)
            self.assertEqual(recovered["decisions"], expected["decisions"])
            self.assertEqual(recovered["new_outbound_requests"], 0)
            self.assertEqual(recovered["cash"], expected["cash"])
            self.assertEqual(store.whole_store_state_cut(), before)
            self.assertEqual(store.pending_outbox_count(), 0)
            self.assertEqual(run(directory, prices)["new_outbound_requests"], 0)

    def test_changed_terminal_receipt_fails_before_restore_and_sidecar_publication(self):
        with TemporaryDirectory() as directory:
            run(directory, stop_after_episodes=3)
            checkpoint_path(directory).unlink()
            store = JournalStore(Path(directory) / "journal.sqlite3")
            before = store.whole_store_state_cut()
            original = JournalStore.load_events
            def changed(selected, aggregate_type, aggregate_id):
                events = original(selected, aggregate_type, aggregate_id)
                if aggregate_type == "canonical_autonomous_simulation":
                    events = deepcopy(events)
                    events[-1]["payload"][COMPLETION_RECEIPT_FIELD]["result_digest"] = "0" * 64
                return events
            with patch.object(JournalStore, "load_events", changed), patch.object(
                    SimulatedProvider, "from_state", side_effect=AssertionError("no restore")):
                with self.assertRaisesRegex(ValueError, "receipt seal is invalid"):
                    run(directory, stop_after_episodes=3)
            self.assertFalse(checkpoint_path(directory).exists())
            self.assertEqual(store.whole_store_state_cut(), before)

    def test_new_journal_fact_cannot_be_adopted_by_missing_sidecar_recovery(self):
        with TemporaryDirectory() as directory:
            run(directory, stop_after_episodes=3)
            checkpoint_path(directory).unlink()
            store = JournalStore(Path(directory) / "journal.sqlite3")
            payload = {"foreign": "after-terminal-completion"}
            store.append_event({"event_id": "foreign-receipt-cut", "event_type": "Probe",
                "aggregate_type": "probe", "aggregate_id": "foreign", "aggregate_version": "1",
                "payload": payload, "payload_hash": payload_digest(payload),
                "committed_at": "2026-10-03T00:00:03Z"})
            before = store.whole_store_state_cut()
            with patch.object(SimulatedProvider, "from_state", side_effect=AssertionError("no restore")):
                with self.assertRaisesRegex(ValueError, "does not match current authorities"):
                    run(directory)
            self.assertFalse(checkpoint_path(directory).exists())
            self.assertEqual(store.whole_store_state_cut(), before)


if __name__ == "__main__":
    unittest.main()
