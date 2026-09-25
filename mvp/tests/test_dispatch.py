from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import GuardedDispatcher, stable_client_order_id
from mvp.autotrade_mvp.persistence import JournalStore


class SimulatedProcessDeath(BaseException):
    """Model abrupt process termination that normal error recovery cannot catch."""


class DispatchTests(unittest.TestCase):
    def store(self, directory):
        return JournalStore(f"{directory}/journal.sqlite3")

    def test_dispatch_scope_is_required_and_separates_client_ids(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            with self.assertRaises(TypeError):
                GuardedDispatcher(store)
            with self.assertRaises(ValueError):
                GuardedDispatcher(store, environment="", account_id="acct")
            with self.assertRaises(ValueError):
                GuardedDispatcher(store, environment="PAPER", account_id="")

        paper = stable_client_order_id(
            "provider", "intent-1", environment="PAPER", account_id="acct-1"
        )
        live = stable_client_order_id(
            "provider", "intent-1", environment="LIVE", account_id="acct-1"
        )
        other_account = stable_client_order_id(
            "provider", "intent-1", environment="PAPER", account_id="acct-2"
        )
        self.assertNotEqual(paper, live)
        self.assertNotEqual(paper, other_account)

    def test_delimiters_inside_external_ids_cannot_alias_client_identity(self):
        first = stable_client_order_id(
            "provider",
            "c",
            environment="PAPER",
            account_id="a|b",
        )
        second = stable_client_order_id(
            "provider",
            "b|c",
            environment="PAPER",
            account_id="a",
        )
        self.assertNotEqual(first, second)

    def test_delimiters_inside_external_ids_cannot_alias_aggregate_identity(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            first = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="a:b",
                owner_token="owner-1",
            )
            second = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="a",
                owner_token="owner-2",
            )
            self.assertNotEqual(
                first._aggregate_id("c"),
                second._aggregate_id("b:c"),
            )

    def test_same_attempt_id_can_exist_independently_in_two_scopes(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            sends = []

            def authority(intent_hash, now):
                return True, "allowed"

            def transport(client_id, request, final_guard):
                final_guard()
                sends.append(client_id)
                return {"ok": True}

            paper = GuardedDispatcher(
                store, environment="PAPER", account_id="acct", owner_token="paper-owner"
            )
            live = GuardedDispatcher(
                store, environment="LIVE", account_id="acct", owner_token="live-owner"
            )
            for dispatcher in (paper, live):
                outcome = dispatcher.dispatch(
                    attempt_id="same-attempt",
                    intent_id="same-intent",
                    intent_hash="hash",
                    provider="provider",
                    request={},
                    now="2026-09-24T18:00:00Z",
                    authority_check=authority,
                    transport_send=transport,
                )
                self.assertEqual(outcome.status, "SENT")

            self.assertEqual(len(sends), 2)
            self.assertNotEqual(sends[0], sends[1])
            paper_events = store.load_events(
                "submission_attempt", paper._aggregate_id("same-attempt")
            )
            live_events = store.load_events(
                "submission_attempt", live._aggregate_id("same-attempt")
            )
            self.assertEqual(len(paper_events), 3)
            self.assertEqual(len(live_events), 3)
            self.assertEqual(paper_events[0]["payload"]["environment"], "PAPER")
            self.assertEqual(paper_events[0]["payload"]["account_id"], "acct")
            self.assertEqual(live_events[0]["payload"]["environment"], "LIVE")
            self.assertEqual(live_events[0]["payload"]["account_id"], "acct")
            self.assertNotEqual(paper_events[0]["aggregate_id"], live_events[0]["aggregate_id"])

    def test_success_uses_final_barrier_and_persists_three_states(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(store, environment="SIMULATION", account_id="acct", owner_token="owner")
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
            events = store.load_events("submission_attempt", dispatcher._aggregate_id("a1"))
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )

    def test_revoke_during_provider_wait_blocks_before_outbound_request(self):
        with TemporaryDirectory() as directory:
            dispatcher = GuardedDispatcher(self.store(directory), environment="SIMULATION", account_id="acct", owner_token="owner")
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

    def test_request_mutation_before_final_barrier_is_blocked(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            outbound = 0

            def authority(intent_hash, now):
                return True, "allowed"

            def transport(client_id, request, final_guard):
                nonlocal outbound
                request["order"]["qty"] = "999"
                final_guard()
                outbound += 1
                return {"provider_order_id": "must-not-happen"}

            original = {"order": {"qty": "1"}}
            result = dispatcher.dispatch(
                attempt_id="mutate-a1",
                intent_id="i1",
                intent_hash="h1",
                provider="sim",
                request=original,
                now="2026-09-24T18:00:00Z",
                authority_check=authority,
                transport_send=transport,
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "transport_failed_before_send")
            self.assertEqual(outbound, 0)
            self.assertEqual(original, {"order": {"qty": "1"}})
            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("mutate-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

    def test_request_cannot_be_mutated_after_final_barrier(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            outbound = 0

            def authority(intent_hash, now):
                return True, "allowed"

            def transport(client_id, request, final_guard):
                nonlocal outbound
                final_guard()
                request["order"]["qty"] = "999"
                outbound += 1
                return {"provider_order_id": "must-not-happen"}

            result = dispatcher.dispatch(
                attempt_id="post-guard-mutate",
                intent_id="i1",
                intent_hash="h1",
                provider="sim",
                request={"order": {"qty": "1"}},
                now="2026-09-24T18:00:00Z",
                authority_check=authority,
                transport_send=transport,
            )
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "transport_result_ambiguous")
            self.assertEqual(outbound, 0)
            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("post-guard-mutate"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )

    def test_caller_nested_mutation_cannot_alias_dispatch_payload(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            outbound = []

            def authority(intent_hash, now):
                return True, "allowed"

            original = {"order": {"qty": "1"}}

            def transport(client_id, request, final_guard):
                original["order"]["qty"] = "999"
                final_guard()
                outbound.append(request["order"]["qty"])
                return {"provider_order_id": "p1"}

            result = dispatcher.dispatch(
                attempt_id="alias-a1",
                intent_id="i1",
                intent_hash="h1",
                provider="sim",
                request=original,
                now="2026-09-24T18:00:00Z",
                authority_check=authority,
                transport_send=transport,
            )
            self.assertEqual(result.status, "SENT")
            self.assertEqual(outbound, ["1"])
            self.assertEqual(original["order"]["qty"], "999")

    def test_timeout_after_outbound_becomes_unknown_and_never_blindly_retries(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(store, environment="SIMULATION", account_id="acct", owner_token="owner")
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
            dispatcher = GuardedDispatcher(store, environment="SIMULATION", account_id="acct", owner_token="owner")
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
            recovered = GuardedDispatcher(store, environment="SIMULATION", account_id="acct", owner_token="owner-2").dispatch(
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
            first = GuardedDispatcher(store, environment="SIMULATION", account_id="acct", owner_token="owner-1", prepared_lease_seconds=60)
            second = GuardedDispatcher(store, environment="SIMULATION", account_id="acct", owner_token="owner-2", prepared_lease_seconds=60)
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

    def test_client_order_id_rejects_low_entropy_length(self):
        for invalid in (12, 16, 19, True):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "at least 20"
            ):
                stable_client_order_id(
                    "provider",
                    "intent-1",
                    environment="PAPER",
                    account_id="acct-1",
                    max_length=invalid,
                )

    def test_stable_client_id_is_deterministic_and_bounded(self):
        first = stable_client_order_id("Provider", "intent-1", environment="PAPER", account_id="acct-1", max_length=20)
        second = stable_client_order_id("Provider", "intent-1", environment="PAPER", account_id="acct-1", max_length=20)
        other = stable_client_order_id("Provider", "intent-2", environment="PAPER", account_id="acct-1", max_length=20)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertLessEqual(len(first), 20)


    def test_final_barrier_uses_fresh_time_and_blocks_expired_authority(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(store, environment="SIMULATION", account_id="acct", owner_token="owner")
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
            events = store.load_events("submission_attempt", dispatcher._aggregate_id("expiry-a1"))
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertEqual(
                events[-1]["committed_at"],
                "2026-09-24T18:02:00Z",
            )



    def test_final_barrier_clock_exception_is_persistently_blocked(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            outbound = 0

            def authority(intent_hash, current_time):
                return True, "allowed"

            def transport(client_id, request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"provider_order_id": "must-not-happen"}

            def broken_clock():
                raise RuntimeError("clock unavailable")

            result = dispatcher.dispatch(
                attempt_id="clock-error-a1",
                intent_id="i1",
                intent_hash="h1",
                provider="sim",
                request={},
                now="2026-09-24T18:00:00Z",
                authority_check=authority,
                transport_send=transport,
                final_barrier_clock=broken_clock,
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "final_barrier_clock_failed:RuntimeError")
            self.assertEqual(outbound, 0)
            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("clock-error-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertEqual(
                events[-1]["payload"]["reason"],
                "final_barrier_clock_failed:RuntimeError",
            )

    def test_invalid_final_barrier_timestamp_is_persistently_blocked(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            outbound = 0

            def authority(intent_hash, current_time):
                return True, "allowed"

            def transport(client_id, request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"provider_order_id": "must-not-happen"}

            result = dispatcher.dispatch(
                attempt_id="clock-invalid-a1",
                intent_id="i1",
                intent_hash="h1",
                provider="sim",
                request={},
                now="2026-09-24T18:00:00Z",
                authority_check=authority,
                transport_send=transport,
                final_barrier_clock=lambda: "not-a-timestamp",
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "final_barrier_clock_failed:ValueError")
            self.assertEqual(outbound, 0)
            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("clock-invalid-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

    def test_backward_final_clock_is_persistently_blocked(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(store, environment="SIMULATION", account_id="acct", owner_token="owner")
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
            events = store.load_events("submission_attempt", dispatcher._aggregate_id("clock-a1"))
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

    def test_unserializable_provider_response_after_send_becomes_unknown(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(store, environment="SIMULATION", account_id="acct", owner_token="owner")
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
            events = store.load_events("submission_attempt", dispatcher._aggregate_id("response-a1"))
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )



    def test_initial_authority_exception_is_blocked_without_send(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            outbound = 0

            def authority(intent_hash, current_time):
                raise RuntimeError("authority unavailable")

            def transport(client_id, request, final_guard):
                nonlocal outbound
                outbound += 1
                raise AssertionError("transport must not run")

            result = dispatcher.dispatch(
                attempt_id="authority-error-initial",
                intent_id="i1",
                intent_hash="h1",
                provider="sim",
                request={},
                now="2026-09-24T18:00:00Z",
                authority_check=authority,
                transport_send=transport,
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "authority_check_failed_before_send")
            self.assertEqual(outbound, 0)
            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("authority-error-initial"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )
            self.assertEqual(
                events[-1]["payload"]["reason"],
                "authority_check_failed_before_send:RuntimeError",
            )

    def test_final_authority_exception_is_blocked_without_outbound_request(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            calls = 0
            outbound = 0

            def authority(intent_hash, current_time):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return True, "allowed"
                raise RuntimeError("authority unavailable at barrier")

            def transport(client_id, request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"provider_order_id": "must-not-happen"}

            result = dispatcher.dispatch(
                attempt_id="authority-error-final",
                intent_id="i1",
                intent_hash="h1",
                provider="sim",
                request={},
                now="2026-09-24T18:00:00Z",
                authority_check=authority,
                transport_send=transport,
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(
                result.reason,
                "authority_check_failed_at_final_barrier:RuntimeError",
            )
            self.assertEqual(outbound, 0)
            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("authority-error-final"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )


if __name__ == "__main__":
    unittest.main()
