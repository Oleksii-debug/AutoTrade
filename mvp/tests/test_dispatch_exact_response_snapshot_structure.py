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

            def forged_loads(_text, **_kwargs):
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

    def test_transport_cannot_replace_depth_guard_code_after_send(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            depth_guard = dispatch_module.require_provider_json_depth
            original_code = depth_guard.__code__

            def forged_depth_guard(_raw):
                return None

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                depth_guard.__code__ = forged_depth_guard.__code__
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "snapshot-depth-guard-retarget",
                    transport,
                )
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


if __name__ == "__main__":
    unittest.main()
