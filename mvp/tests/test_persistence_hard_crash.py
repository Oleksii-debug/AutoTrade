"""Hard-process crash-boundary regressions for the canonical JournalStore."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import textwrap
import unittest

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


def _event() -> dict:
    payload = {"kind": "fill", "quantity": "1"}
    return {
        "event_id": "evt-hard-crash",
        "event_type": "ExecutionFillObserved",
        "aggregate_type": "account",
        "aggregate_id": "paper-1",
        "aggregate_version": "1",
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": "2026-10-04T15:20:00+00:00",
    }


class HardCrashPersistenceTests(unittest.TestCase):
    def test_process_exit_before_outbox_insert_rolls_back_whole_command_transaction(self):
        """Command/event writes cannot survive without their atomic outbox intent."""

        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            self.assertEqual(
                store.whole_store_state_cut(),
                {
                    "journal_sequence": 0,
                    "counts": {
                        "events": 0,
                        "outbox": 0,
                        "command_dedupe": 0,
                        "projection_checkpoints": 0,
                        "global_projection_checkpoints": 0,
                    },
                },
            )

            child = textwrap.dedent(
                """
                import os
                import sys

                from mvp.autotrade_mvp.persistence import JournalStore, payload_digest

                path = sys.argv[1]
                store = JournalStore(path)
                payload = {"kind": "fill", "quantity": "1"}
                envelope = {
                    "event_id": "evt-hard-crash",
                    "event_type": "ExecutionFillObserved",
                    "aggregate_type": "account",
                    "aggregate_id": "paper-1",
                    "aggregate_version": "1",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": "2026-10-04T15:20:00+00:00",
                }

                calls = 0
                def crash_on_outbox_timestamp():
                    global calls
                    calls += 1
                    if calls == 2:
                        os._exit(77)
                    return "2026-10-04T15:20:01+00:00"

                store._now = crash_on_outbox_timestamp
                store.commit_command(
                    actor="crash-test",
                    environment="SIMULATION",
                    command_id="cmd-hard-crash",
                    idempotency_key="key-hard-crash",
                    request={"action": "ORDER.SUBMIT"},
                    result={"status": "ACCEPTED"},
                    state_version=1,
                    events=[(envelope, "events")],
                    expected_journal_sequence=0,
                )
                raise SystemExit(91)
                """
            )
            env = os.environ.copy()
            completed = subprocess.run(
                [sys.executable, "-c", child, str(path)],
                cwd=Path(__file__).resolve().parents[2],
                env=env,
                check=False,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 77)

            reopened = JournalStore(path)
            self.assertEqual(
                reopened.whole_store_state_cut(),
                {
                    "journal_sequence": 0,
                    "counts": {
                        "events": 0,
                        "outbox": 0,
                        "command_dedupe": 0,
                        "projection_checkpoints": 0,
                        "global_projection_checkpoints": 0,
                    },
                },
            )

            saved, inserted, appended = reopened.commit_command(
                actor="crash-test",
                environment="SIMULATION",
                command_id="cmd-hard-crash",
                idempotency_key="key-hard-crash",
                request={"action": "ORDER.SUBMIT"},
                result={"status": "ACCEPTED"},
                state_version=1,
                events=[(_event(), "events")],
                expected_journal_sequence=0,
            )
            self.assertTrue(inserted)
            self.assertEqual(saved, {"status": "ACCEPTED"})
            self.assertEqual([item.event_id for item in appended], ["evt-hard-crash"])
            self.assertEqual(
                reopened.whole_store_state_cut(),
                {
                    "journal_sequence": 1,
                    "counts": {
                        "events": 1,
                        "outbox": 1,
                        "command_dedupe": 1,
                        "projection_checkpoints": 0,
                        "global_projection_checkpoints": 0,
                    },
                },
            )
            pending = reopened.pending_outbox()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["event_id"], "evt-hard-crash")


if __name__ == "__main__":
    unittest.main()
