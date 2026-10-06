from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import dispatch as dispatch_module
from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
)
from mvp.autotrade_mvp.persistence import JournalStore


class JsonDecoderTransitiveAuthorityTests(unittest.TestCase):
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
            now="2026-10-06T18:05:00Z",
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
        self.assertNotIn("response_text", events[-1]["payload"])

    def test_transport_cannot_retarget_json_loads_kwdefault_cls(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            loads = dispatch_module.json.loads
            original_kwdefaults = loads.__kwdefaults__
            self.assertIs(type(original_kwdefaults), dict)
            original_items = tuple(original_kwdefaults.items())
            forged_calls = 0

            class ForgedDecoder:
                def __init__(self, **_kwargs):
                    nonlocal forged_calls
                    forged_calls += 1

                def decode(self, _text):
                    return {"forged": True}

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                loads.__kwdefaults__["cls"] = ForgedDecoder
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "json-loads-kwdefault-cls-retarget",
                    transport,
                )
                self.assertIs(loads.__kwdefaults__, original_kwdefaults)
                self.assertEqual(tuple(original_kwdefaults.items()), original_items)
                self.assertEqual(forged_calls, 0)
            finally:
                loads.__kwdefaults__.clear()
                loads.__kwdefaults__.update(dict(original_items))

            self._assert_unknown_after_send(
                path,
                dispatcher,
                "json-loads-kwdefault-cls-retarget",
                result,
            )

    def test_transport_cannot_rebind_json_decoder_class(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            json_module = dispatch_module.json
            original_decoder = json_module.JSONDecoder

            class ForgedDecoder:
                def __init__(self, **_kwargs):
                    raise AssertionError("forged JSONDecoder executed")

                def decode(self, _text):
                    return {"forged": True}

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
                    "json-decoder-class-retarget",
                    transport,
                )
                self.assertIs(json_module.JSONDecoder, original_decoder)
            finally:
                json_module.JSONDecoder = original_decoder

            self._assert_unknown_after_send(
                path,
                dispatcher,
                "json-decoder-class-retarget",
                result,
            )

    def test_transport_cannot_replace_json_decoder_decode_code(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            decoder = dispatch_module.json.JSONDecoder
            original_decode = decoder.__dict__["decode"]
            original_code = original_decode.__code__

            def forged_decode(self, _text, _w=None):
                raise AssertionError("forged JSONDecoder.decode executed")

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                original_decode.__code__ = forged_decode.__code__
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "json-decoder-decode-code-retarget",
                    transport,
                )
                self.assertIs(original_decode.__code__, original_code)
            finally:
                original_decode.__code__ = original_code

            self._assert_unknown_after_send(
                path,
                dispatcher,
                "json-decoder-decode-code-retarget",
                result,
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
                    "json-decoder-getattribute-retarget",
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
                "json-decoder-getattribute-retarget",
                result,
            )


    def test_transport_cannot_retarget_json_decoder_scanner_make_scanner(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            decoder_init = dispatch_module.json.JSONDecoder.__dict__["__init__"]
            decoder_globals = decoder_init.__globals__
            scanner_module = decoder_globals["scanner"]
            original_make_scanner = scanner_module.make_scanner
            forged_calls = 0

            def forged_make_scanner(_context):
                def forged_scan_once(text, _index):
                    nonlocal forged_calls
                    forged_calls += 1
                    return {"forged": True}, len(text)

                return forged_scan_once

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                scanner_module.make_scanner = forged_make_scanner
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "json-decoder-scanner-retarget",
                    transport,
                )
                self.assertIs(
                    scanner_module.make_scanner,
                    original_make_scanner,
                )
                self.assertEqual(forged_calls, 0)
            finally:
                scanner_module.make_scanner = original_make_scanner

            self._assert_unknown_after_send(
                path,
                dispatcher,
                "json-decoder-scanner-retarget",
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

                        def forged_loads(_text, **_kwargs):
                            return {"forged": True}

                        return forged_loads
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

                        def forged_make_scanner(_context):
                            def forged_scan_once(text, _index):
                                return {"forged": True}, len(text)

                            return forged_scan_once

                        return forged_make_scanner
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
                    "json-scanner-module-class-retarget",
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
                "json-scanner-module-class-retarget",
                result,
            )


    def test_transport_cannot_replace_json_decoder_decode_defaults(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            decoder = dispatch_module.json.JSONDecoder
            decode = decoder.__dict__["decode"]
            original_defaults = decode.__defaults__
            forged_calls = 0

            class Match:
                def end(self):
                    return 0

            def forged_match(_text, _index):
                nonlocal forged_calls
                forged_calls += 1
                return Match()

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                decode.__defaults__ = (forged_match,)
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "json-decoder-decode-defaults-retarget",
                    transport,
                )
                self.assertIs(decode.__defaults__, original_defaults)
                self.assertEqual(forged_calls, 0)
            finally:
                decode.__defaults__ = original_defaults

            self._assert_unknown_after_send(
                path,
                dispatcher,
                "json-decoder-decode-defaults-retarget",
                result,
            )

    def test_transport_cannot_mutate_json_decoder_init_kwdefaults(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            decoder = dispatch_module.json.JSONDecoder
            decoder_init = decoder.__dict__["__init__"]
            original_kwdefaults = decoder_init.__kwdefaults__
            self.assertIs(type(original_kwdefaults), dict)
            original_items = tuple(original_kwdefaults.items())
            equality_calls = 0

            class TrapStrict:
                def __eq__(self, _other):
                    nonlocal equality_calls
                    equality_calls += 1
                    raise AssertionError("kwdefault equality executed")

                def __ne__(self, _other):
                    nonlocal equality_calls
                    equality_calls += 1
                    raise AssertionError("kwdefault inequality executed")

            trap = TrapStrict()

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                decoder_init.__kwdefaults__["strict"] = trap
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "json-decoder-init-kwdefaults-retarget",
                    transport,
                )
                self.assertIs(decoder_init.__kwdefaults__, original_kwdefaults)
                self.assertEqual(
                    tuple(original_kwdefaults.items()),
                    original_items,
                )
                self.assertEqual(equality_calls, 0)
            finally:
                decoder_init.__kwdefaults__.clear()
                decoder_init.__kwdefaults__.update(dict(original_items))

            self._assert_unknown_after_send(
                path,
                dispatcher,
                "json-decoder-init-kwdefaults-retarget",
                result,
            )


if __name__ == "__main__":
    unittest.main()
