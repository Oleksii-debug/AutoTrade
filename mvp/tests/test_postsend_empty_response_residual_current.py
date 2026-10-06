"""Current-parent regression for exact empty post-SEND write evidence."""

from datetime import datetime, timezone
from hashlib import sha256
import json
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import GuardedDispatcher, load_submission_response_binding
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.provider_transport import (
    BINANCE_SPOT_ENDPOINT_POLICIES,
    BinanceSpotCredential,
    BinanceSpotSigner,
    BybitV5Credential,
    KrakenFuturesCredential,
    KrakenSpotCredential,
    KrakenSpotDurableNonceAllocator,
    AlpacaTradingCredential,
    ProviderTransportError,
    ProviderTransportScopeError,
    TradingWireResponse,
    WhiteBitCredential,
    _alpaca_exact_trading_response,
    _binance_exact_trading_response,
    _bybit_exact_trading_response,
    _kraken_spot_exact_trading_response,
    _whitebit_exact_trading_response,
)
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle


class EmptyWriteResidualCurrentTests(unittest.TestCase):
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


    def test_provider_text_authority_rejects_hostile_str_subclasses_before_callbacks(self):
        callbacks = []

        class HostileText(str):
            def strip(self, *_args):
                callbacks.append("strip")
                raise AssertionError("hostile strip executed")

            def upper(self):
                callbacks.append("upper")
                raise AssertionError("hostile upper executed")

            def lower(self):
                callbacks.append("lower")
                raise AssertionError("hostile lower executed")

        cases = (
            dict(
                provider_id=HostileText("BINANCE"),
                environment="PAPER",
                base_url="https://testnet.binance.vision",
                allowed_hosts=frozenset({"testnet.binance.vision"}),
            ),
            dict(
                provider_id="BINANCE",
                environment="PAPER",
                base_url="https://testnet.binance.vision",
                allowed_hosts=frozenset({HostileText("testnet.binance.vision")}),
            ),
            dict(
                provider_id="BINANCE",
                environment="PAPER",
                base_url=HostileText("https://testnet.binance.vision"),
                allowed_hosts=frozenset({"testnet.binance.vision"}),
            ),
        )
        from mvp.autotrade_mvp.provider_transport import ProviderEndpointPolicy
        for kwargs in cases:
            with self.subTest(field=repr(kwargs)):
                with self.assertRaises(ProviderTransportScopeError):
                    ProviderEndpointPolicy(**kwargs)
                self.assertEqual(callbacks, [])

        policy = ProviderEndpointPolicy(
            provider_id="BINANCE",
            environment="PAPER",
            base_url="https://testnet.binance.vision",
            allowed_hosts=frozenset({"testnet.binance.vision"}),
        )
        with self.assertRaisesRegex(ProviderTransportScopeError, "is required"):
            policy.absolute_url(HostileText("/api/v3/order"))
        self.assertEqual(callbacks, [])

        parsers = (
            WhiteBitCredential.parse,
            KrakenFuturesCredential.parse,
            KrakenSpotCredential.parse,
            AlpacaTradingCredential.parse,
            BybitV5Credential.parse,
            BinanceSpotCredential.parse,
        )
        class HostileCredentialText(str):
            def __bool__(self):
                callbacks.append("bool")
                raise AssertionError("hostile bool executed")

            def strip(self, *_args):
                callbacks.append("strip")
                raise AssertionError("hostile strip executed")

            def encode(self, *_args, **_kwargs):
                callbacks.append("encode")
                raise AssertionError("hostile encode executed")

        for parser in parsers:
            with self.subTest(parser=parser.__qualname__):
                with self.assertRaisesRegex(
                    ProviderTransportScopeError,
                    "credential material is unavailable",
                ):
                    parser(HostileCredentialText(
                        '{"api_key":"synthetic","api_secret":"synthetic"}'
                    ))
                self.assertEqual(callbacks, [])

    def test_kraken_legacy_nonce_history_rejects_noncanonical_builtin_handle_text(self):
        fixed = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            legacy_scope = {
                "credential_handle_id": " legacy-trade ",
                "credential_generation": 1,
            }
            aggregate_material = (
                "acct-kraken|LIVE|"
                + json.dumps(
                    legacy_scope,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
            )
            aggregate_id = (
                "KRAKEN:"
                + sha256(aggregate_material.encode("utf-8")).hexdigest()
            )
            payload = {
                "provider_id": "KRAKEN",
                "account_id": "acct-kraken",
                "environment": "LIVE",
                "nonce": 700,
                **legacy_scope,
            }
            store.append_event(
                {
                    "event_id": "legacy-noncanonical-handle",
                    "event_type": "ProviderNonceAllocated",
                    "aggregate_type": "provider_nonce",
                    "aggregate_id": aggregate_id,
                    "aggregate_version": "1",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": fixed.isoformat().replace("+00:00", "Z"),
                }
            )

            handle = PersistentCredentialHandle(
                handle_id="current-trade",
                account_id="acct-kraken",
                provider="KRAKEN",
                environment="LIVE",
                purpose="TRADE",
                generation=1,
            )
            with self.assertRaisesRegex(
                ProviderTransportError,
                "legacy nonce scope is invalid",
            ):
                KrakenSpotDurableNonceAllocator(
                    journal=store,
                    account_id="acct-kraken",
                    environment="LIVE",
                    credential_handle=handle,
                    clock_millis=lambda: 100,
                    clock_utc=lambda: fixed,
                )

    def test_binance_signer_rejects_hostile_parameter_value_before_strip(self):
        callbacks = []

        class HostileValue(str):
            def strip(self, *_args):
                callbacks.append("strip")
                raise AssertionError("hostile strip executed")

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "order parameters must be canonical strings",
        ):
            BinanceSpotSigner.sign(
                policy=BINANCE_SPOT_ENDPOINT_POLICIES["PAPER"],
                endpoint=BinanceSpotSigner.PLACE_ORDER_ENDPOINT,
                body={
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "LIMIT",
                    "quantity": HostileValue("0.001"),
                    "price": "50000",
                    "timeInForce": "GTC",
                },
                credential_plaintext='{"api_key":"key","api_secret":"secret"}',
                timestamp_ms=1,
            )
        self.assertEqual(callbacks, [])


if __name__ == "__main__":
    unittest.main()
