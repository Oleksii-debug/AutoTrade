from tempfile import TemporaryDirectory
import re
import unittest

from mvp.autotrade_mvp import dispatch as dispatch_module
from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
)
from mvp.autotrade_mvp.persistence import JournalStore


class ResponseDepthNumericAuthorityTests(unittest.TestCase):
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
            now="2026-10-06T18:15:00Z",
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

    def test_transport_cannot_mutate_depth_guard_kwdefaults(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            guard = dispatch_module.require_provider_json_depth
            kwdefaults = guard.__kwdefaults__
            self.assertIs(type(kwdefaults), dict)
            original_items = tuple(kwdefaults.items())

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                kwdefaults["max_depth"] = 4096
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "depth-guard-kwdefaults-retarget",
                    transport,
                )
                self.assertIs(guard.__kwdefaults__, kwdefaults)
                self.assertEqual(tuple(kwdefaults.items()), original_items)
            finally:
                kwdefaults.clear()
                kwdefaults.update(dict(original_items))

            self._assert_unknown_after_send(
                path,
                dispatcher,
                "depth-guard-kwdefaults-retarget",
                result,
            )

    def test_transport_cannot_rebind_depth_guard_byte_validator(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            guard = dispatch_module.require_provider_json_depth
            namespace = guard.__globals__
            original = namespace["require_provider_response_bytes"]
            forged_calls = 0

            def forged_validator(raw, **_kwargs):
                nonlocal forged_calls
                forged_calls += 1
                return raw

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )
                namespace["require_provider_response_bytes"] = forged_validator
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "depth-byte-validator-retarget",
                    transport,
                )
                self.assertIs(
                    namespace["require_provider_response_bytes"],
                    original,
                )
                self.assertEqual(forged_calls, 0)
            finally:
                namespace["require_provider_response_bytes"] = original

            self._assert_unknown_after_send(
                path,
                dispatcher,
                "depth-byte-validator-retarget",
                result,
            )

    def test_transport_cannot_rebind_number_parser_exact_decimal(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            parser = dispatch_module.parse_bounded_json_number_token
            namespace = parser.__globals__
            original = namespace["parse_bounded_exact_decimal"]
            forged_calls = 0

            def forged_decimal(_text):
                nonlocal forged_calls
                forged_calls += 1
                return "forged-number"

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"price":1.25}',
                    http_status=200,
                )
                namespace["parse_bounded_exact_decimal"] = forged_decimal
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "number-parser-global-retarget",
                    transport,
                )
                self.assertIs(
                    namespace["parse_bounded_exact_decimal"],
                    original,
                )
                self.assertEqual(forged_calls, 0)
            finally:
                namespace["parse_bounded_exact_decimal"] = original

            self._assert_unknown_after_send(
                path,
                dispatcher,
                "number-parser-global-retarget",
                result,
            )

    def test_transport_cannot_rebind_transitive_decimal_preflight(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            parser = dispatch_module.parse_bounded_json_number_token
            parse_decimal = parser.__globals__["parse_bounded_exact_decimal"]
            namespace = parse_decimal.__globals__
            original = namespace["_preflight_bounded_presentation"]
            forged_calls = 0

            def forged_preflight(_value, *, allow_exponent):
                nonlocal forged_calls
                forged_calls += 1
                return False

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"price":1.25}',
                    http_status=200,
                )
                namespace["_preflight_bounded_presentation"] = forged_preflight
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "number-parser-transitive-global-retarget",
                    transport,
                )
                self.assertIs(
                    namespace["_preflight_bounded_presentation"],
                    original,
                )
                self.assertEqual(forged_calls, 0)
            finally:
                namespace["_preflight_bounded_presentation"] = original

            self._assert_unknown_after_send(
                path,
                dispatcher,
                "number-parser-transitive-global-retarget",
                result,
            )

    def test_transport_cannot_replace_transitive_decimal_parser_code(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            parser = dispatch_module.parse_bounded_json_number_token
            parse_decimal = parser.__globals__["parse_bounded_exact_decimal"]
            original_code = parse_decimal.__code__

            def forged_decimal(value, *, allow_exponent=True):
                raise AssertionError("forged exact-decimal parser executed")

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"price":1.25}',
                    http_status=200,
                )
                parse_decimal.__code__ = forged_decimal.__code__
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "number-parser-transitive-code-retarget",
                    transport,
                )
                self.assertIs(parse_decimal.__code__, original_code)
            finally:
                parse_decimal.__code__ = original_code

            self._assert_unknown_after_send(
                path,
                dispatcher,
                "number-parser-transitive-code-retarget",
                result,
            )

    def test_transport_cannot_rebind_integer_parser_grammar(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            dispatcher = self._dispatcher(path)
            parser = dispatch_module.parse_bounded_json_integer_token
            namespace = parser.__globals__
            original = namespace["_JSON_INTEGER"]

            def transport(_client_order_id, _request, final_guard):
                final_guard()
                response = ExactJsonTransportResponse(
                    b'{"quantity":1}',
                    http_status=200,
                )
                namespace["_JSON_INTEGER"] = re.compile(r".*\\Z")
                return response

            try:
                result = self._dispatch(
                    dispatcher,
                    "integer-parser-grammar-retarget",
                    transport,
                )
                self.assertIs(namespace["_JSON_INTEGER"], original)
            finally:
                namespace["_JSON_INTEGER"] = original

            self._assert_unknown_after_send(
                path,
                dispatcher,
                "integer-parser-grammar-retarget",
                result,
            )


if __name__ == "__main__":
    unittest.main()
