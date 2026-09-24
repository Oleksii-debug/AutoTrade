from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import GuardedDispatcher, stable_client_order_id
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import RecoveryController


class SimulatedProcessDeath(BaseException):
    """Model abrupt process termination that normal error recovery cannot catch."""


class DispatchTests(unittest.TestCase):
    def store(self, directory):
        return JournalStore(f"{directory}/journal.sqlite3")

    def test_success_uses_final_barrier_and_persists_three_states(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(store, owner_token="owner")
            checks = []
            sends = []

            def authority(intent_hash, now):
                checks.append((intent_hash, now))
                return True, "allowed"

            def transport(client_id, request, final_guard):
                final_guard()
                sends.append((client_id, request))
                return {"provider_order_id": "p-1"}

            result = dispatcher.dispatch(
                attempt_id="a1", intent_id="i1", intent_hash="h1",
                provider="sim", request={"side": "BUY"}, now="2026-09-24T18:00:00Z",
                authority_check=authority, transport_send=transport,
            )
            self.assertEqual(result.status, "SENT")
            self.assertEqual(len(checks), 2)
            self.assertEqual(len(sends), 1)
            events = store.load_events("submission_attempt", "a1")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )

    def test_revoke_during_provider_wait_blocks_before_outbound_request(self):
        with TemporaryDirectory() as directory:
            dispatcher = GuardedDispatcher(self.store(directory), owner_token="owner")
            calls = 0
            outbound = 0

            def authority(intent_hash, now):
                nonlocal calls
                calls += 1
                return (True, "allowed") if calls == 1 else (False, "policy_revoked")

            def transport(client_id, request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"provider_order_id": "must-not-happen"}

            result = dispatcher.dispatch(
                attempt_id="a1", intent_id="i1", intent_hash="h1",
                provider="sim", request={}, now="2026-09-24T18:00:00Z",
                authority_check=authority, transport_send=transport,
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "policy_revoked")
            self.assertEqual(outbound, 0)

    def test_timeout_after_outbound_becomes_unknown_and_never_blindly_retries(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(store, owner_token="owner")
            outbound = 0

            def authority(intent_hash, now):
                return True, "allowed"

            def timeout_transport(client_id, request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                raise TimeoutError("provider reply lost")

            first = dispatcher.dispatch(
                attempt_id="a1", intent_id="i1", intent_hash="h1",
                provider="sim", request={}, now="2026-09-24T18:00:00Z",
                authority_check=authority, transport_send=timeout_transport,
            )
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(outbound, 1)

            def forbidden_retry(*args):
                raise AssertionError("ambiguous send must not be retried")

            second = dispatcher.dispatch(
                attempt_id="a1", intent_id="i1", intent_hash="h1",
                provider="sim", request={}, now="2026-09-24T18:01:00Z",
                authority_check=authority, transport_send=forbidden_retry,
            )
            self.assertEqual(second.status, "UNKNOWN")
            self.assertEqual(outbound, 1)

    def test_crash_after_send_before_terminal_record_recovers_unknown_without_resend(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(store, owner_token="owner")
            real_append = store.append_event
            outbound = 0

            def crashing_append(envelope, *, outbox_topic=None):
                if envelope["event_type"] == "SubmissionSent":
                    raise SimulatedProcessDeath("simulated process death before terminal journal")
                return real_append(envelope, outbox_topic=outbox_topic)

            store.append_event = crashing_append

            def authority(intent_hash, now):
                return True, "allowed"

            def transport(client_id, request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"provider_order_id": "p1"}

            with self.assertRaisesRegex(SimulatedProcessDeath, "simulated process death"):
                dispatcher.dispatch(
                    attempt_id="a1", intent_id="i1", intent_hash="h1",
                    provider="sim", request={}, now="2026-09-24T18:00:00Z",
                    authority_check=authority, transport_send=transport,
                )
            self.assertEqual(outbound, 1)

            store.append_event = real_append
            recovered = GuardedDispatcher(store, owner_token="owner-2").dispatch(
                attempt_id="a1", intent_id="i1", intent_hash="h1",
                provider="sim", request={}, now="2026-09-24T18:00:01Z",
                authority_check=authority,
                transport_send=lambda *args: (_ for _ in ()).throw(AssertionError("must not resend")),
            )
            self.assertEqual(recovered.status, "UNKNOWN")
            self.assertEqual(outbound, 1)

    def test_concurrent_retry_during_prepared_lease_does_not_send(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            first = GuardedDispatcher(store, owner_token="owner-1", prepared_lease_seconds=60)
            second = GuardedDispatcher(store, owner_token="owner-2", prepared_lease_seconds=60)
            nested = []
            outbound = 0

            def authority(intent_hash, now):
                return True, "allowed"

            def transport(client_id, request, final_guard):
                nonlocal outbound
                nested.append(second.dispatch(
                    attempt_id="a1", intent_id="i1", intent_hash="h1",
                    provider="sim", request={}, now="2026-09-24T18:00:01Z",
                    authority_check=authority,
                    transport_send=lambda *args: (_ for _ in ()).throw(AssertionError("nested must not send")),
                ))
                final_guard()
                outbound += 1
                return {"provider_order_id": "p1"}

            result = first.dispatch(
                attempt_id="a1", intent_id="i1", intent_hash="h1",
                provider="sim", request={}, now="2026-09-24T18:00:00Z",
                authority_check=authority, transport_send=transport,
            )
            self.assertEqual(result.status, "SENT")
            self.assertEqual(nested[0].status, "IN_PROGRESS")
            self.assertEqual(outbound, 1)

    def test_stable_client_id_is_deterministic_and_bounded(self):
        first = stable_client_order_id("Provider", "intent-1", max_length=20)
        second = stable_client_order_id("Provider", "intent-1", max_length=20)
        other = stable_client_order_id("Provider", "intent-2", max_length=20)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertLessEqual(len(first), 20)


    def test_final_barrier_uses_fresh_time_and_blocks_expired_authority(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(store, owner_token="owner")
            outbound = 0
            observed_times = []

            def authority(intent_hash, current_time):
                observed_times.append(current_time)
                return (
                    (True, "allowed")
                    if current_time < "2026-09-24T18:01:00Z"
                    else (False, "policy_expired")
                )

            def transport(client_id, request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"provider_order_id": "must-not-happen"}

            result = dispatcher.dispatch(
                attempt_id="expiry-a1",
                intent_id="i1",
                intent_hash="h1",
                provider="sim",
                request={},
                now="2026-09-24T18:00:00Z",
                authority_check=authority,
                transport_send=transport,
                final_barrier_clock=lambda: "2026-09-24T18:02:00Z",
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "policy_expired")
            self.assertEqual(outbound, 0)
            self.assertEqual(
                observed_times,
                ["2026-09-24T18:00:00Z", "2026-09-24T18:02:00Z"],
            )
            events = store.load_events("submission_attempt", "expiry-a1")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertEqual(
                events[-1]["committed_at"],
                "2026-09-24T18:02:00Z",
            )



    def test_backward_final_clock_is_persistently_blocked(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(store, owner_token="owner")
            outbound = 0

            def authority(intent_hash, current_time):
                return True, "allowed"

            def transport(client_id, request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"provider_order_id": "must-not-happen"}

            result = dispatcher.dispatch(
                attempt_id="clock-a1",
                intent_id="i1",
                intent_hash="h1",
                provider="sim",
                request={},
                now="2026-09-24T18:00:00Z",
                authority_check=authority,
                transport_send=transport,
                final_barrier_clock=lambda: "2026-09-24T17:59:59Z",
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "final_barrier_clock_moved_backwards")
            self.assertEqual(outbound, 0)
            events = store.load_events("submission_attempt", "clock-a1")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

    def test_unserializable_provider_response_after_send_becomes_unknown(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(store, owner_token="owner")
            outbound = 0

            def authority(intent_hash, current_time):
                return True, "allowed"

            def transport(client_id, request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"not_json": object()}

            result = dispatcher.dispatch(
                attempt_id="response-a1",
                intent_id="i1",
                intent_hash="h1",
                provider="sim",
                request={},
                now="2026-09-24T18:00:00Z",
                authority_check=authority,
                transport_send=transport,
            )
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(outbound, 1)
            events = store.load_events("submission_attempt", "response-a1")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )



    def test_owner_transfer_during_provider_wait_blocks_stale_sender(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            recovery = RecoveryController()
            owner = recovery.start("host-a")
            recovery.record_reconciliation(consistent=True)
            dispatcher = GuardedDispatcher(
                store,
                owner_token=owner.owner_id,
                owner_epoch=owner.epoch,
            )
            outbound = 0

            def authority(intent_hash, current_time):
                return True, "allowed"

            def transport(client_id, request, final_guard):
                nonlocal outbound
                recovery.transfer_owner(
                    new_owner_id="host-b",
                    old_sender_fenced=True,
                    reconciled=True,
                )
                final_guard()
                outbound += 1
                return {"provider_order_id": "must-not-happen"}

            result = dispatcher.dispatch(
                attempt_id="fenced-a1",
                intent_id="i1",
                intent_hash="h1",
                provider="sim",
                request={},
                now="2026-09-24T18:00:00Z",
                authority_check=authority,
                transport_send=transport,
                sender_check=recovery.validate_sender,
            )

            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "sender_fence_rejected:PermissionError")
            self.assertEqual(outbound, 0)
            events = store.load_events("submission_attempt", "fenced-a1")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertEqual(events[-1]["payload"]["owner_epoch"], 1)

    def test_event_envelope_records_actual_owner_epoch(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                owner_token="host-generation-seven",
                owner_epoch=7,
            )

            result = dispatcher.dispatch(
                attempt_id="epoch-seven-a1",
                intent_id="i-epoch-seven",
                intent_hash="h-epoch-seven",
                provider="sim",
                request={},
                now="2026-09-24T18:00:00Z",
                authority_check=lambda _hash, _now: (False, "blocked-for-test"),
                transport_send=lambda *_args: self.fail("transport must not run"),
            )

            self.assertEqual(result.status, "BLOCKED")
            events = store.load_events("submission_attempt", "epoch-seven-a1")
            self.assertEqual([event["owner_epoch"] for event in events], ["7", "7"])
            self.assertEqual(events[0]["payload"]["owner_epoch"], 7)

    def test_owner_epoch_must_be_positive_integer(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                GuardedDispatcher(
                    self.store(directory),
                    owner_token="host-a",
                    owner_epoch=0,
                )


if __name__ == "__main__":
    unittest.main()
