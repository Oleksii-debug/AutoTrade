from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_target_host_runner import (
    RuntimeTargetHostRunnerError,
    _capture_resource_metrics,
)


def append_racing_event(journal: JournalStore) -> None:
    payload = {"kind": "resource-cut-race"}
    journal.append_event(
        {
            "event_id": "resource-cut-race",
            "event_type": "QualificationEvent",
            "aggregate_type": "risk_decision",
            "aggregate_id": "resource-cut-race",
            "aggregate_version": "1",
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-03T22:30:00Z",
        }
    )


class RuntimeTargetHostRunnerResourceCutTests(unittest.TestCase):
    def test_resource_sample_rejects_journal_move_during_builtin_metric_read(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            original_pending = JournalStore.pending_outbox_count
            raced = False

            def racing_pending(store: JournalStore) -> int:
                nonlocal raced
                if not raced:
                    raced = True
                    append_racing_event(store)
                return original_pending(store)

            with patch.object(
                JournalStore,
                "pending_outbox_count",
                new=racing_pending,
            ):
                with self.assertRaisesRegex(
                    RuntimeTargetHostRunnerError,
                    "crossed a durable JournalStore cut",
                ):
                    _capture_resource_metrics(journal, None)

            self.assertTrue(raced)
            self.assertEqual(journal.current_journal_sequence(), 1)

    def test_resource_sample_reports_the_bracketed_journal_sequence(self) -> None:
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            metrics = _capture_resource_metrics(
                journal,
                lambda: {"working_set_bytes": 1234},
            )
            self.assertEqual(metrics["journal_sequence"], 0)
            self.assertEqual(metrics["pending_outbox"], 0)
            self.assertEqual(metrics["working_set_bytes"], 1234)
            self.assertGreaterEqual(metrics["active_threads"], 1)
            self.assertGreaterEqual(metrics["process_cpu_time_ns"], 0)


if __name__ == "__main__":
    unittest.main()
