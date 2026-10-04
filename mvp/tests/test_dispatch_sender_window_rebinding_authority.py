from __future__ import annotations

from contextlib import contextmanager
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


class DispatchSenderWindowRebindingAuthorityTests(unittest.TestCase):
    def test_rebound_sender_window_cannot_hide_pending_takeover(self) -> None:
        with TemporaryDirectory() as directory:
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
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token="host-a",
                owner_epoch=1,
            )
            outbound = 0

            def authority_check(_intent_hash, _now):
                return True, "allowed"

            def transport_send(_client_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"provider_order_id": "must-not-send"}

            @contextmanager
            def forged_sender_window(_store, *, owner_scope):
                self.assertEqual(owner_scope, "PAPER:acct")
                yield object()

            with patch(
                "mvp.autotrade_mvp.dispatch.sender_authority_window",
                new=forged_sender_window,
            ):
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

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(
                outcome.reason,
                "sender_authority_gate_failed:SenderAuthorityError",
            )
            self.assertEqual(outbound, 0)


if __name__ == "__main__":
    unittest.main()
