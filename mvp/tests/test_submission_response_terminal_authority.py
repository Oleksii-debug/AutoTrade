"""Durable terminal-state authority for exact provider response bindings."""

from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    load_submission_response_binding,
)
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.autotrade_mvp.provider_core import (
    ProviderCoreError,
    observe_submission_json_response,
)


class SubmissionResponseTerminalAuthorityTests(unittest.TestCase):
    @staticmethod
    def _request_and_scope():
        request = {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": "1",
        }
        request_hash = (
            "sha256:"
            + sha256(canonical_json(request).encode("utf-8")).hexdigest()
        )
        scope = {
            "endpoint": "/v5/order/create",
            "prepared_request_sha256": request_hash,
            "capability_snapshot_ids": ["cap-current"],
            "instrument_versions": ["instrument-current"],
        }
        return request, request_hash, scope

    @staticmethod
    def _dispatch(directory, *, response, attempt_id, intent_id):
        request, request_hash, scope = (
            SubmissionResponseTerminalAuthorityTests._request_and_scope()
        )
        store = JournalStore(directory + "/journal.sqlite3")
        dispatcher = GuardedDispatcher(
            store,
            environment="SIMULATION",
            account_id="acct",
            owner_token="owner",
        )

        def send(_client_order_id, _request, final_guard):
            final_guard()
            return response

        outcome = dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id=intent_id,
            intent_hash="financial-intent-" + intent_id,
            provider="BYBIT",
            request=request,
            now="2026-10-06T00:15:00Z",
            authority_check=lambda _hash, _now: (True, "allowed"),
            transport_send=send,
            submission_scope=scope,
        )
        binding = load_submission_response_binding(
            store,
            environment="SIMULATION",
            account_id="acct",
            attempt_id=attempt_id,
        )
        return outcome, binding, request_hash

    @staticmethod
    def _observe(binding, request_hash):
        return observe_submission_json_response(
            response_binding=binding,
            provider_id="BYBIT",
            endpoint="/v5/order/create",
            prepared_request_sha256=request_hash,
            capability_snapshot_ids=("cap-current",),
            instrument_versions=("instrument-current",),
        )

    def test_ambiguous_valid_json_binding_stays_unknown_and_cannot_be_observed(self):
        raw = b'{"retCode":10016,"retMsg":"server error","result":{}}'
        response = ExactJsonTransportResponse(
            raw,
            http_status=503,
            requires_reconciliation=True,
            ambiguity_reason="bybit_http_5xx_execution_unknown",
        )
        with TemporaryDirectory() as directory:
            outcome, binding, request_hash = self._dispatch(
                directory,
                response=response,
                attempt_id="attempt-json-503",
                intent_id="intent-json-503",
            )

            self.assertEqual(outcome.status, "UNKNOWN")
            self.assertEqual(binding.response_bytes, raw)
            self.assertEqual(binding.response_encoding, "utf-8-json")
            self.assertEqual(binding.terminal_state, "UNKNOWN")
            self.assertEqual(
                binding.ambiguity_reason,
                "bybit_http_5xx_execution_unknown",
            )
            self.assertEqual(
                binding.retry_disposition,
                "RECONCILE_FIRST",
            )
            with self.assertRaisesRegex(
                ProviderCoreError,
                "requires definitive SENT response",
            ):
                self._observe(binding, request_hash)

    def test_ambiguous_non_json_hex_binding_cannot_be_observed(self):
        raw = b"<html>gateway unavailable</html>"
        response = ExactJsonTransportResponse(
            raw,
            http_status=503,
            requires_reconciliation=True,
            ambiguity_reason="bybit_http_5xx_execution_unknown",
        )
        with TemporaryDirectory() as directory:
            outcome, binding, request_hash = self._dispatch(
                directory,
                response=response,
                attempt_id="attempt-opaque-503",
                intent_id="intent-opaque-503",
            )

            self.assertEqual(outcome.status, "UNKNOWN")
            self.assertEqual(binding.response_bytes, raw)
            self.assertEqual(binding.response_encoding, "hex")
            self.assertEqual(binding.terminal_state, "UNKNOWN")
            with self.assertRaisesRegex(
                ProviderCoreError,
                "requires definitive SENT response",
            ):
                self._observe(binding, request_hash)

    def test_definitive_json_binding_remains_observation_eligible(self):
        raw = b'{"retCode":0,"retMsg":"OK","result":{"orderId":"provider-1"}}'
        response = ExactJsonTransportResponse(raw, http_status=200)
        with TemporaryDirectory() as directory:
            outcome, binding, request_hash = self._dispatch(
                directory,
                response=response,
                attempt_id="attempt-sent-200",
                intent_id="intent-sent-200",
            )

            self.assertEqual(outcome.status, "SENT")
            self.assertEqual(binding.response_encoding, "utf-8-json")
            self.assertEqual(binding.terminal_state, "SENT")
            observation = self._observe(binding, request_hash)
            self.assertEqual(observation.response_binding, binding)
            self.assertEqual(observation.payload["retCode"], 0)

    def test_registered_binding_rejects_terminal_state_mutation(self):
        raw = b'{"retCode":0,"retMsg":"OK","result":{}}'
        response = ExactJsonTransportResponse(raw, http_status=200)
        with TemporaryDirectory() as directory:
            _outcome, binding, request_hash = self._dispatch(
                directory,
                response=response,
                attempt_id="attempt-tamper",
                intent_id="intent-tamper",
            )
            object.__setattr__(binding, "terminal_state", "UNKNOWN")

            with self.assertRaisesRegex(
                ValueError,
                "submission response binding authority is unavailable",
            ):
                self._observe(binding, request_hash)


if __name__ == "__main__":
    unittest.main()
