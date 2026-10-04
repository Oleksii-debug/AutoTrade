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
    def _assert_clean_then_retry(self, path: Path, expected_exit: int, child: str) -> None:
        env = os.environ.copy()
        completed = subprocess.run(
            [sys.executable, "-c", child, str(path)],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            check=False,
            timeout=30,
        )
        self.assertEqual(completed.returncode, expected_exit)

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

    def test_append_event_process_exit_before_outbox_insert_rolls_back_event(self):
        """An event cannot survive a hard exit without its outbox intent."""

        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            JournalStore(path)
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

                def crash_before_outbox_insert():
                    os._exit(79)

                store._now = crash_before_outbox_insert
                store.append_event(envelope, outbox_topic="events")
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
            self.assertEqual(completed.returncode, 79)

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
            result = reopened.append_event(_event(), outbox_topic="events")
            self.assertTrue(result.inserted)
            self.assertEqual(reopened.current_journal_sequence(), 1)
            pending = reopened.pending_outbox()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["event_id"], "evt-hard-crash")

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
            self._assert_clean_then_retry(path, 77, child)


    def test_process_exit_at_final_commit_rolls_back_command_event_and_outbox(self):
        """A hard exit on COMMIT cannot expose a partial durable transaction."""

        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            JournalStore(path)
            child = textwrap.dedent(
                """
                import os
                from contextlib import contextmanager
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

                original_connect = store._connect

                class CrashOnCommit:
                    def __init__(self, connection):
                        self._connection = connection

                    def __getattr__(self, name):
                        return getattr(self._connection, name)

                    def commit(self):
                        os._exit(78)

                @contextmanager
                def crashing_connect():
                    with original_connect() as connection:
                        yield CrashOnCommit(connection)

                store._connect = crashing_connect
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
            self._assert_clean_then_retry(path, 78, child)


    def test_append_event_process_exit_after_real_commit_preserves_full_effect(self):
        """A crash after SQLite COMMIT preserves event and publication intent together."""

        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            JournalStore(path)
            child = textwrap.dedent(
                """
                import os
                from contextlib import contextmanager
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

                original_connect = store._connect

                class CrashAfterCommit:
                    def __init__(self, connection):
                        self._connection = connection

                    def __getattr__(self, name):
                        return getattr(self._connection, name)

                    def commit(self):
                        self._connection.commit()
                        os._exit(80)

                @contextmanager
                def crashing_connect():
                    with original_connect() as connection:
                        yield CrashAfterCommit(connection)

                store._connect = crashing_connect
                store.append_event(envelope, outbox_topic="events")
                raise SystemExit(91)
                """
            )
            completed = subprocess.run(
                [sys.executable, "-c", child, str(path)],
                cwd=Path(__file__).resolve().parents[2],
                env=os.environ.copy(),
                check=False,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 80)

            reopened = JournalStore(path)
            self.assertEqual(
                reopened.whole_store_state_cut(),
                {
                    "journal_sequence": 1,
                    "counts": {
                        "events": 1,
                        "outbox": 1,
                        "command_dedupe": 0,
                        "projection_checkpoints": 0,
                        "global_projection_checkpoints": 0,
                    },
                },
            )
            retry = reopened.append_event(_event(), outbox_topic="events")
            self.assertFalse(retry.inserted)
            self.assertEqual(reopened.current_journal_sequence(), 1)
            pending = reopened.pending_outbox()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["event_id"], "evt-hard-crash")

    def test_command_process_exit_after_real_commit_retries_idempotently(self):
        """A crash after COMMIT but before return leaves one exact command effect."""

        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            JournalStore(path)
            child = textwrap.dedent(
                """
                import os
                from contextlib import contextmanager
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

                original_connect = store._connect

                class CrashAfterCommit:
                    def __init__(self, connection):
                        self._connection = connection

                    def __getattr__(self, name):
                        return getattr(self._connection, name)

                    def commit(self):
                        self._connection.commit()
                        os._exit(81)

                @contextmanager
                def crashing_connect():
                    with original_connect() as connection:
                        yield CrashAfterCommit(connection)

                store._connect = crashing_connect
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
            completed = subprocess.run(
                [sys.executable, "-c", child, str(path)],
                cwd=Path(__file__).resolve().parents[2],
                env=os.environ.copy(),
                check=False,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 81)

            reopened = JournalStore(path)
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
            saved, inserted, appended = reopened.commit_command(
                actor="crash-test",
                environment="SIMULATION",
                command_id="cmd-hard-crash-retry",
                idempotency_key="key-hard-crash",
                request={"action": "ORDER.SUBMIT"},
                result={"status": "IGNORED"},
                state_version=2,
                events=[(_event(), "events")],
                expected_journal_sequence=0,
            )
            self.assertFalse(inserted)
            self.assertEqual(saved, {"status": "ACCEPTED"})
            self.assertEqual(appended, ())
            self.assertEqual(reopened.current_journal_sequence(), 1)
            self.assertEqual(len(reopened.pending_outbox()), 1)


    def test_projection_checkpoint_process_exit_after_commit_is_recoverable(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            JournalStore(path)
            child = textwrap.dedent(
                """
                import os
                from contextlib import contextmanager
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
                store.append_event(envelope)

                original_connect = store._connect

                class CrashAfterCommit:
                    def __init__(self, connection):
                        self._connection = connection

                    def __getattr__(self, name):
                        return getattr(self._connection, name)

                    def commit(self):
                        self._connection.commit()
                        os._exit(82)

                @contextmanager
                def crashing_connect():
                    with original_connect() as connection:
                        yield CrashAfterCommit(connection)

                store._connect = crashing_connect
                store.save_projection_checkpoint(
                    projection_name="position",
                    aggregate_type="account",
                    aggregate_id="paper-1",
                    aggregate_version=1,
                    state={"net_quantity": "1"},
                )
                raise SystemExit(91)
                """
            )
            completed = subprocess.run(
                [sys.executable, "-c", child, str(path)],
                cwd=Path(__file__).resolve().parents[2],
                env=os.environ.copy(),
                check=False,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 82)

            reopened = JournalStore(path)
            checkpoint = reopened.load_projection_checkpoint(
                projection_name="position",
                aggregate_type="account",
                aggregate_id="paper-1",
            )
            self.assertEqual(checkpoint["aggregate_version"], 1)
            self.assertEqual(checkpoint["state"], {"net_quantity": "1"})
            self.assertFalse(
                reopened.save_projection_checkpoint(
                    projection_name="position",
                    aggregate_type="account",
                    aggregate_id="paper-1",
                    aggregate_version=1,
                    state={"net_quantity": "1"},
                )
            )

    def test_global_checkpoint_process_exit_after_commit_is_recoverable(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            JournalStore(path)
            child = textwrap.dedent(
                """
                import os
                from contextlib import contextmanager
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
                store.append_event(envelope)

                original_connect = store._connect

                class CrashAfterCommit:
                    def __init__(self, connection):
                        self._connection = connection

                    def __getattr__(self, name):
                        return getattr(self._connection, name)

                    def commit(self):
                        self._connection.commit()
                        os._exit(83)

                @contextmanager
                def crashing_connect():
                    with original_connect() as connection:
                        yield CrashAfterCommit(connection)

                store._connect = crashing_connect
                store.save_global_projection_checkpoint(
                    projection_name="portfolio",
                    journal_sequence=1,
                    state={"paper-1": "1"},
                )
                raise SystemExit(91)
                """
            )
            completed = subprocess.run(
                [sys.executable, "-c", child, str(path)],
                cwd=Path(__file__).resolve().parents[2],
                env=os.environ.copy(),
                check=False,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 83)

            reopened = JournalStore(path)
            checkpoint = reopened.load_global_projection_checkpoint(
                projection_name="portfolio"
            )
            self.assertEqual(checkpoint["journal_sequence"], 1)
            self.assertEqual(checkpoint["state"], {"paper-1": "1"})
            self.assertFalse(
                reopened.save_global_projection_checkpoint(
                    projection_name="portfolio",
                    journal_sequence=1,
                    state={"paper-1": "1"},
                )
            )


if __name__ == "__main__":
    unittest.main()
