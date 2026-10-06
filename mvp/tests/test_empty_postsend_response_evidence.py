"""Empty post-SEND HTTP bodies remain exact reconciliation evidence."""

from io import BytesIO
from tempfile import TemporaryDirectory
import unittest
from urllib.error import HTTPError

from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    load_submission_response_binding,
    submission_attempt_aggregate_id,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_transport import (
    AuthenticatedReadWireResponse,
    ProviderTransportError,
    SignedHttpRequest,
    TradingWireResponse,
    UrllibJsonWireClient,
    _alpaca_exact_trading_response,
    _binance_exact_trading_response,
    _bybit_exact_trading_response,
    _kraken_spot_exact_trading_response,
    _whitebit_exact_trading_response,
)


class EmptyPostSendResponseEvidenceTests(unittest.TestCase):
    @staticmethod
    def _request():
        return SignedHttpRequest(
            method="POST",
            url="https://api.example.test/v1/order",
            headers={"Content-Type": "application/json"},
            body=b"{}",
            timeout_seconds=2,
        )

    def test_typed_write_preserves_empty_body_but_authenticated_read_stays_strict(self):
        observed = TradingWireResponse(http_status=503, body=b"")
        self.assertEqual(observed.http_status, 503)
        self.assertEqual(observed.body, b"")
        with self.assertRaisesRegex(ProviderTransportError, "authenticated-read response"):
            AuthenticatedReadWireResponse(http_status=503, body=b"")

    def test_wire_client_preserves_empty_success_and_http_error_write_bodies(self):
        class EmptySuccess:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self, size=-1):
                self.last_size = size
                return b""
        class SuccessOpener:
            def __init__(self):
                self.stream = EmptySuccess()
            def open(self, *_args, **_kwargs):
                return self.stream

        client = UrllibJsonWireClient(max_response_bytes=8)
        opener = SuccessOpener()
        client._opener = opener
        success = client.send(self._request())
        self.assertIs(type(success), TradingWireResponse)
        self.assertEqual((success.http_status, success.body), (200, b""))
        self.assertEqual(opener.stream.last_size, 9)

        class ErrorOpener:
            def open(self, *_args, **_kwargs):
                raise HTTPError(
                    "https://api.example.test/v1/order",
                    503,
                    "unavailable",
                    {},
                    BytesIO(b""),
                )
        client = UrllibJsonWireClient(max_response_bytes=8)
        client._opener = ErrorOpener()
        failure = client.send(self._request())
        self.assertIs(type(failure), TradingWireResponse)
        self.assertEqual((failure.http_status, failure.body), (503, b""))

    def test_empty_2xx_is_never_definitive_json_for_any_write_classifier(self):
        cases = (
            (_bybit_exact_trading_response, "bybit_empty_response_execution_unknown"),
            (_kraken_spot_exact_trading_response, "kraken_spot_empty_response_execution_unknown"),
            (_alpaca_exact_trading_response, "alpaca_empty_response_execution_unknown"),
            (_binance_exact_trading_response, "binance_spot_empty_response_execution_unknown"),
            (_whitebit_exact_trading_response, "whitebit_empty_response_execution_unknown"),
        )
        for classifier, reason in cases:
            with self.subTest(classifier=classifier.__name__):
                response = classifier(TradingWireResponse(http_status=200, body=b""))
                self.assertIs(type(response), ExactJsonTransportResponse)
                self.assertEqual((response.response_bytes, response.http_status), (b"", 200))
                self.assertTrue(response.requires_reconciliation)
                self.assertEqual(response.ambiguity_reason, reason)
                with self.assertRaises(ValueError):
                    _ = response.payload

    def test_empty_5xx_retains_specific_transport_ambiguity_reason(self):
        cases = (
            (_bybit_exact_trading_response, "bybit_http_5xx_execution_unknown"),
            (_kraken_spot_exact_trading_response, "kraken_spot_http_5xx_execution_unknown"),
            (_alpaca_exact_trading_response, "alpaca_http_5xx_execution_unknown"),
            (_binance_exact_trading_response, "binance_spot_http_5xx_execution_unknown"),
        )
        for classifier, reason in cases:
            with self.subTest(classifier=classifier.__name__):
                response = classifier(TradingWireResponse(http_status=503, body=b""))
                self.assertEqual((response.response_bytes, response.http_status), (b"", 503))
                self.assertTrue(response.requires_reconciliation)
                self.assertEqual(response.ambiguity_reason, reason)

    def test_empty_bybit_503_is_durable_unknown_and_restart_never_resends(self):
        with TemporaryDirectory() as directory:
            path = directory + "/journal.sqlite3"
            store = JournalStore(path)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            response = _bybit_exact_trading_response(
                TradingWireResponse(http_status=503, body=b"")
            )
            sends = []
            def send(_client_order_id, _request, final_guard):
                final_guard()
                sends.append("sent")
                return response
            args = dict(
                attempt_id="attempt-empty-503",
                intent_id="intent-empty-503",
                intent_hash="intent-hash-empty-503",
                provider="BYBIT",
                request={"symbol": "BTCUSDT", "side": "BUY", "quantity": "1"},
                now="2026-10-06T00:21:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                submission_scope={},
            )
            first = dispatcher.dispatch(**args, transport_send=send)
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(first.reason, "bybit_http_5xx_execution_unknown")
            self.assertEqual(sends, ["sent"])

            aggregate_id = submission_attempt_aggregate_id(
                environment="SIMULATION",
                account_id="acct",
                attempt_id=args["attempt_id"],
            )
            terminal = store.load_events("submission_attempt", aggregate_id)[-1]
            self.assertEqual(terminal["event_type"], "SubmissionUnknown")
            self.assertEqual(terminal["payload"]["response_encoding"], "hex")
            self.assertEqual(terminal["payload"]["response_text"], "")
            self.assertEqual(terminal["payload"]["http_status"], 503)
            self.assertEqual(terminal["payload"]["retry_disposition"], "RECONCILE_FIRST")

            binding = load_submission_response_binding(
                store,
                environment="SIMULATION",
                account_id="acct",
                attempt_id=args["attempt_id"],
            )
            self.assertEqual(binding.response_bytes, b"")
            self.assertEqual(binding.response_encoding, "hex")
            self.assertEqual(binding.terminal_state, "UNKNOWN")
            self.assertEqual(binding.http_status, 503)
            self.assertEqual(binding.retry_disposition, "RECONCILE_FIRST")

            restarted = GuardedDispatcher(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            repeated = restarted.dispatch(
                **args,
                transport_send=lambda *_args: self.fail("blind provider resend"),
            )
            self.assertEqual(repeated.status, "UNKNOWN")
            self.assertEqual(sends, ["sent"])


if __name__ == "__main__":
    unittest.main()
