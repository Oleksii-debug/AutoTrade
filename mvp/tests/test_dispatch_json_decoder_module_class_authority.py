from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import dispatch as dispatch_module
from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
)
from mvp.autotrade_mvp.persistence import JournalStore


class JsonDecoderModuleClassAuthorityTests(unittest.TestCase):
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
            now="2026-10-06T18:25:00Z",
            authority_check=lambda *_args: (True, "allowed"),
            transport_send=transport,
            submission_scope={"endpoint": "/orders"},
        )

    def _assert_unknown_after_send(self, path, dispatcher, attempt_id, result):
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(result.reason, "sent_response_persistence_failed")
        events = self._events(path, dispatcher, attempt_id)
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

    def test_transport_cannot_add_json_decoder_getattribute(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            decoder = dispatch_module.json.JSONDecoder
            self.assertNotIn("__getattribute__", decoder.__dict__)
            forged_calls = 0

            def forged_getattribute(self, name):
                nonlocal forged_calls
                forged_calls += 1
                return object.__getattribute__(self, name)

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                decoder.__getattribute__ = forged_getattribute
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "decoder-class-surface-retarget",
                    transport,
                )
                self.assertNotIn("__getattribute__", decoder.__dict__)
                self.assertEqual(forged_calls, 0)
            finally:
                if "__getattribute__" in decoder.__dict__:
                    del decoder.__getattribute__

            self._assert_unknown_after_send(
                path,
                dispatcher,
                "decoder-class-surface-retarget",
                result,
            )

    def test_transport_cannot_retarget_json_module_class(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            json_module = dispatch_module.json
            module_type = type(json_module)
            forged_gets = 0

            class ForgedJsonModule(module_type):
                def __getattribute__(self, name):
                    nonlocal forged_gets
                    if name == "loads":
                        forged_gets += 1
                        raise AssertionError("forged json module access")
                    return module_type.__getattribute__(self, name)

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                module_type.__setattr__(
                    json_module,
                    "__class__",
                    ForgedJsonModule,
                )
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "json-module-class-retarget",
                    transport,
                )
                self.assertIs(type(json_module), module_type)
                self.assertEqual(forged_gets, 0)
            finally:
                if type(json_module) is not module_type:
                    module_type.__setattr__(
                        json_module,
                        "__class__",
                        module_type,
                    )

            self._assert_unknown_after_send(
                path,
                dispatcher,
                "json-module-class-retarget",
                result,
            )

    def test_transport_cannot_retarget_json_scanner_module_class(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            decoder_init = dispatch_module.json.JSONDecoder.__dict__["__init__"]
            scanner_module = decoder_init.__globals__["scanner"]
            module_type = type(scanner_module)
            forged_gets = 0

            class ForgedScannerModule(module_type):
                def __getattribute__(self, name):
                    nonlocal forged_gets
                    if name == "make_scanner":
                        forged_gets += 1
                        raise AssertionError("forged scanner module access")
                    return module_type.__getattribute__(self, name)

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                module_type.__setattr__(
                    scanner_module,
                    "__class__",
                    ForgedScannerModule,
                )
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "scanner-module-class-retarget",
                    transport,
                )
                self.assertIs(type(scanner_module), module_type)
                self.assertEqual(forged_gets, 0)
            finally:
                if type(scanner_module) is not module_type:
                    module_type.__setattr__(
                        scanner_module,
                        "__class__",
                        module_type,
                    )

            self._assert_unknown_after_send(
                path,
                dispatcher,
                "scanner-module-class-retarget",
                result,
            )


if __name__ == "__main__":
    unittest.main()
