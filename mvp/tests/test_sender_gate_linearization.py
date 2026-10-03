from tempfile import TemporaryDirectory
from threading import Event, Thread
import multiprocessing as mp
import os
import unittest

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import OutboundAttempt, RecoveryController
from mvp.autotrade_mvp.sender_gate import journal_sender_gate


def _process_gate_contender(path, started, entered):
    store = JournalStore(path)
    started.set()
    with journal_sender_gate(store):
        entered.set()


class SameJournalSenderGateTests(unittest.TestCase):
    def test_recovery_start_waits_until_dispatch_terminal_classification(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner-1",
            )
            attempted = Event()
            completed = Event()
            worker = None
            recovery_box = {}

            def authority(intent_hash, now):
                return True, "allowed"

            def start_successor():
                attempted.set()
                recovery = RecoveryController(
                    owner_store=JournalStore(store.path),
                    owner_scope="SIMULATION:acct",
                )
                recovery_box["recovery"] = recovery
                recovery.start("owner-2")
                completed.set()

            def transport(client_id, request, final_guard):
                nonlocal worker
                final_guard()
                worker = Thread(target=start_successor)
                worker.start()
                self.assertTrue(attempted.wait(1.0))
                self.assertFalse(
                    completed.wait(0.05),
                    "owner transition escaped while provider-send gate was held",
                )
                return {"provider_order_id": "provider-1"}

            outcome = dispatcher.dispatch(
                attempt_id="gate-race",
                intent_id="intent-1",
                intent_hash="intent-hash-1",
                provider="sim",
                request={"qty": "1"},
                now="2026-10-01T00:00:00Z",
                authority_check=authority,
                transport_send=transport,
            )
            self.assertEqual(outcome.status, "SENT")
            self.assertIsNotNone(worker)
            worker.join(1.0)
            self.assertFalse(worker.is_alive())
            self.assertTrue(completed.is_set())

            attempt_events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("gate-race"),
            )
            self.assertEqual(
                [event["event_type"] for event in attempt_events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )
            owner_chain = recovery_box["recovery"].durable_owner_chain()
            self.assertEqual(owner_chain[-1].owner_id, "owner-2")

    def test_gate_is_shared_across_distinct_journal_objects_for_same_store(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            first = JournalStore(path)
            second = JournalStore(path)
            attempted = Event()
            completed = Event()

            def worker_body():
                attempted.set()
                with journal_sender_gate(second):
                    completed.set()

            with journal_sender_gate(first):
                worker = Thread(target=worker_body)
                worker.start()
                self.assertTrue(attempted.wait(1.0))
                self.assertFalse(completed.wait(0.05))
            worker.join(1.0)
            self.assertFalse(worker.is_alive())
            self.assertTrue(completed.is_set())

    def test_recovery_owner_mutation_uses_same_gate(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            attempted = Event()
            completed = Event()

            def start_owner():
                attempted.set()
                recovery.start("owner-1")
                completed.set()

            with journal_sender_gate(store):
                worker = Thread(target=start_owner)
                worker.start()
                self.assertTrue(attempted.wait(1.0))
                self.assertFalse(completed.wait(0.05))
            worker.join(1.0)
            self.assertFalse(worker.is_alive())
            self.assertTrue(completed.is_set())
            self.assertEqual(recovery.owner.owner_id, "owner-1")

    def test_same_thread_reentry_fails_instead_of_deadlocking(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            first = JournalStore(path)
            second = JournalStore(path)
            with journal_sender_gate(first):
                with self.assertRaisesRegex(RuntimeError, "re-entry"):
                    with journal_sender_gate(second):
                        self.fail("same-thread re-entry must not enter")

    def test_spawned_process_waits_for_same_journal_gate(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            context = mp.get_context("spawn")
            started = context.Event()
            entered = context.Event()
            process = None
            with journal_sender_gate(store):
                process = context.Process(
                    target=_process_gate_contender,
                    args=(path, started, entered),
                )
                process.start()
                self.assertTrue(started.wait(5.0))
                self.assertFalse(
                    entered.wait(0.05),
                    "spawned process escaped while sender gate was held",
                )
            self.assertTrue(entered.wait(5.0))
            process.join(5.0)
            if process.is_alive():
                process.terminate()
                process.join(2.0)
                self.fail("spawned process did not exit after gate release")
            self.assertEqual(process.exitcode, 0)

    @unittest.skipUnless(
        os.name != "nt" and "fork" in mp.get_all_start_methods(),
        "fork inheritance regression is POSIX-only",
    )
    def test_forked_process_does_not_inherit_locked_thread_gate(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            context = mp.get_context("fork")
            started = context.Event()
            entered = context.Event()
            process = None
            with journal_sender_gate(store):
                process = context.Process(
                    target=_process_gate_contender,
                    args=(path, started, entered),
                )
                process.start()
                self.assertTrue(started.wait(2.0))
                self.assertFalse(
                    entered.wait(0.05),
                    "forked process escaped while sender gate was held",
                )
            self.assertTrue(
                entered.wait(2.0),
                "forked child retained an inherited locked thread gate",
            )
            process.join(2.0)
            if process.is_alive():
                process.terminate()
                process.join(2.0)
                self.fail("forked child deadlocked after parent gate release")
            self.assertEqual(process.exitcode, 0)


    def test_durable_recovery_rejects_legacy_transfer_booleans(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            recovery.start("owner-1")
            with self.assertRaisesRegex(
                PermissionError,
                "independently issued takeover evidence",
            ):
                recovery.transfer_owner(
                    new_owner_id="owner-2",
                    old_sender_fenced=True,
                    reconciled=True,
                )

    def test_durable_recovery_rejects_caller_mutated_attempt_resolution(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            attempt = OutboundAttempt(
                attempt_id="attempt-1",
                intent_id="intent-1",
                owner_epoch=1,
            )
            with self.assertRaisesRegex(
                PermissionError,
                "journal-issued reconciliation checkpoint",
            ):
                recovery.resolve_attempt(attempt)

if __name__ == "__main__":
    unittest.main()
