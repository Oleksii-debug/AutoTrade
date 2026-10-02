"""Exact raw provider-response transport admission before financial evidence."""

from decimal import Decimal
from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import autotrade_numeric.exact_decimal as neutral_numeric
from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    load_submission_response_binding,
    submission_attempt_aggregate_id,
)
from mvp.autotrade_mvp.persistence import JournalStore


class DispatchBoundedNumericTransportTests(unittest.TestCase):
    def test_authoritative_submission_journal_rejects_subclass_before_callbacks(self):
        touched = []

        class HostileJournalStore(JournalStore):
            def load_events(self, *_args, **_kwargs):
                touched.append("load_events")
                raise AssertionError("caller journal override executed")

            def append_event(self, *_args, **_kwargs):
                touched.append("append_event")
                raise AssertionError("caller journal override executed")

        with TemporaryDirectory() as directory:
            hostile = HostileJournalStore(directory + "/journal.sqlite3")

            with self.assertRaisesRegex(
                TypeError, "canonical JournalStore"
            ):
                load_submission_response_binding(
                    hostile,
                    environment="SIMULATION",
                    account_id="acct",
                    attempt_id="forged-attempt",
                )

            with self.assertRaisesRegex(
                TypeError, "canonical JournalStore"
            ):
                GuardedDispatcher(
                    hostile,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="owner",
                )

        self.assertEqual(touched, [])

    def test_exact_journal_instance_shadow_and_generation_replacement_fail_closed(self):
        touched = []

        def hostile_load(*_args, **_kwargs):
            touched.append("load_events")
            raise AssertionError("instance load override executed")

        def hostile_append(*_args, **_kwargs):
            touched.append("append_event")
            raise AssertionError("instance append override executed")

        def hostile_connect(*_args, **_kwargs):
            touched.append("_connect")
            raise AssertionError("instance connection override executed")

        with TemporaryDirectory() as directory:
            store = JournalStore(directory + "/journal.sqlite3")
            store.__dict__["load_events"] = hostile_load
            with self.assertRaisesRegex(TypeError, "instance state is shadowed"):
                load_submission_response_binding(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    attempt_id="shadowed-load",
                )
            del store.__dict__["load_events"]

            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )

            for name, callback in (
                ("load_events", hostile_load),
                ("_connect", hostile_connect),
            ):
                with self.subTest(name=name):
                    store.__dict__[name] = callback
                    with self.assertRaisesRegex(
                        TypeError, "instance state is shadowed"
                    ):
                        dispatcher._events("shadowed-attempt")
                    del store.__dict__[name]

            store.__dict__["append_event"] = hostile_append
            with self.assertRaisesRegex(TypeError, "instance state is shadowed"):
                dispatcher._append(
                    attempt_id="shadowed-attempt",
                    event_type="SubmissionPrepared",
                    version=1,
                    payload={},
                    now="2026-09-30T17:00:00Z",
                )
            del store.__dict__["append_event"]

            other = JournalStore(directory + "/other.sqlite3")
            original_path = store.path
            original_identity = store.store_identity
            store.path = other.path
            store._store_identity = other.store_identity
            with self.assertRaisesRegex(
                PermissionError, "submission journal authority changed"
            ):
                dispatcher._events("mutated-generation")
            store.path = original_path
            store._store_identity = original_identity

            dispatcher.store = other
            with self.assertRaisesRegex(
                PermissionError, "submission journal authority changed"
            ):
                dispatcher._events("wrong-generation")

        self.assertEqual(touched, [])

    def test_shared_depth_boundary_and_parser_recursion_remain_redacted(self):
        at_limit = b"[" * 64 + b"0" + b"]" * 64
        too_deep = b"[" * 65 + b"0" + b"]" * 65
        self.assertEqual(ExactJsonTransportResponse(at_limit).payload, 
                         __import__("json").loads(at_limit))
        for raw in (too_deep, b'{"v":' + b"[" * 65 + b"0" + b"]" * 65 + b"}"):
            with self.subTest(raw_length=len(raw)), self.assertRaisesRegex(
                ValueError, "shared JSON resource budget"
            ) as denied:
                ExactJsonTransportResponse(raw)
            self.assertIsNone(denied.exception.__cause__)
            self.assertIsNone(denied.exception.__context__)
        marker = "SYNTHETIC_SECRET_NOT_FOR_DIAGNOSTICS"
        with patch(
            "mvp.autotrade_mvp.dispatch.json.loads",
            side_effect=RecursionError(marker),
        ):
            with self.assertRaisesRegex(
                ValueError, "shared JSON resource budget"
            ) as denied:
                ExactJsonTransportResponse(b'{"value":1}')
        self.assertNotIn(marker, str(denied.exception))
        self.assertIsNone(denied.exception.__cause__)
        self.assertIsNone(denied.exception.__context__)

    def test_deep_post_send_remains_durable_unknown_with_no_second_wire(self):
        raw = b'{"price":' + b"[" * 65 + b"0" + b"]" * 65 + b"}"
        with TemporaryDirectory() as directory:
            store = JournalStore(directory + "/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store, environment="SIMULATION", account_id="acct", owner_token="owner"
            )
            calls = []
            def after_barrier(_cid, _request, guard):
                guard()
                calls.append("wire")
                return ExactJsonTransportResponse(raw)
            args = {
                "attempt_id": "overdepth-after-send",
                "intent_id": "overdepth-intent",
                "intent_hash": "overdepth-financial-intent",
                "provider": "BYBIT",
                "request": {"symbol": "BTCUSD", "qty": "1"},
                "now": "2026-09-24T18:00:00Z",
                "authority_check": lambda _hash, _now: (True, "allowed"),
            }
            outcome = dispatcher.dispatch(**args, transport_send=after_barrier)
            self.assertEqual(outcome.status, "UNKNOWN")
            self.assertEqual(calls, ["wire"])
            aggregate_id = submission_attempt_aggregate_id(
                environment="SIMULATION", account_id="acct",
                attempt_id="overdepth-after-send",
            )
            events = store.load_events("submission_attempt", aggregate_id)
            self.assertEqual(
                [e["event_type"] for e in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )
            restart = GuardedDispatcher(
                JournalStore(directory + "/journal.sqlite3"),
                environment="SIMULATION", account_id="acct", owner_token="owner",
            )
            again = restart.dispatch(
                **args, transport_send=lambda *_: self.fail("blind wire retry")
            )
            self.assertEqual(again.status, "UNKNOWN")
            self.assertEqual(calls, ["wire"])

    def test_structurally_inadmissible_exact_replay_is_unknown(self):
        raw = b"[" * 65 + b"0" + b"]" * 65
        event = {
            "event_type": "SubmissionSent",
            "payload": {
                "response_encoding": "utf-8-json",
                "response_text": raw.decode("ascii"),
                "response_sha256": "sha256:" + sha256(raw).hexdigest(),
            },
        }
        outcome = GuardedDispatcher._outcome_from_terminal(event, "client-order")
        self.assertEqual(outcome.status, "UNKNOWN")
        self.assertIsNone(outcome.response)


    def test_transport_preview_and_durable_response_are_both_exact(self):
        raw = (
            b'{"price":65000.10,"fee":0.0100,"integer":12345678901234567890,'
            b'"zero":0e-99999999999999}'
        )
        preview = ExactJsonTransportResponse(raw)
        payload = preview.payload
        self.assertIs(type(payload["price"]), Decimal)
        self.assertEqual(
            payload["price"].as_tuple(),
            Decimal("65000.10").as_tuple(),
        )
        self.assertEqual(
            payload["fee"].as_tuple(),
            Decimal("0.0100").as_tuple(),
        )
        self.assertIs(type(payload["integer"]), int)
        self.assertEqual(payload["zero"], Decimal(0))
        self.assertEqual(preview.response_bytes, raw)
        self.assertEqual(preview.response_sha256, "sha256:" + sha256(raw).hexdigest())

        with TemporaryDirectory() as directory:
            store = JournalStore(directory + "/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store, environment="SIMULATION", account_id="acct", owner_token="owner"
            )
            def send(_cid, _request, guard):
                guard()
                return ExactJsonTransportResponse(raw)
            outcome = dispatcher.dispatch(
                attempt_id="numeric-response-a1",
                intent_id="numeric-intent-a1",
                intent_hash="numeric-intent-hash",
                provider="KRAKEN",
                request={"symbol":"BTCUSD","qty":"1"},
                now="2026-09-24T18:00:00Z",
                authority_check=lambda _hash, _now:(True,"allowed"),
                transport_send=send,
            )
            self.assertEqual(outcome.status, "SENT")
            recovered = load_submission_response_binding(
                store,
                environment="SIMULATION",
                account_id="acct",
                attempt_id="numeric-response-a1",
            )
            self.assertEqual(recovered.response_bytes, raw)
            self.assertEqual(
                recovered.payload["price"].as_tuple(),
                Decimal("65000.10").as_tuple(),
            )
            self.assertIs(type(recovered.payload["integer"]), int)
            aggregate_id = submission_attempt_aggregate_id(
                environment="SIMULATION",
                account_id="acct",
                attempt_id="numeric-response-a1",
            )
            terminal = store.load_events("submission_attempt", aggregate_id)[-1]
            self.assertEqual(terminal["event_type"], "SubmissionSent")
            self.assertNotIn("response", terminal["payload"])
            self.assertEqual(
                terminal["payload"]["response_sha256"],
                "sha256:" + sha256(raw).hexdigest(),
            )
            # Exact replay is reconstructed from the SHA-bound bytes. A
            # duplicate attempt never sends again and must preserve Decimal
            # coefficient/trailing-zero identity across restart.
            def forbidden_send(*_args):
                self.fail("terminal exact submission was blindly retried")
            repeated = dispatcher.dispatch(
                attempt_id="numeric-response-a1",
                intent_id="numeric-intent-a1",
                intent_hash="numeric-intent-hash",
                provider="KRAKEN",
                request={"symbol":"BTCUSD","qty":"1"},
                now="2026-09-24T18:00:00Z",
                authority_check=lambda _hash, _now:(True,"allowed"),
                transport_send=forbidden_send,
            )
            self.assertEqual(repeated.status, "SENT")
            self.assertEqual(
                repeated.response["price"].as_tuple(),
                Decimal("65000.10").as_tuple(),
            )

    def test_exact_subclass_after_send_stays_unknown_and_never_retries(self):
        called = []
        raw = b'{"price":12.3400}'
        class HostileResponse(ExactJsonTransportResponse):
            @property
            def response_text(self):
                called.append("text")
                raise AssertionError("subtype getter")
            @property
            def response_sha256(self):
                called.append("digest")
                raise AssertionError("subtype getter")
            @property
            def payload(self):
                called.append("payload")
                raise AssertionError("subtype getter")
        hostile = HostileResponse(raw)
        with TemporaryDirectory() as directory:
            store = JournalStore(directory + "/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store, environment="SIMULATION", account_id="acct", owner_token="owner"
            )
            outbound = []
            def send(_cid, _request, guard):
                guard()
                outbound.append(raw)
                return hostile
            args = {
                "attempt_id": "hostile-after-send",
                "intent_id": "hostile-after-send-intent",
                "intent_hash": "hostile-after-send-hash",
                "provider": "BYBIT",
                "request": {"symbol": "BTCUSD", "qty": "1"},
                "now": "2026-09-24T18:00:00Z",
                "authority_check": lambda _hash, _now: (True, "allowed"),
            }
            result = dispatcher.dispatch(**args, transport_send=send)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(called, [])
            self.assertEqual(len(outbound), 1)
            aggregate_id = submission_attempt_aggregate_id(
                environment="SIMULATION", account_id="acct",
                attempt_id="hostile-after-send",
            )
            events = store.load_events("submission_attempt", aggregate_id)
            self.assertEqual(
                [e["event_type"] for e in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )
            retry = dispatcher.dispatch(
                **args,
                transport_send=lambda *_: self.fail("blind retry"),
            )
            self.assertEqual(retry.status, "UNKNOWN")
            self.assertEqual(len(outbound), 1)

    def test_exact_response_metadata_subclasses_never_dispatch_callbacks(self):
        touched = []
        class HostileStatus(int):
            def __lt__(self, value):
                touched.append("compare")
                raise AssertionError("virtual int comparison")
        class HostileReason(str):
            def strip(self):
                touched.append("strip")
                raise AssertionError("virtual str strip")
        raw = b'{"price":12.34}'
        with self.assertRaises(ValueError):
            ExactJsonTransportResponse(raw, http_status=HostileStatus(200))
        with self.assertRaises(ValueError):
            ExactJsonTransportResponse(
                raw, requires_reconciliation=True,
                ambiguity_reason=HostileReason("needs reconciliation"),
            )
        self.assertEqual(touched, [])

    def test_resource_excess_cannot_construct_a_decimal_before_rejection(self):
        invalid = (
            b'{"price":1e256}',
            b'{"price":1e-257}',
            b'{"price":' + b'9' * 257 + b'}',
            b'{"price":0.' + b'0' * 256 + b'1}',
            b'{"integer":' + b'9' * 257 + b'}',
        )
        for raw in invalid:
            with self.subTest(length=len(raw), prefix=raw[:16]):
                with patch.object(
                    neutral_numeric,
                    "Decimal",
                    side_effect=AssertionError("Decimal constructed before preflight"),
                ):
                    with self.assertRaisesRegex(
                        ValueError, "invalid or oversized exact JSON number"
                    ) as rejected:
                        ExactJsonTransportResponse(raw)
                self.assertIsInstance(
                    rejected.exception.__cause__,
                    neutral_numeric.ExactDecimalError,
                )

    def test_malformed_numeric_after_send_remains_durable_unknown_not_retryable(self):
        for counter, raw in enumerate(
            (
                b'{"price":1e256}',
                b'{"price":' + b'9' * 257 + b'}',
            )
        ):
            with self.subTest(counter=counter), TemporaryDirectory() as directory:
                store = JournalStore(directory + "/journal.sqlite3")
                dispatcher = GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="owner",
                )
                attempt_id = f"invalid-numeric-post-send-{counter}"
                sends = []
                def send(_cid, _request, guard):
                    guard()
                    sends.append(raw)
                    return ExactJsonTransportResponse(raw)
                args = {
                    "attempt_id": attempt_id,
                    "intent_id": f"numeric-intent-{counter}",
                    "intent_hash": "numeric-economic-intent",
                    "provider": "BYBIT",
                    "request": {"symbol":"BTCUSD","qty":"1"},
                    "now": "2026-09-24T18:00:00Z",
                    "authority_check": lambda _hash, _now:(True,"allowed"),
                }
                outcome = dispatcher.dispatch(**args, transport_send=send)
                self.assertEqual(outcome.status, "UNKNOWN")
                self.assertEqual(len(sends), 1)
                aggregate_id = submission_attempt_aggregate_id(
                    environment="SIMULATION",
                    account_id="acct",
                    attempt_id=attempt_id,
                )
                event_types = [
                    row["event_type"]
                    for row in store.load_events("submission_attempt", aggregate_id)
                ]
                self.assertEqual(
                    event_types,
                    ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
                )
                outcome_again = dispatcher.dispatch(
                    **args,
                    transport_send=lambda *_: self.fail("blind provider retry"),
                )
                self.assertEqual(outcome_again.status, "UNKNOWN")
                self.assertEqual(len(sends), 1)

    def test_invalid_utf8_and_generic_json_errors_do_not_reflect_provider_bytes(self):
        for raw in (
            b'{"raw-secret":"dont-print-me-\xff"}',
            b'{"raw-secret":"dont-print-me","price": }',
            b'{"raw-secret":"dont-print-me" "price":2}',
        ):
            with self.subTest(prefix=raw[:18]):
                with self.assertRaises(ValueError) as caught:
                    ExactJsonTransportResponse(raw)
                self.assertEqual(
                    str(caught.exception),
                    "provider response must be exact UTF-8 JSON bytes",
                )
                self.assertNotIn("dont-print-me", str(caught.exception))
                self.assertNotIn("dont-print-me", repr(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)

    def test_duplicate_key_diagnostic_is_fixed_and_secret_free(self):
        marker = "AUTOTRADE_SYNTHETIC_SECRET_MARKER_f1137"
        raw = ('{"' + marker + '":1,"' + marker + '":2}').encode("utf-8")
        with self.assertRaisesRegex(
            ValueError, "^provider response contains duplicate JSON keys$"
        ) as caught:
            ExactJsonTransportResponse(raw)
        current = caught.exception
        visited = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            self.assertNotIn(marker, str(current))
            self.assertNotIn(marker, repr(current))
            current = current.__cause__ or current.__context__

    def test_duplicate_key_after_send_is_unknown_and_never_retried(self):
        marker = "AUTOTRADE_SYNTHETIC_SECRET_MARKER_send"
        raw = ('{"' + marker + '":1,"' + marker + '":2}').encode("utf-8")
        with TemporaryDirectory() as directory:
            store = JournalStore(directory + "/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            sends = []

            def send(_cid, _request, guard):
                guard()
                sends.append(raw)
                return ExactJsonTransportResponse(raw)

            args = {
                "attempt_id": "duplicate-secret-post-send",
                "intent_id": "duplicate-secret-intent",
                "intent_hash": "duplicate-secret-intent-hash",
                "provider": "BYBIT",
                "request": {"symbol": "BTCUSD", "qty": "1"},
                "now": "2026-09-24T18:00:00Z",
                "authority_check": lambda _hash, _now: (True, "allowed"),
            }
            outcome = dispatcher.dispatch(**args, transport_send=send)
            self.assertEqual(outcome.status, "UNKNOWN")
            self.assertEqual(len(sends), 1)
            aggregate_id = submission_attempt_aggregate_id(
                environment="SIMULATION",
                account_id="acct",
                attempt_id=args["attempt_id"],
            )
            events = store.load_events("submission_attempt", aggregate_id)
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )
            self.assertNotIn(marker, repr(events))
            retry = dispatcher.dispatch(
                **args,
                transport_send=lambda *_: self.fail("blind provider retry"),
            )
            self.assertEqual(retry.status, "UNKNOWN")
            self.assertEqual(len(sends), 1)

    def test_duplicate_and_nonfinite_json_fail_closed(self):
        for raw in (
            b'{"price":1.25,"price":2.5}',
            b'{"price":NaN}',
            b'{"price":Infinity}',
            b'{"price":-Infinity}',
            b'{"price":1e256,"price":3}',
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                ExactJsonTransportResponse(raw)


    def test_partial_exact_terminal_markers_never_fall_back_to_legacy_or_resend(self):
        raw = b'{"provider_order_id":"p-1","status":"ACK"}'
        with TemporaryDirectory() as directory:
            store = JournalStore(directory + "/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store, environment="SIMULATION", account_id="acct", owner_token="owner"
            )
            sends = []

            def send(_cid, _request, guard):
                guard()
                sends.append(raw)
                return ExactJsonTransportResponse(raw)

            args = {
                "attempt_id": "partial-exact-marker-a1",
                "intent_id": "partial-exact-marker-intent",
                "intent_hash": "partial-exact-marker-hash",
                "provider": "BYBIT",
                "request": {"symbol": "BTCUSD", "qty": "1"},
                "now": "2026-09-24T18:00:00Z",
                "authority_check": lambda _hash, _now: (True, "allowed"),
            }
            first = dispatcher.dispatch(**args, transport_send=send)
            self.assertEqual(first.status, "SENT")
            self.assertEqual(len(sends), 1)
            original_events = dispatcher._events(args["attempt_id"])
            exact = dict(original_events[-1]["payload"])
            legacy = {"legacy": "must-not-authorize"}

            variants = []
            for keep in (
                {"response_text"},
                {"response_sha256"},
                {"response_encoding"},
            ):
                payload = {
                    key: value
                    for key, value in exact.items()
                    if key == "client_order_id" or key in keep
                }
                variants.append(payload)
            wrong = dict(exact)
            wrong["response_encoding"] = "json"
            variants.append(wrong)
            mixed = {
                "client_order_id": exact["client_order_id"],
                "response_text": exact["response_text"],
                "response": legacy,
            }
            variants.append(mixed)

            for counter, payload in enumerate(variants):
                with self.subTest(counter=counter):
                    altered = [dict(item) for item in original_events]
                    terminal = dict(altered[-1])
                    terminal["payload"] = payload
                    altered[-1] = terminal
                    with patch.object(dispatcher, "_events", return_value=altered):
                        recovered = dispatcher.dispatch(
                            **args,
                            transport_send=lambda *_: self.fail("blind provider retry"),
                        )
                    self.assertEqual(recovered.status, "UNKNOWN")
                    self.assertIsNone(recovered.response)
                    self.assertEqual(len(sends), 1)

            marker_free = {
                "event_type": "SubmissionSent",
                "payload": {"response": legacy},
            }
            recovered_legacy = GuardedDispatcher._outcome_from_terminal(
                marker_free, exact["client_order_id"]
            )
            self.assertEqual(recovered_legacy.status, "SENT")
            self.assertEqual(recovered_legacy.response, legacy)



if __name__ == "__main__":
    unittest.main()
