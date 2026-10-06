"""Current-parent regression for exact empty post-SEND write evidence."""

from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import GuardedDispatcher, load_submission_response_binding
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_transport import (
    TradingWireResponse,
    _alpaca_exact_trading_response,
    _binance_exact_trading_response,
    _bybit_exact_trading_response,
    _kraken_spot_exact_trading_response,
    _whitebit_exact_trading_response,
)


class EmptyWriteResidualCurrentTests(unittest.TestCase):
    def test_response_resource_helper_rebinding_fails_before_callback(self):
        import mvp.autotrade_mvp.provider_transport as transport_module
        from unittest.mock import patch

        original = transport_module.require_provider_response_bytes
        calls = []

        def hostile(*_args, **_kwargs):
            calls.append("called")
            raise AssertionError("hostile response resource helper executed")

        with patch.object(transport_module, "require_provider_response_bytes", hostile):
            with self.assertRaisesRegex(
                transport_module.ProviderTransportScopeError,
                "provider response resource authority changed",
            ):
                TradingWireResponse(http_status=200, body=b"{}")
        self.assertEqual(calls, [])
        self.assertIs(
            transport_module.require_provider_response_bytes,
            original,
        )

    def test_raw_trading_response_resource_helper_rebinding_fails_before_callback(self):
        import mvp.autotrade_mvp.provider_transport as transport_module
        from unittest.mock import patch

        calls = []

        def hostile(*_args, **_kwargs):
            calls.append("called")
            raise AssertionError("hostile response resource helper executed")

        with patch.object(transport_module, "require_provider_response_bytes", hostile):
            with self.assertRaisesRegex(
                transport_module.ProviderTransportScopeError,
                "provider response resource authority changed",
            ):
                _bybit_exact_trading_response(b"{}")
        self.assertEqual(calls, [])

    def test_response_resource_helper_code_mutation_fails_closed(self):
        import mvp.autotrade_mvp.provider_transport as transport_module

        helper = transport_module.require_provider_response_bytes
        original_code = helper.__code__
        forged_code = (
            lambda raw, *, max_bytes=1, allow_empty=False: raw
        ).__code__
        try:
            helper.__code__ = forged_code
            with self.assertRaisesRegex(
                transport_module.ProviderTransportScopeError,
                "provider response resource authority changed",
            ):
                TradingWireResponse(http_status=200, body=b"{}")
        finally:
            helper.__code__ = original_code

    def test_response_resource_ceiling_rebinding_fails_closed(self):
        import mvp.autotrade_mvp.provider_transport as transport_module
        from unittest.mock import patch

        with patch.object(
            transport_module,
            "HARD_MAX_PROVIDER_RESPONSE_BYTES",
            1,
        ):
            with self.assertRaisesRegex(
                transport_module.ProviderTransportScopeError,
                "provider response resource authority changed",
            ):
                TradingWireResponse(http_status=200, body=b"{}")

    def test_empty_http_200_is_reconciliation_first_for_every_current_write_classifier(self):
        cases = (
            (_bybit_exact_trading_response, "bybit_empty_response_execution_unknown"),
            (
                _kraken_spot_exact_trading_response,
                "kraken_spot_empty_response_execution_unknown",
            ),
            (_alpaca_exact_trading_response, "alpaca_empty_response_execution_unknown"),
            (
                _binance_exact_trading_response,
                "binance_spot_empty_response_execution_unknown",
            ),
            (
                _whitebit_exact_trading_response,
                "whitebit_empty_response_execution_unknown",
            ),
        )
        for classifier, reason in cases:
            with self.subTest(classifier=classifier.__name__):
                response = classifier(TradingWireResponse(http_status=200, body=b""))
                self.assertEqual(response.response_bytes, b"")
                self.assertEqual(response.http_status, 200)
                self.assertTrue(response.requires_reconciliation)
                self.assertEqual(response.ambiguity_reason, reason)
                with self.assertRaises(ValueError):
                    _ = response.payload

    def test_empty_legacy_write_evidence_remains_unknown_without_status(self):
        cases = (
            (
                _bybit_exact_trading_response,
                "bybit_http_status_unavailable_execution_unknown",
            ),
            (
                _kraken_spot_exact_trading_response,
                "kraken_spot_http_status_unavailable_execution_unknown",
            ),
            (
                _alpaca_exact_trading_response,
                "alpaca_http_status_unavailable_execution_unknown",
            ),
            (
                _binance_exact_trading_response,
                "binance_spot_http_status_unavailable_execution_unknown",
            ),
            (
                _whitebit_exact_trading_response,
                "whitebit_http_status_unavailable_execution_unknown",
            ),
        )
        for classifier, reason in cases:
            with self.subTest(classifier=classifier.__name__):
                response = classifier(b"")
                self.assertEqual(response.response_bytes, b"")
                self.assertIsNone(response.http_status)
                self.assertTrue(response.requires_reconciliation)
                self.assertEqual(response.ambiguity_reason, reason)

    def test_empty_bybit_http_200_is_durable_unknown_and_restart_never_resends(self):
        with TemporaryDirectory() as directory:
            path = directory + "/journal.sqlite3"
            store = JournalStore(path)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct-empty-200",
                owner_token="owner-empty-200",
            )
            response = _bybit_exact_trading_response(
                TradingWireResponse(http_status=200, body=b"")
            )
            sends = []

            def send(_client_order_id, _request, final_guard):
                final_guard()
                sends.append("sent")
                return response

            args = dict(
                attempt_id="attempt-empty-200",
                intent_id="intent-empty-200",
                intent_hash="intent-hash-empty-200",
                provider="BYBIT",
                request={"symbol": "BTCUSDT", "side": "BUY", "quantity": "1"},
                now="2026-10-06T00:40:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                submission_scope={},
            )
            first = dispatcher.dispatch(**args, transport_send=send)
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(first.reason, "bybit_empty_response_execution_unknown")
            self.assertEqual(sends, ["sent"])

            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id(args["attempt_id"]),
            )
            terminal = events[-1]
            self.assertEqual(terminal["event_type"], "SubmissionUnknown")
            self.assertEqual(terminal["payload"]["response_encoding"], "hex")
            self.assertEqual(terminal["payload"]["response_text"], "")
            self.assertEqual(terminal["payload"]["http_status"], 200)
            self.assertEqual(
                terminal["payload"]["retry_disposition"],
                "RECONCILE_FIRST",
            )

            binding = load_submission_response_binding(
                store,
                environment="SIMULATION",
                account_id="acct-empty-200",
                attempt_id=args["attempt_id"],
            )
            self.assertEqual(binding.response_bytes, b"")
            self.assertEqual(binding.response_encoding, "hex")
            self.assertEqual(binding.terminal_state, "UNKNOWN")
            self.assertEqual(binding.http_status, 200)
            self.assertEqual(
                binding.retry_disposition,
                "RECONCILE_FIRST",
            )

            restarted = GuardedDispatcher(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct-empty-200",
                owner_token="owner-empty-200",
            )
            repeated = restarted.dispatch(
                **args,
                transport_send=lambda *_args: self.fail("blind provider resend"),
            )
            self.assertEqual(repeated.status, "UNKNOWN")
            self.assertEqual(sends, ["sent"])


if __name__ == "__main__":
    unittest.main()
