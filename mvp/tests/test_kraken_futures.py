from datetime import datetime, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.kraken_futures import (
    build_order_payload,
    coverage_evidence,
    futures_base_url,
    parse_position_executions,
    parse_submission_response,
)
from mvp.autotrade_mvp.provider_core import ProviderCoreError


NOW = "2026-09-24T20:00:00Z"


class KrakenFuturesAdapterTests(unittest.TestCase):
    def test_live_and_demo_services_are_explicit(self):
        self.assertEqual(futures_base_url("LIVE"), "https://futures.kraken.com")
        self.assertEqual(futures_base_url("DEMO"), "https://demo-futures.kraken.com")
        with self.assertRaises(ProviderCoreError):
            futures_base_url("SPOT-TESTNET")

    def test_limit_payload_preserves_exact_values_and_reduce_only(self):
        payload = build_order_payload(
            environment="DEMO",
            symbol="PI_XBTUSD",
            side="SELL",
            order_type="LIMIT",
            size=Decimal("2.00"),
            price="70000.00",
            client_order_id="hedge-001",
            reduce_only=True,
        )
        self.assertEqual(payload, {
            "orderType": "lmt",
            "symbol": "PI_XBTUSD",
            "side": "sell",
            "size": "2",
            "cliOrdId": "hedge-001",
            "limitPrice": "70000",
            "reduceOnly": "true",
        })

    def test_unsafe_payload_shapes_fail_closed(self):
        with self.assertRaisesRegex(ProviderCoreError, "ignored"):
            build_order_payload(
                environment="LIVE", symbol="PI_XBTUSD", side="BUY",
                order_type="MARKET", size="1", price="10",
                client_order_id="hedge-002",
            )
        with self.assertRaisesRegex(ProviderCoreError, "exact decimal"):
            build_order_payload(
                environment="LIVE", symbol="PI_XBTUSD", side="BUY",
                order_type="MARKET", size=0.1,
                client_order_id="hedge-003",
            )
        with self.assertRaisesRegex(ProviderCoreError, "client_order_id"):
            build_order_payload(
                environment="LIVE", symbol="PI_XBTUSD", side="BUY",
                order_type="MARKET", size="1",
                client_order_id="this-client-identity-is-not-qualified",
            )

    def test_success_is_acknowledgement_not_fill(self):
        for send_status in (
            {"order_id": "provider-order-1", "status": "placed"},
            '{"order_id":"provider-order-1","status":"placed"}',
        ):
            with self.subTest(send_status=send_status):
                result = parse_submission_response(
                    attempt_id=str(uuid4()),
                    client_order_id="hedge-004",
                    environment="LIVE",
                    observed_at=NOW,
                    response={"result": "success", "sendStatus": send_status},
                )
                self.assertEqual(result["outcome"], "ACKNOWLEDGED")
                self.assertEqual(result["provider_order_id"], "provider-order-1")
                self.assertEqual(result["retry_disposition"], "NEVER")
                self.assertNotIn("fill", repr(result).lower())

    def test_transport_ambiguity_is_unknown_and_never_blind_retried(self):
        result = parse_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="hedge-005",
            environment="DEMO",
            observed_at=NOW,
            response=None,
            transport_ambiguous=True,
        )
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertEqual(result["retry_disposition"], "RECONCILE_FIRST")
        self.assertEqual(result["evidence"], [])
        self.assertNotIn("provider_received_at", result)
        self.assertNotIn("observed_at", result)

    def test_explicit_provider_error_is_rejected(self):
        result = parse_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="hedge-006",
            environment="LIVE",
            observed_at=NOW,
            response={"result": "error", "error": "insufficientFunds"},
        )
        self.assertEqual(result["outcome"], "REJECTED")
        self.assertEqual(result["retry_disposition"], "NEVER")

    def test_position_history_maps_only_trade_execution_facts(self):
        fills = parse_position_executions(
            {
                "elements": [
                    {
                        "tradeable": "PI_XBTUSD",
                        "fillTime": 1790280000123,
                        "fee": "1.25",
                        "feeCurrency": "USD",
                        "executionUid": "exec-1",
                        "executionPrice": "65000.10",
                        "executionSize": "0.25",
                        "timestamp": 1790280000123,
                        "updateReason": "trade",
                    },
                    {
                        "tradeable": "PI_XBTUSD",
                        "timestamp": 1790280000456,
                        "updateReason": "fundingRealisation",
                        "realizedFunding": "-0.10",
                    },
                ]
            },
            instrument_versions={"PI_XBTUSD": "PI_XBTUSD@v1"},
            execution_client_ids={"exec-1": "hedge-007"},
        )
        self.assertEqual(len(fills), 1)
        fill = fills[0]
        self.assertEqual(fill.provider_execution_id, "exec-1")
        self.assertEqual(fill.client_order_id, "hedge-007")
        self.assertEqual(fill.quantity, Decimal("0.25"))
        self.assertEqual(fill.price, Decimal("65000.10"))
        self.assertEqual(fill.fee_amount, Decimal("1.25"))
        self.assertEqual(fill.trade_time, "2026-09-24T20:00:00.123Z")

    def test_execution_fee_must_be_explicit_and_exact(self):
        base = {
            "tradeable": "PI_XBTUSD",
            "fillTime": 1790280000123,
            "feeCurrency": "USD",
            "executionUid": "fee-evidence",
            "executionPrice": "65000",
            "executionSize": "1",
            "timestamp": 1790280000123,
            "updateReason": "trade",
        }
        for missing in ({**base}, {**base, "fee": None}):
            with self.subTest(missing=missing.get("fee", "<absent>")):
                with self.assertRaisesRegex(ProviderCoreError, "fee evidence"):
                    parse_position_executions({"elements": [missing]}, instrument_versions={"PI_XBTUSD": "PI_XBTUSD@v1"})
        zero = parse_position_executions({"elements": [{**base, "fee": "0"}]}, instrument_versions={"PI_XBTUSD": "PI_XBTUSD@v1"})
        self.assertEqual(zero[0].fee_amount, Decimal("0"))
        with self.assertRaisesRegex(ProviderCoreError, "exact decimal"):
            parse_position_executions({"elements": [{**base, "fee": 0.0}]}, instrument_versions={"PI_XBTUSD": "PI_XBTUSD@v1"})

    def test_conflicting_duplicate_execution_id_fails_closed(self):
        base = {
            "tradeable": "PI_XBTUSD",
            "fillTime": 1790280000123,
            "fee": "1",
            "feeCurrency": "USD",
            "executionUid": "same",
            "executionPrice": "65000",
            "executionSize": "1",
            "timestamp": 1790280000123,
            "updateReason": "trade",
        }
        with self.assertRaisesRegex(ProviderCoreError, "conflicting"):
            parse_position_executions(
                {"elements": [base, {**base, "executionSize": "2"}]},
                instrument_versions={"PI_XBTUSD": "PI_XBTUSD@v1"},
            )

    def test_futures_foundation_cannot_self_assert_absence_semantics(self):
        with self.assertRaisesRegex(
            ProviderCoreError,
            "cannot self-assert provider exclusion semantics",
        ):
            coverage_evidence(
                surface="EXECUTIONS",
                coverage_start="2026-09-24T19:00:00Z",
                coverage_end="2026-09-24T21:00:00Z",
                pagination_complete=True,
                consistency_horizon_satisfied=True,
                qualified_exclusion_semantics=True,
            )

    def test_absence_semantics_default_fail_closed(self):
        evidence = coverage_evidence(
            surface="EXECUTIONS",
            coverage_start="2026-09-24T19:00:00Z",
            coverage_end="2026-09-24T21:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
        )
        self.assertFalse(evidence.provider_semantics_exclude_execution)
        self.assertFalse(
            evidence.proves_absence_for(datetime(2026, 9, 24, 20, tzinfo=timezone.utc))
        )


if __name__ == "__main__":
    unittest.main()
