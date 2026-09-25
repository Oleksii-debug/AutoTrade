from tempfile import TemporaryDirectory
import unittest
from uuid import UUID

from mvp.autotrade_mvp.dispatch import (
    DispatchBlocked,
    ExactJsonTransportResponse,
    GuardedDispatcher,
    SubmissionResponseBinding,
    load_submission_response_binding,
    stable_client_order_id,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import RecoveryController
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.tests.test_reconciliation_journal import reconciliation


class SimulatedProcessDeath(BaseException):
    """Model abrupt process termination that normal error recovery cannot catch."""


class DispatchTests(unittest.TestCase):
    def _record_ready(self, recovery, store, *, account_id="acct"):
        owner = recovery.owner
        if owner is None:
            raise AssertionError("recovery owner must exist")
        result = reconciliation(
            account_id=account_id,
            provider_activity_account_id=account_id,
        )
        record_reconciliation_checkpoint(
            store,
            reconciliation_id="dispatch-ready",
            result=result,
            observed_at="2026-09-24T19:00:00Z",
            host_id=owner.owner_id,
            owner_epoch=str(owner.epoch),
        )
        recovery.record_reconciliation_checkpoint(
            reconciliation_id="dispatch-ready",
            provider_id=result.provider_id,
            account_id=result.account_id,
            environment=result.environment,
        )

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

    def test_uuid_client_order_id_is_deterministic_scope_bound_and_not_truncated(self):
        first = stable_client_order_id(
            "KRAKEN",
            "intent-uuid",
            environment="LIVE",
            account_id="spot-account",
            max_length=36,
            client_id_format="UUID",
        )
        repeated = stable_client_order_id(
            "KRAKEN",
            "intent-uuid",
            environment="LIVE",
            account_id="spot-account",
            max_length=36,
            client_id_format="uuid",
        )
        other_scope = stable_client_order_id(
            "KRAKEN",
            "intent-uuid",
            environment="LIVE",
            account_id="other-account",
            max_length=36,
            client_id_format="UUID",
        )
        self.assertEqual(str(UUID(first)), first)
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, other_scope)
        with self.assertRaisesRegex(ValueError, "at least 36"):
            stable_client_order_id(
                "KRAKEN",
                "intent-uuid",
                environment="LIVE",
                account_id="spot-account",
                max_length=32,
                client_id_format="UUID",
            )
        with self.assertRaisesRegex(ValueError, "TOKEN or UUID"):
            stable_client_order_id(
                "KRAKEN",
                "intent-uuid",
                environment="LIVE",
                account_id="spot-account",
                client_id_format="provider-magic",
            )

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
                    sender_check=lambda _owner, _epoch: None,
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
            self.assertNotEqual(
                paper_events[0]["aggregate_id"],
                live_events[0]["aggregate_id"],
            )

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

    def test_exact_provider_response_bytes_are_durable_and_restart_stable(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            raw = b'{ "provider_order_id" : "p-1", "ok" : true }'

            result = dispatcher.dispatch(
                attempt_id="exact-response-a1",
                intent_id="i1",
                intent_hash="h1",
                provider="provider",
                request={"side": "BUY"},
                now="2026-09-24T18:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=lambda _cid, _request, guard: (
                    guard(),
                    ExactJsonTransportResponse(raw),
                )[1],
                submission_scope={
                    "endpoint": "/orders",
                    "capability_snapshot_ids": ["cap-1"],
                    "instrument_versions": ["BTCUSD:v1"],
                },
            )
            self.assertEqual(result.status, "SENT")
            self.assertEqual(result.response["provider_order_id"], "p-1")

            binding = load_submission_response_binding(
                store,
                environment="SIMULATION",
                account_id="acct",
                attempt_id="exact-response-a1",
            )
            self.assertIsInstance(binding, SubmissionResponseBinding)
            self.assertEqual(binding.response_bytes, raw)
            self.assertEqual(
                binding.response_sha256,
                "sha256:" + __import__("hashlib").sha256(raw).hexdigest(),
            )
            self.assertEqual(binding.payload["provider_order_id"], "p-1")
            self.assertEqual(binding.submission_scope["endpoint"], "/orders")

            reopened = JournalStore(f"{directory}/journal.sqlite3")
            after_restart = load_submission_response_binding(
                reopened,
                environment="SIMULATION",
                account_id="acct",
                attempt_id="exact-response-a1",
            )
            self.assertEqual(after_restart.response_bytes, binding.response_bytes)
            self.assertEqual(after_restart.response_sha256, binding.response_sha256)
            self.assertEqual(
                after_restart.submission_scope_hash,
                binding.submission_scope_hash,
            )

    def test_mapping_response_cannot_mint_exact_durable_response_provenance(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            result = dispatcher.dispatch(
                attempt_id="legacy-response-a1",
                intent_id="i1",
                intent_hash="h1",
                provider="provider",
                request={},
                now="2026-09-24T18:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=lambda _cid, _request, guard: (
                    guard(),
                    {"provider_order_id": "p-1"},
                )[1],
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(result.status, "SENT")
            with self.assertRaisesRegex(
                ValueError,
                "exact provider response bytes are unavailable",
            ):
                load_submission_response_binding(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    attempt_id="legacy-response-a1",
                )

    def test_exact_response_identity_preserves_wire_whitespace(self):
        first = ExactJsonTransportResponse(b'{"ok":true}')
        second = ExactJsonTransportResponse(b'{ "ok" : true }')
        self.assertEqual(first.payload, second.payload)
        self.assertNotEqual(first.response_sha256, second.response_sha256)

    def test_exact_transport_response_rejects_duplicate_keys_and_non_json(self):
        for raw in (
            b'{"ok":true,"ok":false}',
            b'{"value":NaN}',
            b'not-json',
            b"",
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                ExactJsonTransportResponse(raw)

    def test_submission_scope_is_part_of_attempt_idempotency_contract(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )

            def transport(_cid, _request, guard):
                guard()
                return ExactJsonTransportResponse(b'{"ok":true}')

            common = dict(
                attempt_id="scope-a1",
                intent_id="i1",
                intent_hash="h1",
                provider="provider",
                request={},
                now="2026-09-24T18:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
            )
            first = dispatcher.dispatch(
                **common,
                submission_scope={"capability_snapshot_ids": ["cap-1"]},
            )
            self.assertEqual(first.status, "SENT")
            with self.assertRaisesRegex(ValueError, "conflicts"):
                dispatcher.dispatch(
                    **common,
                    submission_scope={"capability_snapshot_ids": ["cap-2"]},
                )

    def test_exact_binding_cannot_be_forged_or_relabelled_to_other_scope(self):
        with self.assertRaisesRegex(ValueError, "loaded from the durable journal"):
            SubmissionResponseBinding(
                attempt_id="a",
                aggregate_id="agg",
                provider="provider",
                request_hash="sha256:" + "1" * 64,
                client_order_id="client",
                environment="SIMULATION",
                account_id="acct",
                prepared_at="2026-09-24T18:00:00Z",
                sent_at="2026-09-24T18:00:01Z",
                submission_scope={},
                submission_scope_hash="sha256:" + "2" * 64,
                response_bytes=b'{"ok":true}',
                response_sha256="sha256:" + "3" * 64,
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


    def test_strict_authority_contract_blocks_truthy_string_before_transport(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            outbound = 0

            def transport(*_args):
                nonlocal outbound
                outbound += 1
                return {"provider_order_id": "must-not-happen"}

            result = dispatcher.dispatch(
                attempt_id="bad-authority-a1",
                intent_id="i1",
                intent_hash="h1",
                provider="sim",
                request={},
                now="2026-09-24T18:00:00Z",
                authority_check=lambda _hash, _now: ("false", "malformed"),
                transport_send=transport,
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "authority_check_invalid_allowed")
            self.assertEqual(outbound, 0)

    def test_malformed_final_authority_result_blocks_before_outbound(self):
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

            def authority(_hash, _now):
                nonlocal calls
                calls += 1
                return (True, "allowed") if calls == 1 else (True, "")

            def transport(_client_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"provider_order_id": "must-not-happen"}

            result = dispatcher.dispatch(
                attempt_id="bad-final-authority-a1",
                intent_id="i1",
                intent_hash="h1",
                provider="sim",
                request={},
                now="2026-09-24T18:00:00Z",
                authority_check=authority,
                transport_send=transport,
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "authority_check_invalid_reason")
            self.assertEqual(outbound, 0)

    def test_paper_and_live_require_sender_fence_before_outbound(self):
        for environment in ("PAPER", "LIVE"):
            with self.subTest(environment=environment), TemporaryDirectory() as directory:
                store = self.store(directory)
                dispatcher = GuardedDispatcher(
                    store,
                    environment=environment,
                    account_id="acct",
                    owner_token="owner",
                    owner_epoch=1,
                )
                outbound = 0

                def transport(_client_id, _request, final_guard):
                    nonlocal outbound
                    final_guard()
                    outbound += 1
                    return {"provider_order_id": "must-not-happen"}

                result = dispatcher.dispatch(
                    attempt_id="fence-required",
                    intent_id="i1",
                    intent_hash="h1",
                    provider="sim",
                    request={},
                    now="2026-09-24T18:00:00Z",
                    authority_check=lambda _hash, _now: (True, "allowed"),
                    transport_send=transport,
                )
                self.assertEqual(result.status, "BLOCKED")
                self.assertEqual(result.reason, "sender_fence_required")
                self.assertEqual(outbound, 0)
                events = store.load_events(
                    "submission_attempt",
                    dispatcher._aggregate_id("fence-required"),
                )
                self.assertEqual(
                    [event["event_type"] for event in events],
                    ["SubmissionPrepared", "SubmissionBlocked"],
                )

    def test_owner_transfer_during_provider_wait_blocks_stale_sender(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            owner = recovery.start("host-a")
            self._record_ready(recovery, store, account_id="acct")
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token=owner.owner_id,
                owner_epoch=owner.epoch,
            )
            outbound = 0

            def transport(_client_id, _request, final_guard):
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
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
                sender_check=recovery.validate_sender,
            )
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason, "sender_fence_rejected:PermissionError")
            self.assertEqual(outbound, 0)

    def test_paper_send_succeeds_only_with_current_durable_sender(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            owner = recovery.start("host-a")
            self._record_ready(recovery, store, account_id="acct")
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token=owner.owner_id,
                owner_epoch=owner.epoch,
            )
            outbound = 0

            def transport(_client_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"provider_order_id": "p-1"}

            result = dispatcher.dispatch(
                attempt_id="paper-current-owner",
                intent_id="i1",
                intent_hash="h1",
                provider="sim",
                request={},
                now="2026-09-24T18:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=transport,
                sender_check=recovery.validate_sender,
            )
            self.assertEqual(result.status, "SENT")
            self.assertEqual(outbound, 1)
            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("paper-current-owner"),
            )
            self.assertEqual(
                [event["owner_epoch"] for event in events],
                [str(owner.epoch), str(owner.epoch), str(owner.epoch)],
            )
            self.assertEqual(events[0]["payload"]["owner_epoch"], owner.epoch)

    def test_owner_epoch_must_be_positive_integer(self):
        with TemporaryDirectory() as directory:
            for invalid in (0, -1, True):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(
                    ValueError, "positive integer"
                ):
                    GuardedDispatcher(
                        self.store(directory),
                        environment="SIMULATION",
                        account_id="acct",
                        owner_token="host-a",
                        owner_epoch=invalid,
                    )


    def test_masked_final_guard_block_exception_becomes_unknown(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            calls = 0
            outbound_after_block = 0

            def authority(intent_hash, current_time):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return True, "allowed"
                return False, "revoked_at_final_barrier"

            def broken_transport(client_id, request, final_guard):
                nonlocal outbound_after_block
                try:
                    final_guard()
                except DispatchBlocked:
                    # The wrapper violates the barrier, may perform an outbound
                    # side effect, and then masks the original rejection with a
                    # different transport error.
                    outbound_after_block += 1
                    raise OSError("provider failed after ignored guard")
                raise AssertionError("final guard should have blocked")

            result = dispatcher.dispatch(
                attempt_id="masked-guard-a1",
                intent_id="i1",
                intent_hash="h1",
                provider="sim",
                request={},
                now="2026-09-24T18:00:00Z",
                authority_check=authority,
                transport_send=broken_transport,
            )

            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "provider_guard_contract_violation")
            self.assertEqual(outbound_after_block, 1)
            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("masked-guard-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "SubmissionPrepared",
                    "SubmissionBlocked",
                    "SubmissionUnknown",
                ],
            )
            self.assertEqual(
                events[-1]["payload"]["reason"],
                "provider_wrapper_masked_final_guard_failure:OSError",
            )

    def test_swallowed_final_guard_block_becomes_unknown(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            calls = 0
            outbound_after_block = 0

            def authority(intent_hash, current_time):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return True, "allowed"
                return False, "revoked_at_final_barrier"

            def broken_transport(client_id, request, final_guard):
                nonlocal outbound_after_block
                try:
                    final_guard()
                except DispatchBlocked:
                    # Simulate a provider wrapper bug: it ignores the barrier
                    # and proceeds as if a send could still have happened.
                    outbound_after_block += 1
                    return {"provider_order_id": "unsafe-wrapper-result"}
                raise AssertionError("final guard should have blocked")

            result = dispatcher.dispatch(
                attempt_id="swallowed-guard-a1",
                intent_id="i1",
                intent_hash="h1",
                provider="sim",
                request={},
                now="2026-09-24T18:00:00Z",
                authority_check=authority,
                transport_send=broken_transport,
            )

            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "provider_guard_contract_violation")
            self.assertEqual(outbound_after_block, 1)
            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("swallowed-guard-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "SubmissionPrepared",
                    "SubmissionBlocked",
                    "SubmissionUnknown",
                ],
            )
            self.assertEqual(
                events[-1]["payload"]["reason"],
                "provider_wrapper_swallowed_final_guard_failure",
            )


if __name__ == "__main__":
    unittest.main()
