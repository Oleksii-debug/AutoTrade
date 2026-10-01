"""#652 exact opaque post-SEND provider response evidence regressions.

Non-networked acceptance tests for preserving definitive Binance 5xx status
and bounded raw body bytes without promoting opaque data to JSON authority.
"""
from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    ExactOpaqueTransportResponse,
    GuardedDispatcher,
    SubmissionResponseBinding,
    _SUBMISSION_RESPONSE_BINDING_TOKEN,
    load_submission_response_binding,
    submission_attempt_aggregate_id,
)
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.autotrade_mvp.provider_core import (
    ProviderCoreError,
    observe_submission_json_response,
)
from mvp.autotrade_mvp.provider_transport import (
    TradingWireResponse,
    _binance_exact_trading_response,
)


class OpaqueProviderResponseEvidenceTests(unittest.TestCase):
    def test_parseable_binance_5xx_stays_exact_json_but_requires_reconciliation(self):
        raw = b'{"code":-1000,"msg":"provider unavailable"}'
        response = _binance_exact_trading_response(
            TradingWireResponse(http_status=503, body=raw)
        )
        self.assertIs(type(response), ExactJsonTransportResponse)
        self.assertEqual(response.response_bytes, raw)
        self.assertEqual(response.http_status, 503)
        self.assertTrue(response.requires_reconciliation)
        self.assertEqual(
            response.ambiguity_reason,
            "binance_spot_http_5xx_execution_unknown",
        )

    def test_binance_5xx_non_json_or_empty_is_exact_reconciliation_evidence(self):
        for raw in (b"", b"<html>upstream failure</html>", b"\xff\xfe"):
            with self.subTest(raw=raw):
                response = _binance_exact_trading_response(
                    TradingWireResponse(http_status=503, body=raw)
                )
                self.assertIs(type(response), ExactOpaqueTransportResponse)
                self.assertEqual(response.response_bytes, raw)
                self.assertEqual(response.http_status, 503)
                self.assertEqual(
                    response.response_sha256,
                    "sha256:" + sha256(raw).hexdigest(),
                )
                self.assertEqual(
                    response.ambiguity_reason,
                    "binance_spot_http_5xx_execution_unknown",
                )

    def test_opaque_5xx_is_durable_unknown_restart_safe_and_not_json_authority(self):
        for raw in (b"", b"<html>temporarily unavailable</html>", b"\xff\xfe"):
            with self.subTest(raw=raw), TemporaryDirectory() as directory:
                store = JournalStore(directory + "/journal.sqlite3")
                dispatcher = GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="owner",
                )
                response = _binance_exact_trading_response(
                    TradingWireResponse(http_status=503, body=raw)
                )
                sends = []

                def send(_client_order_id, _request, final_guard):
                    final_guard()
                    sends.append(raw)
                    return response

                args = dict(
                    attempt_id="opaque-503-" + sha256(raw).hexdigest()[:12],
                    intent_id="intent-opaque-503-" + sha256(raw).hexdigest()[:12],
                    intent_hash="opaque-503-financial-intent",
                    provider="BINANCE",
                    request={"symbol": "BTCUSDT", "side": "BUY", "quantity": "1"},
                    now="2026-09-30T17:45:00Z",
                    authority_check=lambda _hash, _now: (True, "allowed"),
                    submission_scope={},
                )
                outcome = dispatcher.dispatch(**args, transport_send=send)
                self.assertEqual(outcome.status, "UNKNOWN")
                self.assertIsNone(outcome.response)
                self.assertEqual(sends, [raw])

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
                terminal = events[-1]["payload"]
                self.assertEqual(terminal["response_encoding"], "base64")
                self.assertEqual(
                    terminal["response_sha256"],
                    "sha256:" + sha256(raw).hexdigest(),
                )
                self.assertEqual(terminal["http_status"], 503)
                self.assertEqual(
                    terminal["reason"],
                    "binance_spot_http_5xx_execution_unknown",
                )
                self.assertEqual(terminal["retry_disposition"], "RECONCILE_FIRST")

                binding = load_submission_response_binding(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    attempt_id=args["attempt_id"],
                )
                self.assertEqual(binding.response_encoding, "base64")
                self.assertEqual(binding.response_bytes, raw)
                self.assertEqual(binding.http_status, 503)

                with self.assertRaisesRegex(
                    ProviderCoreError,
                    "requires durable utf-8-json",
                ):
                    observe_submission_json_response(
                        response_binding=binding,
                        provider_id="BINANCE",
                        endpoint="/api/v3/order",
                        prepared_request_sha256=binding.request_hash,
                        capability_snapshot_ids=("cap-1",),
                        instrument_versions=("instrument-v1",),
                    )

                restarted = GuardedDispatcher(
                    JournalStore(directory + "/journal.sqlite3"),
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="owner",
                )
                repeated = restarted.dispatch(
                    **args,
                    transport_send=lambda *_: self.fail("blind provider retry"),
                )
                self.assertEqual(repeated.status, "UNKNOWN")
                self.assertEqual(sends, [raw])

    def test_opaque_binding_digest_tamper_fails_closed(self):
        raw = b"opaque-provider-error"
        scope = {}
        scope_hash = "sha256:" + sha256(canonical_json(scope).encode("utf-8")).hexdigest()
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            SubmissionResponseBinding(
                attempt_id="attempt-tamper",
                aggregate_id="submission-attempt:tamper",
                provider="BINANCE",
                request_hash="sha256:" + "1" * 64,
                client_order_id="at-tamper",
                environment="SIMULATION",
                account_id="acct",
                prepared_at="2026-09-30T17:45:00Z",
                sent_at="2026-09-30T17:45:01Z",
                submission_scope=scope,
                submission_scope_hash=scope_hash,
                response_bytes=raw,
                response_sha256="sha256:" + sha256(raw + b"!").hexdigest(),
                response_encoding="base64",
                http_status=503,
                _factory_token=_SUBMISSION_RESPONSE_BINDING_TOKEN,
            )


if __name__ == "__main__":
    unittest.main()
