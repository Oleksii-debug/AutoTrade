from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.pipeline import run_multi_episode, run_vertical_slice, verify_replay


class RuntimeJournalTests(unittest.TestCase):
    def test_multi_episode_journal_and_outbox_are_idempotent(self):
        with TemporaryDirectory() as directory:
            episodes = [[100, 101, 102, 103], [100, 100, 100], [103, 102, 101, 100]]
            first = run_multi_episode(episodes, directory)
            self.assertEqual([item.decision for item in first], ["BUY", "HOLD", "SELL"])

            store = JournalStore(Path(directory) / "journal.sqlite3")
            events = store.load_events("simulation_portfolio", "SIM")
            self.assertEqual([event["aggregate_version"] for event in events], [1, 2, 3])
            self.assertEqual(len({event["event_id"] for event in events}), 3)
            self.assertEqual(len(store.pending_outbox()), 3)

            second = run_multi_episode(episodes, directory)
            self.assertEqual([item.decision for item in second], ["BUY", "HOLD", "SELL"])
            self.assertEqual(len(store.load_events("simulation_portfolio", "SIM")), 3)
            self.assertEqual(len(store.pending_outbox()), 3)

    def test_journal_boundary_failure_repairs_on_replay(self):
        with TemporaryDirectory() as directory:
            with patch(
                "mvp.autotrade_mvp.pipeline.handle_journal_event",
                side_effect=RuntimeError("simulated journal crash"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated journal crash"):
                    run_vertical_slice([100, 101, 102, 103], directory)

            self.assertTrue((Path(directory) / "checkpoint.json").exists())
            self.assertTrue((Path(directory) / "learning-evidence.jsonl").exists())
            self.assertTrue(verify_replay(directory))

            recovered = run_vertical_slice([100, 101, 102, 103], directory)
            self.assertTrue(recovered.resumed)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            self.assertEqual(len(store.load_events("simulation_portfolio", "SIM")), 1)
            self.assertEqual(len(store.pending_outbox()), 1)

    def test_recreated_journal_rebuilds_from_deterministic_replay(self):
        with TemporaryDirectory() as directory:
            episodes = [[100, 101, 102, 103], [103, 102, 101, 100]]
            run_multi_episode(episodes, directory)
            journal = Path(directory) / "journal.sqlite3"
            journal.unlink()
            for suffix in ("-wal", "-shm"):
                companion = Path(str(journal) + suffix)
                if companion.exists():
                    companion.unlink()

            run_multi_episode(episodes, directory)
            store = JournalStore(journal)
            events = store.load_events("simulation_portfolio", "SIM")
            self.assertEqual(len(events), 2)
            self.assertEqual([event["aggregate_version"] for event in events], [1, 2])


if __name__ == "__main__":
    unittest.main()
