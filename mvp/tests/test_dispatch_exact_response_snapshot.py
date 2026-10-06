from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    _snapshot_exact_transport_response,
    load_submission_response_binding,
    submission_response_binding_projection,
)
from mvp.autotrade_mvp.persistence import JournalStore


class ExactResponseSnapshotCurrentTests(unittest.TestCase):
    @staticmethod
    def _allow(_intent_hash, _now):
        return True, "allowed"

    @staticmethod
    def _dispatcher(path: str) -> GuardedDispatcher:
        return GuardedDispatcher(
            JournalStore(path),
            environment="SIMULATION",
            account_id="acct",
            owner_token="owner",
        )

    def test_definitive_snapshot_has_seven_field_utf8_json_contract(self):
        response = ExactJsonTransportResponse(
            b'{"accepted":true}',
            http_status=201,
        )

        snapshot = _snapshot_exact_transport_response(response)

        self.assertEqual(len(snapshot), 7)
        self.assertEqual(snapshot[0], '{"accepted":true}')
        self.assertEqual(snapshot[1], "utf-8-json")
        self.assertEqual(snapshot[2], response.response_sha256)
        self.assertEqual(snapshot[3], {"accepted": True})
        self.assertEqual(snapshot[4], 201)
        self.assertFalse(snapshot[5])
        self.assertIsNone(snapshot[6])

    def test_ambiguous_non_json_snapshot_is_sha_bound_hex_unknown(self):
        raw = b"\xff\x00provider-ack"
        response = ExactJsonTransportResponse(
            raw,
            http_status=502,
            requires_reconciliation=True,
            ambiguity_reason="provider_ack_opaque",
        )

        snapshot = _snapshot_exact_transport_response(response)

        self.assertEqual(len(snapshot), 7)
        self.assertEqual(snapshot[0], raw.hex())
        self.assertEqual(snapshot[1], "hex")
        self.assertEqual(
            snapshot[2],
            "sha256:" + sha256(raw).hexdigest(),
        )
        self.assertIsNone(snapshot[3])
        self.assertEqual(snapshot[4], 502)
        self.assertTrue(snapshot[5])
        self.assertEqual(snapshot[6], "provider_ack_opaque")

    def test_dispatch_definitive_exact_response_reaches_durable_sent(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            outbound = 0

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return ExactJsonTransportResponse(
                    b'{"accepted":true,"orderId":"p-1"}',
                    http_status=200,
                )

            result = dispatcher.dispatch(
                attempt_id="snapshot-sent-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T17:20:00Z",
                authority_check=self._allow,
                transport_send=transport,
                submission_scope={"endpoint": "/orders"},
            )

            self.assertEqual(outbound, 1)
            self.assertEqual(result.status, "SENT")
            self.assertEqual(result.reason, "sent_confirmed")
            self.assertEqual(
                result.response,
                {"accepted": True, "orderId": "p-1"},
            )
            binding = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="snapshot-sent-a1",
            )
            projected = submission_response_binding_projection(binding)
            self.assertEqual(projected["terminal_state"], "SENT")
            self.assertEqual(projected["response_encoding"], "utf-8-json")
            self.assertEqual(projected["http_status"], 200)

    def test_dispatch_opaque_ambiguous_response_replays_exact_bytes_without_resend(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            raw = b"\xff\x00provider-ack"
            outbound = 0

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return ExactJsonTransportResponse(
                    raw,
                    http_status=502,
                    requires_reconciliation=True,
                    ambiguity_reason="provider_ack_opaque",
                )

            first = dispatcher.dispatch(
                attempt_id="snapshot-opaque-a1",
                intent_id="intent-2",
                intent_hash="sha256:" + "2" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T17:21:00Z",
                authority_check=self._allow,
                transport_send=transport,
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(first.reason, "provider_ack_opaque")
            self.assertEqual(outbound, 1)

            binding = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="snapshot-opaque-a1",
            )
            projected = submission_response_binding_projection(binding)
            self.assertEqual(projected["terminal_state"], "UNKNOWN")
            self.assertEqual(projected["response_encoding"], "hex")
            self.assertEqual(projected["response_bytes"], raw)
            self.assertEqual(
                projected["response_sha256"],
                "sha256:" + sha256(raw).hexdigest(),
            )
            self.assertEqual(projected["http_status"], 502)
            self.assertEqual(
                projected["retry_disposition"],
                "RECONCILE_FIRST",
            )
            self.assertEqual(
                projected["ambiguity_reason"],
                "provider_ack_opaque",
            )

            restarted = GuardedDispatcher(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner-b",
            )
            replay = restarted.dispatch(
                attempt_id="snapshot-opaque-a1",
                intent_id="intent-2",
                intent_hash="sha256:" + "2" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T17:21:01Z",
                authority_check=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("terminal replay must not re-authorize")),
                transport_send=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("terminal replay must not resend")),
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(replay.status, "UNKNOWN")
            self.assertEqual(replay.reason, "provider_ack_opaque")
            self.assertEqual(outbound, 1)

    def test_definitive_non_json_response_is_rejected_before_dispatch_use(self):
        with self.assertRaises(ValueError):
            ExactJsonTransportResponse(b"\xff\x00not-json")


    def test_post_send_response_type_rebind_unknown_replays_without_resend(self):
        from mvp.autotrade_mvp import dispatch as dispatch_module

        callbacks = 0

        class HostileMeta(type):
            def __instancecheck__(cls, _instance):
                nonlocal callbacks
                callbacks += 1
                raise AssertionError(
                    "rebound response class __instancecheck__ executed"
                )

        class HostileResponse(metaclass=HostileMeta):
            pass

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            response = ExactJsonTransportResponse(
                b'{"accepted":true}',
                http_status=200,
            )
            original_response_type = dispatch_module.ExactJsonTransportResponse
            outbound = 0

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                dispatch_module.ExactJsonTransportResponse = HostileResponse
                return response

            try:
                first = dispatcher.dispatch(
                    attempt_id="snapshot-type-rebind-a1",
                    intent_id="intent-type-rebind",
                    intent_hash="sha256:" + "3" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T17:22:00Z",
                    authority_check=self._allow,
                    transport_send=transport,
                    submission_scope={"endpoint": "/orders"},
                )
            finally:
                dispatch_module.ExactJsonTransportResponse = original_response_type

            self.assertEqual(callbacks, 0)
            self.assertEqual(outbound, 1)
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(first.reason, "sent_response_persistence_failed")

            events = JournalStore.load_events(
                JournalStore(path),
                "submission_attempt",
                dispatcher._aggregate_id("snapshot-type-rebind-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionUnknown",
                ],
            )
            self.assertEqual(
                events[-1]["payload"]["reason"],
                "sent_response_persistence_failed:ValueError",
            )

            restarted = GuardedDispatcher(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner-b",
            )
            replay = restarted.dispatch(
                attempt_id="snapshot-type-rebind-a1",
                intent_id="intent-type-rebind",
                intent_hash="sha256:" + "3" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T17:22:01Z",
                authority_check=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("terminal replay must not re-authorize")),
                transport_send=lambda *_args: (
                    _ for _ in ()
                ).throw(AssertionError("terminal replay must not resend")),
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(replay.status, "UNKNOWN")
            self.assertEqual(
                replay.reason,
                "sent_response_persistence_failed:ValueError",
            )
            self.assertEqual(callbacks, 0)
            self.assertEqual(outbound, 1)
            self.assertEqual(
                [
                    event["event_type"]
                    for event in JournalStore.load_events(
                        JournalStore(path),
                        "submission_attempt",
                        restarted._aggregate_id("snapshot-type-rebind-a1"),
                    )
                ],
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionUnknown",
                ],
            )


if __name__ == "__main__":
    unittest.main()
