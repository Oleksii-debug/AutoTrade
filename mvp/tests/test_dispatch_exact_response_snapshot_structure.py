from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import dispatch as dispatch_module
from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
)
from mvp.autotrade_mvp.persistence import JournalStore


class ExactResponseSnapshotStructureTests(unittest.TestCase):
    @staticmethod
    def _dispatcher(path: str) -> GuardedDispatcher:
        return GuardedDispatcher(
            JournalStore(path),
            environment="SIMULATION",
            account_id="acct",
            owner_token="owner",
        )

    @staticmethod
    def _events(path: str, dispatcher: GuardedDispatcher, attempt_id: str):
        return JournalStore.load_events(
            JournalStore(path),
            "submission_attempt",
            dispatcher._aggregate_id(attempt_id),
        )

    def _dispatch(self, dispatcher, attempt_id, transport):
        return dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id="intent-1",
            intent_hash="sha256:" + "1" * 64,
            provider="provider",
            request={"side": "BUY", "quantity": "1"},
            now="2026-10-06T17:30:00Z",
            authority_check=lambda *_args: (True, "allowed"),
            transport_send=transport,
            submission_scope={"endpoint": "/orders"},
        )

    def test_transport_cannot_retarget_snapshot_defaults_after_send(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            snapshot = dispatch_module._snapshot_exact_transport_response
            original_defaults = snapshot.__defaults__

            def forged_authority(_response):
                return (b'{"forged":true}', 200, False, None)

            def forged_decoder(_raw):
                return {"forged": True}

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                snapshot.__defaults__ = (
                    forged_authority,
                    forged_decoder,
                    sha256,
                )
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "snapshot-defaults-retarget",
                    transport,
                )
                self.assertIs(snapshot.__defaults__, original_defaults)
            finally:
                snapshot.__defaults__ = original_defaults

            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "sent_response_persistence_failed")
            events = self._events(
                path,
                dispatcher,
                "snapshot-defaults-retarget",
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
            self.assertNotIn("response_text", events[-1]["payload"])

    def test_transport_cannot_replace_snapshot_code_after_send(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            snapshot = dispatch_module._snapshot_exact_transport_response
            original_code = snapshot.__code__

            def forged_snapshot(
                _response,
                _authority=None,
                _decoder=None,
                _digest=None,
            ):
                raw = b'{"forged":true}'
                return (
                    raw.decode("utf-8"),
                    "utf-8-json",
                    "sha256:" + sha256(raw).hexdigest(),
                    {"forged": True},
                    200,
                    False,
                    None,
                )

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                snapshot.__code__ = forged_snapshot.__code__
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "snapshot-code-retarget",
                    transport,
                )
                self.assertIs(snapshot.__code__, original_code)
            finally:
                snapshot.__code__ = original_code

            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "sent_response_persistence_failed")
            events = self._events(
                path,
                dispatcher,
                "snapshot-code-retarget",
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
            self.assertNotIn("response_text", events[-1]["payload"])


    def test_transport_cannot_rebind_decoder_json_after_send(self):
        class ForgedJsonAuthority:
            JSONDecodeError = ValueError

            @staticmethod
            def loads(_text, **_kwargs):
                return {"forged": True}

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            original_json = dispatch_module.json

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                dispatch_module.json = ForgedJsonAuthority
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "snapshot-decoder-json-retarget",
                    transport,
                )
                self.assertIs(dispatch_module.json, original_json)
            finally:
                dispatch_module.json = original_json

            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "sent_response_persistence_failed")
            events = self._events(
                path,
                dispatcher,
                "snapshot-decoder-json-retarget",
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
            self.assertNotIn("response_text", events[-1]["payload"])


    def test_transport_cannot_replace_json_loads_in_place_after_send(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            json_module = dispatch_module.json
            original_loads = json_module.loads

            forged_calls = 0

            def forged_loads(_text, **_kwargs):
                nonlocal forged_calls
                forged_calls += 1
                return {"forged": True}

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                json_module.loads = forged_loads
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "snapshot-json-loads-retarget",
                    transport,
                )
                self.assertIs(json_module.loads, original_loads)
                self.assertEqual(forged_calls, 0)
            finally:
                json_module.loads = original_loads

            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "sent_response_persistence_failed")
            events = self._events(
                path,
                dispatcher,
                "snapshot-json-loads-retarget",
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
            self.assertNotIn("response_text", events[-1]["payload"])


    def test_transport_cannot_replace_json_loads_code_after_send(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            json_module = dispatch_module.json
            loads = json_module.loads
            original_code = loads.__code__
            def forged_loads(_text, **_kwargs):
                raise AssertionError("forged json.loads executed")

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                loads.__code__ = forged_loads.__code__
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "snapshot-json-loads-code-retarget",
                    transport,
                )
                self.assertIs(loads.__code__, original_code)
            finally:
                loads.__code__ = original_code

            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "sent_response_persistence_failed")
            events = self._events(
                path,
                dispatcher,
                "snapshot-json-loads-code-retarget",
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
            self.assertNotIn("response_text", events[-1]["payload"])

    def test_transport_cannot_mutate_json_loads_kwdefaults_after_send(self):
        class ForgedDecoder:
            def __init__(self, **_kwargs):
                pass

            def decode(self, _text):
                return {"forged": True}

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            json_loads = dispatch_module.json.loads
            original_kwdefaults = json_loads.__kwdefaults__
            baseline_kwdefaults = dict(original_kwdefaults)

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                json_loads.__kwdefaults__["cls"] = ForgedDecoder
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "snapshot-json-loads-kwdefaults-retarget",
                    transport,
                )
                self.assertEqual(
                    json_loads.__kwdefaults__,
                    baseline_kwdefaults,
                )
            finally:
                json_loads.__kwdefaults__ = original_kwdefaults
                original_kwdefaults.clear()
                original_kwdefaults.update(baseline_kwdefaults)

            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "sent_response_persistence_failed")
            events = self._events(
                path,
                dispatcher,
                "snapshot-json-loads-kwdefaults-retarget",
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
            self.assertNotIn("response_text", events[-1]["payload"])

    def test_transport_cannot_rebind_json_decoder_after_send(self):
        class ForgedDecoder:
            def __init__(self, **_kwargs):
                pass

            def decode(self, _text):
                return {"forged": True}

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            json_module = dispatch_module.json
            original_decoder = json_module.JSONDecoder

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                json_module.JSONDecoder = ForgedDecoder
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "snapshot-json-decoder-retarget",
                    transport,
                )
                self.assertIs(json_module.JSONDecoder, original_decoder)
            finally:
                json_module.JSONDecoder = original_decoder

            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "sent_response_persistence_failed")
            events = self._events(
                path,
                dispatcher,
                "snapshot-json-decoder-retarget",
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
            self.assertNotIn("response_text", events[-1]["payload"])

    def test_transport_cannot_replace_json_decoder_decode_code_after_send(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            decoder = dispatch_module.json.JSONDecoder
            decode = decoder.decode
            original_code = decode.__code__

            def forged_decode(self, _text, *_args, **_kwargs):
                return {"forged": True}

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                decode.__code__ = forged_decode.__code__
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "snapshot-json-decoder-code-retarget",
                    transport,
                )
                self.assertIs(decode.__code__, original_code)
                self.assertIs(decoder.decode, decode)
            finally:
                decode.__code__ = original_code
                decoder.decode = decode

            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "sent_response_persistence_failed")
            events = self._events(
                path,
                dispatcher,
                "snapshot-json-decoder-code-retarget",
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
            self.assertNotIn("response_text", events[-1]["payload"])

    def test_transport_cannot_replace_depth_guard_code_after_send(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            depth_guard = dispatch_module.require_provider_json_depth
            original_code = depth_guard.__code__
            over_depth = (
                ("[" * 70) + "0" + ("]" * 70)
            ).encode("utf-8")

            def forged_depth_guard(_raw):
                return None

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                depth_guard.__code__ = forged_depth_guard.__code__
                return ExactJsonTransportResponse(
                    over_depth,
                    http_status=200,
                )

            try:
                result = self._dispatch(
                    dispatcher,
                    "snapshot-depth-guard-retarget",
                    transport,
                )
                self.assertIs(depth_guard.__code__, original_code)
            finally:
                depth_guard.__code__ = original_code

            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "sent_response_persistence_failed")
            events = self._events(
                path,
                dispatcher,
                "snapshot-depth-guard-retarget",
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
            self.assertNotIn("response_text", events[-1]["payload"])


    def test_unchanged_snapshot_authority_preserves_definitive_sent(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            outbound = 0

            def transport(_client_order_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return ExactJsonTransportResponse(
                    b'{"accepted":true,"orderId":"stable-1"}',
                    http_status=200,
                )

            result = self._dispatch(
                dispatcher,
                "snapshot-authority-positive-sent",
                transport,
            )

            self.assertEqual(outbound, 1)
            self.assertEqual(result.status, "SENT")
            self.assertEqual(result.reason, "sent_confirmed")
            self.assertEqual(
                result.response,
                {"accepted": True, "orderId": "stable-1"},
            )
            events = self._events(
                path,
                dispatcher,
                "snapshot-authority-positive-sent",
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionSent",
                ],
            )
            self.assertEqual(
                events[-1]["payload"]["response_encoding"],
                "utf-8-json",
            )

    def test_unchanged_snapshot_authority_preserves_opaque_unknown(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            outbound = 0
            raw = b"\xff\x00opaque-ack"

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

            result = self._dispatch(
                dispatcher,
                "snapshot-authority-positive-opaque",
                transport,
            )

            self.assertEqual(outbound, 1)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "provider_ack_opaque")
            events = self._events(
                path,
                dispatcher,
                "snapshot-authority-positive-opaque",
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionUnknown",
                ],
            )
            terminal = events[-1]["payload"]
            self.assertEqual(terminal["response_encoding"], "hex")
            self.assertEqual(terminal["response_text"], raw.hex())
            self.assertEqual(
                terminal["retry_disposition"],
                "RECONCILE_FIRST",
            )


    def test_transport_exception_restores_decoder_before_unknown_journal_read(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            json_module = dispatch_module.json
            original_loads = json_module.loads
            forged_calls = 0

            def forged_loads(_text, **_kwargs):
                nonlocal forged_calls
                forged_calls += 1
                return {"forged": True}

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                json_module.loads = forged_loads
                raise RuntimeError("provider transport failed after wire")

            try:
                result = self._dispatch(
                    dispatcher,
                    "snapshot-transport-raise-restores-json",
                    transport,
                )
                self.assertIs(json_module.loads, original_loads)
                self.assertEqual(forged_calls, 0)
            finally:
                json_module.loads = original_loads

            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "transport_result_ambiguous")
            events = self._events(
                path,
                dispatcher,
                "snapshot-transport-raise-restores-json",
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
                "transport_exception_after_send_barrier:RuntimeError",
            )
            self.assertNotIn("response_text", events[-1]["payload"])


if __name__ == "__main__":
    unittest.main()
