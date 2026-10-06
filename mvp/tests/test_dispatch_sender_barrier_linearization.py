from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.dispatch as dispatch_module
from mvp.autotrade_mvp.dispatch import DispatchBlocked, GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


NOW = "2026-10-04T00:40:00Z"


def _append_control_mutation(store: JournalStore, *, event_id: str) -> None:
    payload = {"marker": event_id}
    store.append_event(
        {
            "event_id": event_id,
            "event_type": "ConcurrentControlChanged",
            "aggregate_type": "concurrent_control",
            "aggregate_id": "global",
            "aggregate_version": "1",
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": NOW,
        }
    )


class DispatchSenderBarrierLinearizationTests(unittest.TestCase):
    def store(self, directory: str) -> JournalStore:
        return JournalStore(f"{directory}/journal.sqlite3")

    def test_journal_mutation_after_sender_check_blocks_before_provider_bytes(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token="owner-a",
                owner_epoch=1,
            )
            outbound = 0

            def sender_check(_owner: str, _epoch: int) -> None:
                _append_control_mutation(store, event_id="control-after-sender-check")

            def transport(_client_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"ok": True}

            outcome = dispatcher.dispatch(
                attempt_id="race-sender",
                intent_id="intent-sender",
                intent_hash="hash-sender",
                provider="provider",
                request={"side": "BUY"},
                now=NOW,
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=transport,
                sender_check=sender_check,
            )

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(
                outcome.reason,
                "journal_changed_during_final_send_validation",
            )
            self.assertEqual(outbound, 0)
            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("race-sender"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertNotIn(
                "SubmissionSending",
                [event["event_type"] for event in events],
            )

    def test_journal_mutation_during_final_authority_recheck_is_zero_wire(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="LIVE",
                account_id="acct",
                owner_token="owner-a",
                owner_epoch=1,
            )
            authority_calls = 0
            outbound = 0

            def authority(_intent_hash: str, _now: str):
                nonlocal authority_calls
                authority_calls += 1
                if authority_calls == 2:
                    _append_control_mutation(
                        store,
                        event_id="control-during-final-authority",
                    )
                return True, "allowed"

            def transport(_client_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"ok": True}

            outcome = dispatcher.dispatch(
                attempt_id="race-authority",
                intent_id="intent-authority",
                intent_hash="hash-authority",
                provider="provider",
                request={"side": "SELL"},
                now=NOW,
                authority_check=authority,
                transport_send=transport,
                sender_check=lambda _owner, _epoch: None,
            )

            self.assertEqual(authority_calls, 2)
            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(
                outcome.reason,
                "journal_changed_during_final_send_validation",
            )
            self.assertEqual(outbound, 0)
            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("race-authority"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

    def test_unchanged_journal_commits_sending_before_provider_call(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token="owner-a",
                owner_epoch=1,
            )
            outbound = 0

            def transport(_client_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                events_before_wire = store.load_events(
                    "submission_attempt",
                    dispatcher._aggregate_id("stable-send"),
                )
                self.assertEqual(
                    [event["event_type"] for event in events_before_wire],
                    ["SubmissionPrepared", "SubmissionSending"],
                )
                outbound += 1
                return {"ok": True}

            outcome = dispatcher.dispatch(
                attempt_id="stable-send",
                intent_id="intent-stable",
                intent_hash="hash-stable",
                provider="provider",
                request={"side": "BUY"},
                now=NOW,
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=transport,
                sender_check=lambda _owner, _epoch: None,
            )

            self.assertEqual(outcome.status, "SENT")
            self.assertEqual(outbound, 1)
            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("stable-send"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )

    def test_public_journal_cas_method_replacement_cannot_redirect_final_barrier(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token="owner-a",
                owner_epoch=1,
            )
            outbound = 0

            def transport(_client_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"ok": True}

            with (
                patch.object(
                    JournalStore,
                    "current_journal_sequence",
                    side_effect=AssertionError(
                        "public current_journal_sequence replacement must not run"
                    ),
                ),
                patch.object(
                    JournalStore,
                    "commit_command",
                    side_effect=AssertionError(
                        "public commit_command replacement must not run"
                    ),
                ),
            ):
                outcome = dispatcher.dispatch(
                    attempt_id="canonical-cas-methods",
                    intent_id="intent-canonical-cas-methods",
                    intent_hash="hash-canonical-cas-methods",
                    provider="provider",
                    request={"side": "BUY"},
                    now=NOW,
                    authority_check=lambda _intent_hash, _now: (True, "allowed"),
                    transport_send=transport,
                    sender_check=lambda _owner, _epoch: None,
                )

            self.assertEqual(outcome.status, "SENT")
            self.assertEqual(outbound, 1)
            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("canonical-cas-methods"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )

    def test_concurrent_committed_send_barrier_is_unknown_not_blocked(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token="owner-a",
                owner_epoch=1,
            )
            outbound = 0
            captured_client_id = ""

            def sender_check(_owner: str, _epoch: int) -> None:
                cut = store.current_journal_sequence()
                inserted = dispatcher._append(
                    attempt_id="concurrent-sending",
                    event_type="SubmissionSending",
                    version=2,
                    payload={
                        "client_order_id": captured_client_id,
                        "owner_token": "owner-a",
                        "owner_epoch": 1,
                        "reason": "final_send_barrier_passed",
                    },
                    now=NOW,
                    expected_journal_sequence=cut,
                )
                self.assertTrue(inserted.inserted)

            def transport(client_id, _request, final_guard):
                nonlocal outbound, captured_client_id
                captured_client_id = client_id
                final_guard()
                outbound += 1
                return {"ok": True}

            outcome = dispatcher.dispatch(
                attempt_id="concurrent-sending",
                intent_id="intent-concurrent-sending",
                intent_hash="hash-concurrent-sending",
                provider="provider",
                request={"side": "BUY"},
                now=NOW,
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=transport,
                sender_check=sender_check,
            )

            self.assertEqual(outcome.status, "UNKNOWN")
            self.assertEqual(
                outcome.reason,
                "concurrent_send_barrier_already_committed",
            )
            self.assertEqual(outbound, 0)
            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("concurrent-sending"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending"],
            )

    def test_callback_global_retarget_cannot_replace_captured_journal_cut_authority(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token="owner-a",
                owner_epoch=1,
            )
            original_sequence = dispatch_module._CANONICAL_JOURNAL_CURRENT_SEQUENCE
            original_commit = dispatch_module._CANONICAL_JOURNAL_COMMIT_COMMAND
            forged_sequence_called = False
            forged_commit_called = False
            outbound = 0

            def forged_sequence(_store):
                nonlocal forged_sequence_called
                forged_sequence_called = True
                raise AssertionError("retargeted journal sequence callable executed")

            def forged_commit(*_args, **_kwargs):
                nonlocal forged_commit_called
                forged_commit_called = True
                raise AssertionError("retargeted journal commit callable executed")

            def barrier_clock():
                dispatch_module._CANONICAL_JOURNAL_CURRENT_SEQUENCE = forged_sequence
                return NOW

            def sender_check(_owner, _epoch):
                dispatch_module._CANONICAL_JOURNAL_COMMIT_COMMAND = forged_commit

            def transport(_client_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"ok": True}

            try:
                outcome = dispatcher.dispatch(
                    attempt_id="callback-global-retarget",
                    intent_id="intent-callback-global-retarget",
                    intent_hash="hash-callback-global-retarget",
                    provider="provider",
                    request={"side": "BUY"},
                    now=NOW,
                    authority_check=lambda _intent_hash, _now: (True, "allowed"),
                    transport_send=transport,
                    final_barrier_clock=barrier_clock,
                    sender_check=sender_check,
                )
            finally:
                dispatch_module._CANONICAL_JOURNAL_CURRENT_SEQUENCE = original_sequence
                dispatch_module._CANONICAL_JOURNAL_COMMIT_COMMAND = original_commit

            self.assertFalse(forged_sequence_called)
            self.assertFalse(forged_commit_called)
            self.assertEqual(outcome.status, "SENT")
            self.assertEqual(outbound, 1)
            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("callback-global-retarget"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )

    def test_committed_sending_barrier_replay_cannot_authorize_second_send(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token="owner-a",
                owner_epoch=1,
            )
            dispatcher._append(
                attempt_id="one-use",
                event_type="SubmissionPrepared",
                version=1,
                payload={"prepared": True},
                now=NOW,
            )
            cut = store.current_journal_sequence()
            sending_payload = {
                "client_order_id": "client-one-use",
                "owner_token": "owner-a",
                "owner_epoch": 1,
                "reason": "final_send_barrier_passed",
            }
            first = dispatcher._append(
                attempt_id="one-use",
                event_type="SubmissionSending",
                version=2,
                payload=sending_payload,
                now=NOW,
                expected_journal_sequence=cut,
            )
            self.assertTrue(first.inserted)

            with self.assertRaisesRegex(
                DispatchBlocked,
                "send_barrier_already_committed",
            ):
                dispatcher._append(
                    attempt_id="one-use",
                    event_type="SubmissionSending",
                    version=2,
                    payload=sending_payload,
                    now=NOW,
                    expected_journal_sequence=cut,
                )

            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("one-use"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending"],
            )


if __name__ == "__main__":
    unittest.main()
