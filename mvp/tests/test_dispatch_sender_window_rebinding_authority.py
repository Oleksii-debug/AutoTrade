from __future__ import annotations

from contextlib import contextmanager
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import dispatch as dispatch_module
from mvp.autotrade_mvp.dispatch import GuardedDispatcher
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


if __name__ == "__main__":
    unittest.main()
