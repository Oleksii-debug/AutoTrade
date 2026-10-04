from __future__ import annotations

from contextlib import contextmanager
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import dispatch as dispatch_module
from mvp.autotrade_mvp.dispatch import (
    GuardedDispatcher,
    submission_attempt_aggregate_id,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


class DispatchSenderWindowRebindingAuthorityTests(unittest.TestCase):
    @staticmethod
    def _pending_takeover_store(directory: str) -> JournalStore:
        store = JournalStore(f"{directory}/journal.sqlite3")
        takeover_payload = {"owner_scope": "PAPER:acct"}
        JournalStore.append_event(
            store,
            {
                "event_id": "dispatch-window-rebind-pending-takeover",
                "event_type": "RecoveryTakeoverStarted",
                "aggregate_type": "recovery_takeover",
                "aggregate_id": "takeover/dispatch-window-rebind",
                "aggregate_version": "1",
                "payload": takeover_payload,
                "payload_hash": payload_digest(takeover_payload),
                "committed_at": "2026-10-04T20:00:00Z",
            },
        )
        return store

    @staticmethod
    def _dispatcher(store: JournalStore) -> GuardedDispatcher:
        return GuardedDispatcher(
            store,
            environment="PAPER",
            account_id="acct",
            owner_token="host-a",
            owner_epoch=1,
        )

    def _dispatch(self, dispatcher: GuardedDispatcher, authority_check):
        outbound = 0

        def transport_send(_client_id, _request, final_guard):
            nonlocal outbound
            final_guard()
            outbound += 1
            return {"provider_order_id": "must-not-send"}

        outcome = dispatcher.dispatch(
            attempt_id="dispatch-window-rebind-attempt",
            intent_id="dispatch-window-rebind-intent",
            intent_hash="intent-hash",
            provider="SIMULATED",
            request={"side": "BUY"},
            now="2026-10-04T20:01:00Z",
            authority_check=authority_check,
            transport_send=transport_send,
            sender_check=lambda _owner, _epoch: None,
        )
        return outcome, outbound

    @staticmethod
    def _submission_event_types(store: JournalStore) -> list[str]:
        aggregate_id = submission_attempt_aggregate_id(
            environment="PAPER",
            account_id="acct",
            attempt_id="dispatch-window-rebind-attempt",
        )
        return [
            event["event_type"]
            for event in JournalStore.load_events(
                store,
                "submission_attempt",
                aggregate_id,
            )
        ]

    @staticmethod
    def _append_race_probe(store: JournalStore, suffix: str) -> None:
        payload = {"reason": suffix}
        JournalStore.append_event(
            store,
            {
                "event_id": f"dispatch-final-cut-race-{suffix}",
                "event_type": "DispatchFinalCutRaceProbe",
                "aggregate_type": "test_probe",
                "aggregate_id": f"dispatch-final-cut-race-{suffix}",
                "aggregate_version": "1",
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "committed_at": "2026-10-04T20:01:00Z",
            },
        )

    def test_rebound_sender_window_cannot_hide_pending_takeover(self) -> None:
        with TemporaryDirectory() as directory:
            dispatcher = self._dispatcher(self._pending_takeover_store(directory))

            @contextmanager
            def forged_sender_window(_store, *, owner_scope):
                self.assertEqual(owner_scope, "PAPER:acct")
                yield object()

            with patch(
                "mvp.autotrade_mvp.dispatch.sender_authority_window",
                new=forged_sender_window,
            ):
                outcome, outbound = self._dispatch(
                    dispatcher,
                    lambda _intent_hash, _now: (True, "allowed"),
                )

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(
                outcome.reason,
                "sender_authority_gate_failed:SenderAuthorityError",
            )
            self.assertEqual(outbound, 0)

    def test_private_canonical_sender_window_rebind_before_call_is_never_executed(self) -> None:
        with TemporaryDirectory() as directory:
            dispatcher = self._dispatcher(self._pending_takeover_store(directory))
            calls = []

            @contextmanager
            def forged_sender_window(_store, *, owner_scope):
                calls.append(owner_scope)
                yield object()

            with patch.object(
                dispatch_module,
                "_CANONICAL_SENDER_AUTHORITY_WINDOW",
                new=forged_sender_window,
            ):
                outcome, outbound = self._dispatch(
                    dispatcher,
                    lambda _intent_hash, _now: (True, "allowed"),
                )

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(
                outcome.reason,
                "sender_authority_gate_failed:SenderAuthorityError",
            )
            self.assertEqual(outbound, 0)
            self.assertEqual(calls, [])

    def test_caller_callback_cannot_retarget_captured_sender_window(self) -> None:
        with TemporaryDirectory() as directory:
            dispatcher = self._dispatcher(self._pending_takeover_store(directory))
            active_patches = []
            mutated = False

            @contextmanager
            def forged_sender_window(_store, *, owner_scope):
                self.assertEqual(owner_scope, "PAPER:acct")
                yield object()

            def authority_check(_intent_hash, _now):
                nonlocal mutated
                if not mutated:
                    mutated = True
                    for name in (
                        "sender_authority_window",
                        "_CANONICAL_SENDER_AUTHORITY_WINDOW",
                    ):
                        patcher = patch.object(
                            dispatch_module,
                            name,
                            new=forged_sender_window,
                        )
                        patcher.start()
                        active_patches.append(patcher)
                return True, "allowed"

            try:
                outcome, outbound = self._dispatch(dispatcher, authority_check)
            finally:
                for patcher in reversed(active_patches):
                    patcher.stop()

            self.assertTrue(mutated)
            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(
                outcome.reason,
                "sender_authority_gate_failed:SenderAuthorityError",
            )
            self.assertEqual(outbound, 0)

    def test_private_current_sequence_rebind_cannot_preapprove_intervening_event(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = self._dispatcher(store)
            original_current = JournalStore.current_journal_sequence
            calls = []
            checks = 0

            def forged_current_sequence(target_store):
                calls.append(target_store)
                return original_current(target_store) + 1

            def authority_check(_intent_hash, _now):
                nonlocal checks
                checks += 1
                if checks == 2:
                    self._append_race_probe(store, "current-sequence")
                return True, "allowed"

            with patch.object(
                dispatch_module,
                "_CANONICAL_JOURNAL_CURRENT_SEQUENCE",
                new=forged_current_sequence,
            ):
                outcome, outbound = self._dispatch(dispatcher, authority_check)

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(
                outcome.reason,
                "journal_changed_during_final_send_validation",
            )
            self.assertEqual(outbound, 0)
            self.assertEqual(calls, [])

    def test_private_commit_command_rebind_cannot_strip_final_cut_cas(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = self._dispatcher(store)
            original_commit = JournalStore.commit_command
            calls = []
            checks = 0

            def forged_commit(target_store, *args, **kwargs):
                calls.append(target_store)
                kwargs.pop("expected_journal_sequence", None)
                return original_commit(target_store, *args, **kwargs)

            def authority_check(_intent_hash, _now):
                nonlocal checks
                checks += 1
                if checks == 2:
                    self._append_race_probe(store, "commit-command")
                return True, "allowed"

            with patch.object(
                dispatch_module,
                "_CANONICAL_JOURNAL_COMMIT_COMMAND",
                new=forged_commit,
            ):
                outcome, outbound = self._dispatch(dispatcher, authority_check)

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(
                outcome.reason,
                "journal_changed_during_final_send_validation",
            )
            self.assertEqual(outbound, 0)
            self.assertEqual(calls, [])

    def test_rebound_append_event_cannot_erase_durable_send_chronology(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = self._dispatcher(store)

            def forged_append(_store, _envelope, *args, **kwargs):
                return SimpleNamespace(inserted=True)

            with patch.object(JournalStore, "append_event", new=forged_append):
                outcome, outbound = self._dispatch(
                    dispatcher,
                    lambda _intent_hash, _now: (True, "allowed"),
                )

            self.assertEqual(outcome.status, "SENT")
            self.assertEqual(outbound, 1)
            self.assertEqual(
                self._submission_event_types(store),
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )

    def test_callback_rebound_append_event_cannot_erase_terminal_send_state(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = self._dispatcher(store)
            patcher = None
            mutated = False

            def forged_append(_store, _envelope, *args, **kwargs):
                return SimpleNamespace(inserted=True)

            def authority_check(_intent_hash, _now):
                nonlocal patcher, mutated
                if not mutated:
                    mutated = True
                    patcher = patch.object(
                        JournalStore,
                        "append_event",
                        new=forged_append,
                    )
                    patcher.start()
                return True, "allowed"

            try:
                outcome, outbound = self._dispatch(dispatcher, authority_check)
            finally:
                if patcher is not None:
                    patcher.stop()

            self.assertTrue(mutated)
            self.assertEqual(outcome.status, "SENT")
            self.assertEqual(outbound, 1)
            self.assertEqual(
                self._submission_event_types(store),
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )


if __name__ == "__main__":
    unittest.main()
