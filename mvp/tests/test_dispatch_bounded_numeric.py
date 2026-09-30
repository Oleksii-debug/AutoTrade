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
                provider="BYBIT",
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
                provider="BYBIT",
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


if __name__ == "__main__":
    unittest.main()
