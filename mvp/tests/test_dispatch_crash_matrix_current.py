from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

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
    ) -> GuardedDispatcher:
        return GuardedDispatcher(
            JournalStore(path),
            environment="SIMULATION",
            account_id="acct",
            owner_token=owner_token,
            prepared_lease_seconds=prepared_lease_seconds,
        )

    def _dispatch(
        self,
        dispatcher: GuardedDispatcher,
        *,
        attempt_id: str,
        now: str,
        transport,
    ):
        return dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id="intent-1",
            intent_hash="sha256:" + "1" * 64,
            provider="provider",
            request={"side": "BUY", "quantity": "1"},
            now=now,
            authority_check=lambda _intent_hash, _now: (True, "allowed"),
            transport_send=transport,
            submission_scope={"endpoint": "/orders"},
        )

    def _event_types(
        self,
        path: str,
        dispatcher: GuardedDispatcher,
        attempt_id: str,
    ) -> list[str]:
        events = JournalStore(path).load_events(
            "submission_attempt",
            dispatcher._aggregate_id(attempt_id),
        )
        return [event["event_type"] for event in events]

    def _forbidden_transport(self, *_args):
        raise AssertionError("restart must not emit a blind provider request")

    def test_death_before_prepared_commit_leaves_no_durable_send_authority(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)
            real_append = JournalStore.append_event

            def die_before_prepared(store, envelope, *, outbox_topic=None):
                if envelope["event_type"] == "SubmissionPrepared":
                    raise SimulatedProcessDeath("before Prepared commit")
                return real_append(store, envelope, outbox_topic=outbox_topic)

            with patch.object(JournalStore, "append_event", new=die_before_prepared):
                with self.assertRaisesRegex(
                    SimulatedProcessDeath,
                    "before Prepared commit",
                ):
                    self._dispatch(
                        dispatcher,
                        attempt_id="crash-before-prepared",
                        now="2026-10-06T16:30:00Z",
                        transport=self._forbidden_transport,
                    )

            self.assertEqual(
                self._event_types(path, dispatcher, "crash-before-prepared"),
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
                attempt_id="crash-before-prepared",
                now="2026-10-06T16:30:01Z",
                transport=transport,
            )
            self.assertEqual(result.status, "SENT")
            self.assertEqual(outbound, 1)
            self.assertEqual(
                self._event_types(path, restarted, "crash-before-prepared"),
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
            real_append = JournalStore.append_event

            def die_after_prepared(store, envelope, *, outbox_topic=None):
                result = real_append(store, envelope, outbox_topic=outbox_topic)
                if envelope["event_type"] == "SubmissionPrepared":
                    raise SimulatedProcessDeath("after Prepared commit")
                return result

            with patch.object(JournalStore, "append_event", new=die_after_prepared):
                with self.assertRaisesRegex(
                    SimulatedProcessDeath,
                    "after Prepared commit",
                ):
                    self._dispatch(
                        dispatcher,
                        attempt_id="crash-after-prepared",
                        now="2026-10-06T16:31:00Z",
                        transport=self._forbidden_transport,
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

    def test_death_before_sending_commit_preserves_zero_wire_recovery(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)
            real_append = JournalStore.append_event
            outbound = 0

            def die_before_sending(store, envelope, *, outbox_topic=None):
                if envelope["event_type"] == "SubmissionSending":
                    raise SimulatedProcessDeath("before Sending commit")
                return real_append(store, envelope, outbox_topic=outbox_topic)

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                raise AssertionError("wire must remain unreachable")

            with patch.object(JournalStore, "append_event", new=die_before_sending):
                with self.assertRaisesRegex(
                    SimulatedProcessDeath,
                    "before Sending commit",
                ):
                    self._dispatch(
                        dispatcher,
                        attempt_id="crash-before-sending",
                        now="2026-10-06T16:33:00Z",
                        transport=transport,
                    )

            self.assertEqual(outbound, 0)
            self.assertEqual(
                self._event_types(path, dispatcher, "crash-before-sending"),
                ["SubmissionPrepared"],
            )

            restarted = self._dispatcher(path, owner_token="owner-b")
            recovered = self._dispatch(
                restarted,
                attempt_id="crash-before-sending",
                now="2026-10-06T16:34:01Z",
                transport=self._forbidden_transport,
            )
            self.assertEqual(recovered.status, "BLOCKED")
            self.assertEqual(
                recovered.reason,
                "prepared_owner_lease_expired_before_send",
            )
            self.assertEqual(
                self._event_types(path, restarted, "crash-before-sending"),
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

    def test_death_after_sending_commit_is_sticky_unknown_even_before_wire(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)
            real_append = JournalStore.append_event
            outbound = 0

            def die_after_sending(store, envelope, *, outbox_topic=None):
                result = real_append(store, envelope, outbox_topic=outbox_topic)
                if envelope["event_type"] == "SubmissionSending":
                    raise SimulatedProcessDeath("after Sending commit")
                return result

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                raise AssertionError("wire must remain unreachable")

            with patch.object(JournalStore, "append_event", new=die_after_sending):
                with self.assertRaisesRegex(
                    SimulatedProcessDeath,
                    "after Sending commit",
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

    def test_death_after_sent_commit_replays_terminal_without_resend(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)
            real_append = JournalStore.append_event
            outbound = 0

            def die_after_sent(store, envelope, *, outbox_topic=None):
                result = real_append(store, envelope, outbox_topic=outbox_topic)
                if envelope["event_type"] == "SubmissionSent":
                    raise SimulatedProcessDeath("after Sent commit")
                return result

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )

            with patch.object(JournalStore, "append_event", new=die_after_sent):
                with self.assertRaisesRegex(
                    SimulatedProcessDeath,
                    "after Sent commit",
                ):
                    self._dispatch(
                        dispatcher,
                        attempt_id="crash-after-sent",
                        now="2026-10-06T16:36:00Z",
                        transport=transport,
                    )

            self.assertEqual(outbound, 1)
            self.assertEqual(
                self._event_types(path, dispatcher, "crash-after-sent"),
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionSent",
                ],
            )

            restarted = self._dispatcher(path, owner_token="owner-b")
            recovered = self._dispatch(
                restarted,
                attempt_id="crash-after-sent",
                now="2026-10-06T16:36:01Z",
                transport=self._forbidden_transport,
            )
            self.assertEqual(recovered.status, "SENT")
            self.assertEqual(recovered.reason, "sent_confirmed")
            self.assertEqual(recovered.response, {"accepted": True})
            self.assertEqual(outbound, 1)
            self.assertEqual(
                self._event_types(path, restarted, "crash-after-sent"),
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionSent",
                ],
            )

    def test_death_after_timeout_unknown_commit_replays_without_resend(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)
            real_append = JournalStore.append_event
            outbound = 0

            def die_after_unknown(store, envelope, *, outbox_topic=None):
                result = real_append(store, envelope, outbox_topic=outbox_topic)
                if envelope["event_type"] == "SubmissionUnknown":
                    raise SimulatedProcessDeath("after Unknown commit")
                return result

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                raise TimeoutError("reply lost after wire")

            with patch.object(JournalStore, "append_event", new=die_after_unknown):
                with self.assertRaisesRegex(
                    SimulatedProcessDeath,
                    "after Unknown commit",
                ):
                    self._dispatch(
                        dispatcher,
                        attempt_id="crash-after-unknown",
                        now="2026-10-06T16:37:00Z",
                        transport=transport,
                    )

            self.assertEqual(outbound, 1)
            self.assertEqual(
                self._event_types(path, dispatcher, "crash-after-unknown"),
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionUnknown",
                ],
            )

            restarted = self._dispatcher(path, owner_token="owner-b")
            recovered = self._dispatch(
                restarted,
                attempt_id="crash-after-unknown",
                now="2026-10-06T16:37:01Z",
                transport=self._forbidden_transport,
            )
            self.assertEqual(recovered.status, "UNKNOWN")
            self.assertEqual(
                recovered.reason,
                "transport_exception_after_send_barrier:TimeoutError",
            )
            self.assertEqual(outbound, 1)
            self.assertEqual(
                self._event_types(path, restarted, "crash-after-unknown"),
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionUnknown",
                ],
            )


    def test_death_after_exact_ambiguous_unknown_commit_replays_reason(self):
        with TemporaryDirectory() as directory:
            path = self._path(directory)
            dispatcher = self._dispatcher(path)
            real_append = JournalStore.append_event
            outbound = 0
            response = ExactJsonTransportResponse(
                b'{"retCode":10000}',
                http_status=200,
                requires_reconciliation=True,
                ambiguity_reason="provider_ack_ambiguous",
            )

            def die_after_unknown(store, envelope, *, outbox_topic=None):
                result = real_append(store, envelope, outbox_topic=outbox_topic)
                if envelope["event_type"] == "SubmissionUnknown":
                    raise SimulatedProcessDeath("after exact Unknown commit")
                return result

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return response

            with patch.object(JournalStore, "append_event", new=die_after_unknown):
                with self.assertRaisesRegex(
                    SimulatedProcessDeath,
                    "after exact Unknown commit",
                ):
                    self._dispatch(
                        dispatcher,
                        attempt_id="crash-after-exact-unknown",
                        now="2026-10-06T16:38:00Z",
                        transport=transport,
                    )

            self.assertEqual(outbound, 1)
            events = JournalStore(path).load_events(
                "submission_attempt",
                dispatcher._aggregate_id("crash-after-exact-unknown"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionUnknown",
                ],
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
                attempt_id="crash-after-exact-unknown",
                now="2026-10-06T16:38:01Z",
                transport=self._forbidden_transport,
            )
            self.assertEqual(recovered.status, "UNKNOWN")
            self.assertEqual(recovered.reason, "provider_ack_ambiguous")
            self.assertEqual(outbound, 1)


if __name__ == "__main__":
    unittest.main()
