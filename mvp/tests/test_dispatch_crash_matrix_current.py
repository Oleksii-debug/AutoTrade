from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
)
from mvp.autotrade_mvp.persistence import JournalStore


class SimulatedProcessDeath(BaseException):
    """Model abrupt process termination outside ordinary Exception recovery."""


class DispatchCrashMatrixCurrentTests(unittest.TestCase):
    def _path(self, directory: str) -> str:
        return f"{directory}/journal.sqlite3"

    def _dispatcher(
        self,
        path: str,
        *,
        owner_token: str = "owner-a",
        prepared_lease_seconds: int = 60,
        environment: str = "SIMULATION",
    ) -> GuardedDispatcher:
        return GuardedDispatcher(
            JournalStore(path),
            environment=environment,
            account_id="acct",
            owner_token=owner_token,
            prepared_lease_seconds=prepared_lease_seconds,
        )

    @staticmethod
    def _allow(_intent_hash, _now):
        return True, "allowed"

    def _dispatch(
        self,
        dispatcher: GuardedDispatcher,
        *,
        attempt_id: str,
        now: str,
        transport,
        authority_check=None,
        sender_check=None,
        final_barrier_clock=None,
    ):
        return dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id="intent-1",
            intent_hash="sha256:" + "1" * 64,
            provider="provider",
            request={"side": "BUY", "quantity": "1"},
            now=now,
            authority_check=authority_check or self._allow,
            transport_send=transport,
            sender_check=sender_check,
            final_barrier_clock=final_barrier_clock,
            submission_scope={"endpoint": "/orders"},
        )

    def _events(
        self,
        path: str,
        dispatcher: GuardedDispatcher,
        attempt_id: str,
    ) -> list[dict]:
        return JournalStore.load_events(
            JournalStore(path),
            "submission_attempt",
            dispatcher._aggregate_id(attempt_id),
        )

    def _event_types(
        self,
        path: str,
        dispatcher: GuardedDispatcher,
        attempt_id: str,
    ) -> list[str]:
        return [
            event["event_type"]
            for event in self._events(path, dispatcher, attempt_id)
        ]

    @staticmethod
    def _forbidden_transport(*_args):
        raise AssertionError("restart must not emit a blind provider request")

    def test_empty_prepared_cut_is_safe_for_one_fresh_send(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            first_process = self._dispatcher(path)

            self.assertEqual(
                self._event_types(path, first_process, "empty-cut"),
                [],
            )

            outbound = 0

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )

            restarted = self._dispatcher(path, owner_token="owner-b")
            result = self._dispatch(
                restarted,
                attempt_id="empty-cut",
                now="2026-10-06T16:30:01Z",
                transport=transport,
            )
            self.assertEqual(result.status, "SENT")
            self.assertEqual(outbound, 1)
            self.assertEqual(
                self._event_types(path, restarted, "empty-cut"),
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionSent",
                ],
            )

    def test_death_after_prepared_commit_recovers_zero_wire_without_resend(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)

            def die_after_prepared(_intent_hash, _now):
                raise SimulatedProcessDeath("after Prepared commit")

            with self.assertRaisesRegex(
                SimulatedProcessDeath,
                "after Prepared commit",
            ):
                self._dispatch(
                    dispatcher,
                    attempt_id="crash-after-prepared",
                    now="2026-10-06T16:31:00Z",
                    transport=self._forbidden_transport,
                    authority_check=die_after_prepared,
                )

            self.assertEqual(
                self._event_types(path, dispatcher, "crash-after-prepared"),
                ["SubmissionPrepared"],
            )

            restarted = self._dispatcher(path, owner_token="owner-b")
            active = self._dispatch(
                restarted,
                attempt_id="crash-after-prepared",
                now="2026-10-06T16:31:01Z",
                transport=self._forbidden_transport,
            )
            self.assertEqual(active.status, "IN_PROGRESS")
            self.assertEqual(active.reason, "prepared_owner_lease_active")

            expired = self._dispatch(
                restarted,
                attempt_id="crash-after-prepared",
                now="2026-10-06T16:32:01Z",
                transport=self._forbidden_transport,
            )
            self.assertEqual(expired.status, "BLOCKED")
            self.assertEqual(
                expired.reason,
                "prepared_owner_lease_expired_before_send",
            )
            self.assertEqual(
                self._event_types(path, restarted, "crash-after-prepared"),
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

    def test_death_at_final_authority_check_stays_pre_sending_and_zero_wire(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)
            authority_calls = 0
            outbound = 0

            def authority(_intent_hash, _now):
                nonlocal authority_calls
                authority_calls += 1
                if authority_calls == 2:
                    raise SimulatedProcessDeath("during final authority check")
                return True, "allowed"

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                raise AssertionError("wire must remain unreachable")

            with self.assertRaisesRegex(
                SimulatedProcessDeath,
                "during final authority check",
            ):
                self._dispatch(
                    dispatcher,
                    attempt_id="crash-final-authority",
                    now="2026-10-06T16:33:00Z",
                    transport=transport,
                    authority_check=authority,
                )

            self.assertEqual(authority_calls, 2)
            self.assertEqual(outbound, 0)
            self.assertEqual(
                self._event_types(path, dispatcher, "crash-final-authority"),
                ["SubmissionPrepared"],
            )

            restarted = self._dispatcher(path, owner_token="owner-b")
            recovered = self._dispatch(
                restarted,
                attempt_id="crash-final-authority",
                now="2026-10-06T16:34:01Z",
                transport=self._forbidden_transport,
            )
            self.assertEqual(recovered.status, "BLOCKED")
            self.assertEqual(
                recovered.reason,
                "prepared_owner_lease_expired_before_send",
            )

    def test_paper_sender_fence_death_stays_pre_sending_and_zero_wire(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path, environment="PAPER")
            sender_calls = 0
            outbound = 0

            def sender_check(_owner_token, _owner_epoch):
                nonlocal sender_calls
                sender_calls += 1
                raise SimulatedProcessDeath("inside PAPER sender fence")

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                raise AssertionError("wire must remain unreachable")

            with self.assertRaisesRegex(
                SimulatedProcessDeath,
                "inside PAPER sender fence",
            ):
                self._dispatch(
                    dispatcher,
                    attempt_id="crash-paper-sender",
                    now="2026-10-06T16:34:00Z",
                    transport=transport,
                    sender_check=sender_check,
                )

            self.assertEqual(sender_calls, 1)
            self.assertEqual(outbound, 0)
            self.assertEqual(
                self._event_types(path, dispatcher, "crash-paper-sender"),
                ["SubmissionPrepared"],
            )

            restarted = self._dispatcher(
                path,
                owner_token="owner-b",
                environment="PAPER",
            )
            recovered = self._dispatch(
                restarted,
                attempt_id="crash-paper-sender",
                now="2026-10-06T16:35:01Z",
                transport=self._forbidden_transport,
                sender_check=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("recovery must not reacquire sender fence")),
            )
            self.assertEqual(recovered.status, "BLOCKED")
            self.assertEqual(
                recovered.reason,
                "prepared_owner_lease_expired_before_send",
            )
            self.assertEqual(sender_calls, 1)

    def test_death_inside_transport_before_guard_stays_zero_wire(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)

            def transport(_client_order_id, _request, _final_guard):
                raise SimulatedProcessDeath("inside transport before guard")

            with self.assertRaisesRegex(
                SimulatedProcessDeath,
                "inside transport before guard",
            ):
                self._dispatch(
                    dispatcher,
                    attempt_id="crash-before-guard",
                    now="2026-10-06T16:34:10Z",
                    transport=transport,
                )

            self.assertEqual(
                self._event_types(path, dispatcher, "crash-before-guard"),
                ["SubmissionPrepared"],
            )

            restarted = self._dispatcher(path, owner_token="owner-b")
            recovered = self._dispatch(
                restarted,
                attempt_id="crash-before-guard",
                now="2026-10-06T16:35:11Z",
                transport=self._forbidden_transport,
            )
            self.assertEqual(recovered.status, "BLOCKED")
            self.assertEqual(
                recovered.reason,
                "prepared_owner_lease_expired_before_send",
            )
            self.assertEqual(
                self._event_types(path, restarted, "crash-before-guard"),
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

    def test_death_after_sending_commit_before_wire_is_sticky_unknown(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)
            outbound = 0

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                raise SimulatedProcessDeath("after Sending commit before wire")

            with self.assertRaisesRegex(
                SimulatedProcessDeath,
                "after Sending commit before wire",
            ):
                self._dispatch(
                    dispatcher,
                    attempt_id="crash-after-sending",
                    now="2026-10-06T16:35:00Z",
                    transport=transport,
                )

            self.assertEqual(outbound, 0)
            self.assertEqual(
                self._event_types(path, dispatcher, "crash-after-sending"),
                ["SubmissionPrepared", "SubmissionSending"],
            )

            restarted = self._dispatcher(path, owner_token="owner-b")
            recovered = self._dispatch(
                restarted,
                attempt_id="crash-after-sending",
                now="2026-10-06T16:35:01Z",
                transport=self._forbidden_transport,
            )
            self.assertEqual(recovered.status, "UNKNOWN")
            self.assertEqual(
                recovered.reason,
                "recovered_after_send_barrier_without_terminal_result",
            )
            self.assertEqual(outbound, 0)
            self.assertEqual(
                self._event_types(path, restarted, "crash-after-sending"),
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionUnknown",
                ],
            )

    def test_death_after_wire_before_terminal_is_unknown_without_resend(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)
            outbound = 0

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                raise SimulatedProcessDeath("after wire before terminal")

            with self.assertRaisesRegex(
                SimulatedProcessDeath,
                "after wire before terminal",
            ):
                self._dispatch(
                    dispatcher,
                    attempt_id="crash-after-wire",
                    now="2026-10-06T16:36:00Z",
                    transport=transport,
                )

            self.assertEqual(outbound, 1)
            self.assertEqual(
                self._event_types(path, dispatcher, "crash-after-wire"),
                ["SubmissionPrepared", "SubmissionSending"],
            )

            restarted = self._dispatcher(path, owner_token="owner-b")
            recovered = self._dispatch(
                restarted,
                attempt_id="crash-after-wire",
                now="2026-10-06T16:36:01Z",
                transport=self._forbidden_transport,
            )
            self.assertEqual(recovered.status, "UNKNOWN")
            self.assertEqual(
                recovered.reason,
                "recovered_after_send_barrier_without_terminal_result",
            )
            self.assertEqual(outbound, 1)

    def test_committed_blocked_replays_without_callbacks_after_restart(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)

            blocked = self._dispatch(
                dispatcher,
                attempt_id="lost-blocked-result",
                now="2026-10-06T16:36:30Z",
                transport=self._forbidden_transport,
                authority_check=lambda _intent_hash, _now: (
                    False,
                    "operator_revoked",
                ),
            )
            self.assertEqual(blocked.status, "BLOCKED")
            self.assertEqual(blocked.reason, "operator_revoked")
            self.assertEqual(
                self._event_types(path, dispatcher, "lost-blocked-result"),
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

            restarted = self._dispatcher(path, owner_token="owner-b")
            recovered = self._dispatch(
                restarted,
                attempt_id="lost-blocked-result",
                now="2026-10-06T16:36:31Z",
                transport=self._forbidden_transport,
                authority_check=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("terminal replay must not re-authorize")),
            )
            self.assertEqual(recovered.status, "BLOCKED")
            self.assertEqual(recovered.reason, "operator_revoked")
            self.assertEqual(
                self._event_types(path, restarted, "lost-blocked-result"),
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

    def test_committed_sent_replays_after_caller_result_loss_without_resend(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)
            outbound = 0

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )

            committed = self._dispatch(
                dispatcher,
                attempt_id="lost-sent-result",
                now="2026-10-06T16:37:00Z",
                transport=transport,
            )
            self.assertEqual(committed.status, "SENT")
            self.assertEqual(outbound, 1)

            restarted = self._dispatcher(path, owner_token="owner-b")
            recovered = self._dispatch(
                restarted,
                attempt_id="lost-sent-result",
                now="2026-10-06T16:37:01Z",
                transport=self._forbidden_transport,
            )
            self.assertEqual(recovered.status, "SENT")
            self.assertEqual(recovered.reason, "sent_confirmed")
            self.assertEqual(recovered.response, {"accepted": True})
            self.assertEqual(outbound, 1)
            self.assertEqual(
                self._event_types(path, restarted, "lost-sent-result"),
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionSent",
                ],
            )

    def test_committed_timeout_unknown_replays_without_resend(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)
            outbound = 0

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                raise TimeoutError("reply lost after wire")

            committed = self._dispatch(
                dispatcher,
                attempt_id="lost-timeout-result",
                now="2026-10-06T16:38:00Z",
                transport=transport,
            )
            self.assertEqual(committed.status, "UNKNOWN")
            self.assertEqual(outbound, 1)

            restarted = self._dispatcher(path, owner_token="owner-b")
            recovered = self._dispatch(
                restarted,
                attempt_id="lost-timeout-result",
                now="2026-10-06T16:38:01Z",
                transport=self._forbidden_transport,
            )
            self.assertEqual(recovered.status, "UNKNOWN")
            self.assertEqual(
                recovered.reason,
                "transport_exception_after_send_barrier:TimeoutError",
            )
            self.assertEqual(outbound, 1)
            self.assertEqual(
                self._event_types(path, restarted, "lost-timeout-result"),
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionUnknown",
                ],
            )

    def test_committed_exact_ambiguous_unknown_replays_bound_reason_and_bytes(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)
            outbound = 0
            response = ExactJsonTransportResponse(
                b'{"retCode":10000}',
                http_status=200,
                requires_reconciliation=True,
                ambiguity_reason="provider_ack_ambiguous",
            )

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return response

            committed = self._dispatch(
                dispatcher,
                attempt_id="lost-exact-unknown-result",
                now="2026-10-06T16:39:00Z",
                transport=transport,
            )
            self.assertEqual(committed.status, "UNKNOWN")
            self.assertEqual(committed.reason, "provider_ack_ambiguous")
            self.assertEqual(outbound, 1)

            events = self._events(
                path,
                dispatcher,
                "lost-exact-unknown-result",
            )
            terminal_payload = events[-1]["payload"]
            self.assertEqual(terminal_payload["reason"], "provider_ack_ambiguous")
            self.assertEqual(
                terminal_payload["retry_disposition"],
                "RECONCILE_FIRST",
            )
            self.assertEqual(
                terminal_payload["response_sha256"],
                response.response_sha256,
            )
            self.assertEqual(terminal_payload["response_text"], response.response_text)

            restarted = self._dispatcher(path, owner_token="owner-b")
            recovered = self._dispatch(
                restarted,
                attempt_id="lost-exact-unknown-result",
                now="2026-10-06T16:39:01Z",
                transport=self._forbidden_transport,
            )
            self.assertEqual(recovered.status, "UNKNOWN")
            self.assertEqual(recovered.reason, "provider_ack_ambiguous")
            self.assertEqual(outbound, 1)
            self.assertEqual(
                self._event_types(path, restarted, "lost-exact-unknown-result"),
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionUnknown",
                ],
            )


    def test_death_in_final_barrier_clock_stays_pre_sending_and_zero_wire(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)
            outbound = 0

            def die_in_barrier_clock():
                raise SimulatedProcessDeath("inside final barrier clock")

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                raise AssertionError("wire must remain unreachable")

            with self.assertRaisesRegex(
                SimulatedProcessDeath,
                "inside final barrier clock",
            ):
                self._dispatch(
                    dispatcher,
                    attempt_id="crash-final-clock",
                    now="2026-10-06T16:40:00Z",
                    transport=transport,
                    final_barrier_clock=die_in_barrier_clock,
                )

            self.assertEqual(outbound, 0)
            self.assertEqual(
                self._event_types(path, dispatcher, "crash-final-clock"),
                ["SubmissionPrepared"],
            )

            restarted = self._dispatcher(path, owner_token="owner-b")
            recovered = self._dispatch(
                restarted,
                attempt_id="crash-final-clock",
                now="2026-10-06T16:41:01Z",
                transport=self._forbidden_transport,
                authority_check=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("recovery must not re-authorize")),
                final_barrier_clock=lambda: (
                    _ for _ in ()
                ).throw(AssertionError("recovery must not call barrier clock")),
            )
            self.assertEqual(recovered.status, "BLOCKED")
            self.assertEqual(
                recovered.reason,
                "prepared_owner_lease_expired_before_send",
            )
            self.assertEqual(outbound, 0)

    def test_recovered_sending_unknown_is_restart_idempotent_without_callbacks(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)
            outbound = 0

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                raise SimulatedProcessDeath("after Sending before wire")

            with self.assertRaisesRegex(
                SimulatedProcessDeath,
                "after Sending before wire",
            ):
                self._dispatch(
                    dispatcher,
                    attempt_id="recovery-unknown-idempotent",
                    now="2026-10-06T16:42:00Z",
                    transport=transport,
                )

            self.assertEqual(
                self._event_types(
                    path,
                    dispatcher,
                    "recovery-unknown-idempotent",
                ),
                ["SubmissionPrepared", "SubmissionSending"],
            )

            first_restart = self._dispatcher(path, owner_token="owner-b")
            first = self._dispatch(
                first_restart,
                attempt_id="recovery-unknown-idempotent",
                now="2026-10-06T16:42:01Z",
                transport=self._forbidden_transport,
                authority_check=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("recovery must not re-authorize")),
                final_barrier_clock=lambda: (
                    _ for _ in ()
                ).throw(AssertionError("recovery must not call barrier clock")),
            )
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(
                first.reason,
                "recovered_after_send_barrier_without_terminal_result",
            )

            second_restart = self._dispatcher(path, owner_token="owner-c")
            second = self._dispatch(
                second_restart,
                attempt_id="recovery-unknown-idempotent",
                now="2026-10-06T16:42:02Z",
                transport=self._forbidden_transport,
                authority_check=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("terminal replay must not re-authorize")),
                final_barrier_clock=lambda: (
                    _ for _ in ()
                ).throw(AssertionError("terminal replay must not call barrier clock")),
            )
            self.assertEqual(second.status, "UNKNOWN")
            self.assertEqual(second.reason, first.reason)
            self.assertEqual(outbound, 0)
            self.assertEqual(
                self._event_types(
                    path,
                    second_restart,
                    "recovery-unknown-idempotent",
                ),
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionUnknown",
                ],
            )

    def test_recovered_prepared_blocked_is_restart_idempotent_without_callbacks(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)

            def die_after_prepared(_intent_hash, _now):
                raise SimulatedProcessDeath("after Prepared")

            with self.assertRaisesRegex(SimulatedProcessDeath, "after Prepared"):
                self._dispatch(
                    dispatcher,
                    attempt_id="recovery-blocked-idempotent",
                    now="2026-10-06T16:43:00Z",
                    transport=self._forbidden_transport,
                    authority_check=die_after_prepared,
                )

            first_restart = self._dispatcher(path, owner_token="owner-b")
            first = self._dispatch(
                first_restart,
                attempt_id="recovery-blocked-idempotent",
                now="2026-10-06T16:44:01Z",
                transport=self._forbidden_transport,
                authority_check=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("recovery must not re-authorize")),
            )
            self.assertEqual(first.status, "BLOCKED")
            self.assertEqual(
                first.reason,
                "prepared_owner_lease_expired_before_send",
            )

            second_restart = self._dispatcher(path, owner_token="owner-c")
            second = self._dispatch(
                second_restart,
                attempt_id="recovery-blocked-idempotent",
                now="2026-10-06T16:44:02Z",
                transport=self._forbidden_transport,
                authority_check=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("terminal replay must not re-authorize")),
            )
            self.assertEqual(second.status, "BLOCKED")
            self.assertEqual(second.reason, first.reason)
            self.assertEqual(
                self._event_types(
                    path,
                    second_restart,
                    "recovery-blocked-idempotent",
                ),
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

    def test_clock_before_prepared_never_terminalizes_and_later_converges(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)

            def die_after_prepared(_intent_hash, _now):
                raise SimulatedProcessDeath("after Prepared")

            with self.assertRaisesRegex(SimulatedProcessDeath, "after Prepared"):
                self._dispatch(
                    dispatcher,
                    attempt_id="clock-before-prepared",
                    now="2026-10-06T16:45:00Z",
                    transport=self._forbidden_transport,
                    authority_check=die_after_prepared,
                )

            restarted = self._dispatcher(path, owner_token="owner-b")
            before = self._dispatch(
                restarted,
                attempt_id="clock-before-prepared",
                now="2026-10-06T16:44:59Z",
                transport=self._forbidden_transport,
            )
            self.assertEqual(before.status, "IN_PROGRESS")
            self.assertEqual(before.reason, "clock_before_prepared_timestamp")
            self.assertEqual(
                self._event_types(path, restarted, "clock-before-prepared"),
                ["SubmissionPrepared"],
            )

            active = self._dispatch(
                restarted,
                attempt_id="clock-before-prepared",
                now="2026-10-06T16:45:01Z",
                transport=self._forbidden_transport,
            )
            self.assertEqual(active.status, "IN_PROGRESS")
            self.assertEqual(active.reason, "prepared_owner_lease_active")
            self.assertEqual(
                self._event_types(path, restarted, "clock-before-prepared"),
                ["SubmissionPrepared"],
            )

            expired = self._dispatch(
                restarted,
                attempt_id="clock-before-prepared",
                now="2026-10-06T16:46:01Z",
                transport=self._forbidden_transport,
            )
            self.assertEqual(expired.status, "BLOCKED")
            self.assertEqual(
                expired.reason,
                "prepared_owner_lease_expired_before_send",
            )

    def test_committed_legacy_sent_replays_after_restart_without_resend(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)
            outbound = 0
            response = {
                "accepted": True,
                "source": ["legacy", {"id": 7}],
            }

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return response

            committed = self._dispatch(
                dispatcher,
                attempt_id="lost-legacy-sent-result",
                now="2026-10-06T16:47:00Z",
                transport=transport,
            )
            self.assertEqual(committed.status, "SENT")
            self.assertEqual(committed.response, response)
            self.assertEqual(outbound, 1)

            restarted = self._dispatcher(path, owner_token="owner-b")
            recovered = self._dispatch(
                restarted,
                attempt_id="lost-legacy-sent-result",
                now="2026-10-06T16:47:01Z",
                transport=self._forbidden_transport,
                authority_check=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("terminal replay must not re-authorize")),
            )
            self.assertEqual(recovered.status, "SENT")
            self.assertEqual(recovered.reason, "sent_confirmed")
            self.assertEqual(recovered.response, response)
            self.assertEqual(outbound, 1)
            self.assertEqual(
                self._event_types(
                    path,
                    restarted,
                    "lost-legacy-sent-result",
                ),
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionSent",
                ],
            )


if __name__ == "__main__":
    unittest.main()
