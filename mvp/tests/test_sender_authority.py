from contextlib import contextmanager
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.sender_authority import (
    SenderAuthorityError,
    sender_authority_gate_path,
    sender_authority_window,
)


class SenderAuthorityTests(unittest.TestCase):
    def test_gate_identity_is_deterministic_and_scope_bound(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            paper = sender_authority_gate_path(store, owner_scope="PAPER:acct")
            repeated = sender_authority_gate_path(store, owner_scope="PAPER:acct")
            live = sender_authority_gate_path(store, owner_scope="LIVE:acct")
            other = sender_authority_gate_path(store, owner_scope="PAPER:other")

            self.assertEqual(paper, repeated)
            self.assertNotEqual(paper, live)
            self.assertNotEqual(paper, other)
            self.assertEqual(paper.parent, store.path.parent)

    @unittest.skipIf(sys.platform == "win32", "POSIX flock pathname test")
    def test_posix_gate_revalidates_pathname_after_lock_acquisition(self):
        import fcntl

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            lock_path = sender_authority_gate_path(
                store,
                owner_scope="PAPER:acct",
            )
            displaced = Path(directory) / "displaced-sender-gate.lock"
            original_flock = fcntl.flock
            replaced = False

            def replace_after_lock(descriptor, operation):
                nonlocal replaced
                original_flock(descriptor, operation)
                if operation == fcntl.LOCK_EX and not replaced:
                    os.replace(lock_path, displaced)
                    lock_path.write_bytes(b"\0")
                    replaced = True

            with patch("fcntl.flock", new=replace_after_lock):
                with self.assertRaisesRegex(
                    SenderAuthorityError,
                    "pathname changed while lock was held",
                ):
                    with sender_authority_window(
                        store,
                        owner_scope="PAPER:acct",
                    ):
                        self.fail("replaced POSIX gate must not issue a lease")

            self.assertTrue(replaced)

    @unittest.skipIf(sys.platform == "win32", "POSIX flock pathname test")
    def test_posix_gate_detects_pathname_replacement_during_held_window(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            lock_path = sender_authority_gate_path(
                store,
                owner_scope="PAPER:acct",
            )
            displaced = Path(directory) / "displaced-held-sender-gate.lock"

            with self.assertRaisesRegex(
                SenderAuthorityError,
                "pathname changed while lock was held",
            ):
                with sender_authority_window(
                    store,
                    owner_scope="PAPER:acct",
                ):
                    os.replace(lock_path, displaced)
                    lock_path.write_bytes(b"\0")

    def test_paper_sender_validation_and_wire_send_share_one_gate_window(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token="host-a",
                owner_epoch=1,
            )
            active = False
            calls = []

            @contextmanager
            def fake_window(selected_store, *, owner_scope):
                nonlocal active
                self.assertIs(selected_store, store)
                self.assertEqual(owner_scope, "PAPER:acct")
                self.assertFalse(active)
                active = True
                calls.append("enter")
                try:
                    yield
                finally:
                    calls.append("exit")
                    active = False

            def sender_check(owner_id, owner_epoch):
                self.assertTrue(active)
                self.assertEqual((owner_id, owner_epoch), ("host-a", 1))
                calls.append("sender-check")

            def transport(_client_id, _request, final_guard):
                self.assertFalse(active)
                calls.append("transport-enter")
                final_guard()
                self.assertTrue(active)
                calls.append("wire-send")
                return {"provider_order_id": "p-1"}

            with patch(
                "mvp.autotrade_mvp.dispatch.sender_authority_window",
                new=fake_window,
            ):
                outcome = dispatcher.dispatch(
                    attempt_id="gate-a1",
                    intent_id="gate-i1",
                    intent_hash="gate-h1",
                    provider="sim",
                    request={},
                    now="2026-10-04T00:20:00Z",
                    authority_check=lambda _hash, _now: (True, "allowed"),
                    transport_send=transport,
                    sender_check=sender_check,
                )

            self.assertEqual(outcome.status, "SENT")
            self.assertFalse(active)
            self.assertEqual(
                calls,
                [
                    "transport-enter",
                    "enter",
                    "sender-check",
                    "wire-send",
                    "exit",
                ],
            )
            events = store.load_events(
                "submission_attempt", dispatcher._aggregate_id("gate-a1")
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )

    def test_gate_acquisition_failure_blocks_before_sender_check_and_wire(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="LIVE",
                account_id="acct",
                owner_token="host-a",
                owner_epoch=1,
            )
            sender_checks = 0
            outbound = 0

            @contextmanager
            def broken_window(_store, *, owner_scope):
                self.assertEqual(owner_scope, "LIVE:acct")
                raise OSError("gate unavailable")
                yield

            def sender_check(_owner_id, _owner_epoch):
                nonlocal sender_checks
                sender_checks += 1

            def transport(_client_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"provider_order_id": "must-not-happen"}

            with patch(
                "mvp.autotrade_mvp.dispatch.sender_authority_window",
                new=broken_window,
            ):
                outcome = dispatcher.dispatch(
                    attempt_id="gate-fail",
                    intent_id="gate-i2",
                    intent_hash="gate-h2",
                    provider="sim",
                    request={},
                    now="2026-10-04T00:21:00Z",
                    authority_check=lambda _hash, _now: (True, "allowed"),
                    transport_send=transport,
                    sender_check=sender_check,
                )

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(outcome.reason, "sender_authority_gate_failed:OSError")
            self.assertEqual(sender_checks, 0)
            self.assertEqual(outbound, 0)
            events = store.load_events(
                "submission_attempt", dispatcher._aggregate_id("gate-fail")
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

    def test_transport_exception_releases_gate_and_preserves_unknown(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token="host-a",
                owner_epoch=1,
            )
            active = False
            exits = 0

            @contextmanager
            def fake_window(_store, *, owner_scope):
                nonlocal active, exits
                self.assertEqual(owner_scope, "PAPER:acct")
                active = True
                try:
                    yield
                finally:
                    active = False
                    exits += 1

            def transport(_client_id, _request, final_guard):
                final_guard()
                self.assertTrue(active)
                raise TimeoutError("provider reply lost")

            with patch(
                "mvp.autotrade_mvp.dispatch.sender_authority_window",
                new=fake_window,
            ):
                outcome = dispatcher.dispatch(
                    attempt_id="gate-unknown",
                    intent_id="gate-i3",
                    intent_hash="gate-h3",
                    provider="sim",
                    request={},
                    now="2026-10-04T00:22:00Z",
                    authority_check=lambda _hash, _now: (True, "allowed"),
                    transport_send=transport,
                    sender_check=lambda _owner, _epoch: None,
                )

            self.assertEqual(outcome.status, "UNKNOWN")
            self.assertFalse(active)
            self.assertEqual(exits, 1)
            events = store.load_events(
                "submission_attempt", dispatcher._aggregate_id("gate-unknown")
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )


if __name__ == "__main__":
    unittest.main()
