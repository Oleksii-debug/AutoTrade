"""Section-6 zero-wire recovery at the durable Prepared/send boundary."""

from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore


class _ProcessDeath(BaseException):
    pass


class PreparedZeroWireSection6Tests(unittest.TestCase):
    @staticmethod
    def authority(_intent_hash, _now):
        return True, "allowed"

    @staticmethod
    def _dispatcher(store, *, owner):
        return GuardedDispatcher(
            store,
            environment="SIMULATION",
            account_id="section6-account",
            owner_token=owner,
            prepared_lease_seconds=1,
        )

    def _leave_prepared(self, dispatcher):
        def crash_before_guard(_client_order_id, _request, _final_guard):
            raise _ProcessDeath("crash-before-final-send-guard")

        with self.assertRaisesRegex(
            _ProcessDeath,
            "crash-before-final-send-guard",
        ):
            dispatcher.dispatch(
                attempt_id="section6-attempt",
                intent_id="section6-intent",
                intent_hash="section6-intent-hash",
                provider="simulated",
                request={"quantity": "1"},
                now="2026-10-06T10:00:00Z",
                authority_check=self.authority,
                transport_send=crash_before_guard,
            )
        events = dispatcher._events("section6-attempt")
        self.assertEqual(
            [event["event_type"] for event in events],
            ["SubmissionPrepared"],
        )

    def test_active_prepared_lease_remains_in_progress_and_zero_wire(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = self._dispatcher(store, owner="owner-a")
            self._leave_prepared(dispatcher)
            result = dispatcher.dispatch(
                attempt_id="section6-attempt",
                intent_id="section6-intent",
                intent_hash="section6-intent-hash",
                provider="simulated",
                request={"quantity": "1"},
                now="2026-10-06T10:00:00Z",
                authority_check=self.authority,
                transport_send=lambda *_args: self.fail(
                    "active Prepared recovery reached provider transport"
                ),
            )
            self.assertEqual(result.status, "IN_PROGRESS")
            self.assertEqual(result.reason, "prepared_owner_lease_active")
            self.assertEqual(
                [event["event_type"] for event in dispatcher._events("section6-attempt")],
                ["SubmissionPrepared"],
            )

    def test_expired_prepared_terminalizes_blocked_without_wire(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = self._dispatcher(store, owner="owner-a")
            self._leave_prepared(dispatcher)
            result = dispatcher.dispatch(
                attempt_id="section6-attempt",
                intent_id="section6-intent",
                intent_hash="section6-intent-hash",
                provider="simulated",
                request={"quantity": "1"},
                now="2026-10-06T10:00:02Z",
                authority_check=self.authority,
                transport_send=lambda *_args: self.fail(
                    "expired Prepared recovery reached provider transport"
                ),
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(
                result.reason,
                "prepared_owner_lease_expired_before_send",
            )
            events = dispatcher._events("section6-attempt")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertNotIn(
                "SubmissionSending",
                [event["event_type"] for event in events],
            )
            self.assertNotIn(
                "SubmissionUnknown",
                [event["event_type"] for event in events],
            )

    def test_send_barrier_winning_recovery_cas_converges_unknown(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            original = self._dispatcher(store, owner="original-owner")
            recovery = self._dispatcher(store, owner="recovery-owner")
            recovery_results = []

            def transport(_client_order_id, _request, final_guard):
                recovery_append = recovery._append

                def racing_append(*, attempt_id, event_type, version, payload, now):
                    if event_type == "SubmissionBlocked":
                        # Force the opposite race ordering from the BLOCKED-wins
                        # regression: Sending commits after recovery read Prepared
                        # but before recovery can commit its version-2 Blocked.
                        final_guard()
                    return recovery_append(
                        attempt_id=attempt_id,
                        event_type=event_type,
                        version=version,
                        payload=payload,
                        now=now,
                    )

                with patch.object(recovery, "_append", side_effect=racing_append):
                    recovered = recovery.dispatch(
                        attempt_id="section6-send-wins",
                        intent_id="section6-send-wins-intent",
                        intent_hash="section6-send-wins-hash",
                        provider="simulated",
                        request={"quantity": "1"},
                        now="2026-10-06T10:00:02Z",
                        authority_check=self.authority,
                        transport_send=lambda *_args: self.fail(
                            "recovery reached provider transport"
                        ),
                    )
                recovery_results.append(recovered)
                raise _ProcessDeath("crash-after-send-barrier-before-wire")

            with self.assertRaisesRegex(
                _ProcessDeath,
                "crash-after-send-barrier-before-wire",
            ):
                original.dispatch(
                    attempt_id="section6-send-wins",
                    intent_id="section6-send-wins-intent",
                    intent_hash="section6-send-wins-hash",
                    provider="simulated",
                    request={"quantity": "1"},
                    now="2026-10-06T10:00:00Z",
                    authority_check=self.authority,
                    transport_send=transport,
                )

            self.assertEqual(len(recovery_results), 1)
            self.assertEqual(recovery_results[0].status, "UNKNOWN")
            self.assertEqual(
                recovery_results[0].reason,
                "recovered_after_send_barrier_without_terminal_result",
            )
            events = original._events("section6-send-wins")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )
            self.assertNotIn(
                "SubmissionBlocked",
                [event["event_type"] for event in events],
            )

    def test_recovery_fence_blocks_late_original_final_guard_before_wire(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            original = self._dispatcher(store, owner="original-owner")
            recovery = self._dispatcher(store, owner="recovery-owner")
            recovery_results = []
            wire_sends = []

            def transport(client_order_id, _request, final_guard):
                recovered = recovery.dispatch(
                    attempt_id="section6-race",
                    intent_id="section6-race-intent",
                    intent_hash="section6-race-hash",
                    provider="simulated",
                    request={"quantity": "1"},
                    now="2026-10-06T10:00:02Z",
                    authority_check=self.authority,
                    transport_send=lambda *_args: self.fail(
                        "recovery reached provider transport"
                    ),
                )
                recovery_results.append(recovered)
                final_guard()
                wire_sends.append(client_order_id)
                return {"provider_order_id": "must-not-exist"}

            result = original.dispatch(
                attempt_id="section6-race",
                intent_id="section6-race-intent",
                intent_hash="section6-race-hash",
                provider="simulated",
                request={"quantity": "1"},
                now="2026-10-06T10:00:00Z",
                authority_check=self.authority,
                transport_send=transport,
            )
            self.assertEqual(len(recovery_results), 1)
            self.assertEqual(recovery_results[0].status, "BLOCKED")
            self.assertEqual(
                recovery_results[0].reason,
                "prepared_owner_lease_expired_before_send",
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(
                result.reason,
                "prepared_owner_lease_expired_before_send",
            )
            self.assertEqual(wire_sends, [])
            events = original._events("section6-race")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )


if __name__ == "__main__":
    unittest.main()
