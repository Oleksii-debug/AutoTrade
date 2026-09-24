from datetime import datetime, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.kraken import (
    build_futures_order_payload,
    build_spot_order_payload,
    coverage_evidence,
    futures_base_url,
    parse_futures_position_executions,
    parse_futures_submission_response,
    parse_spot_submission_response,
    parse_spot_trades,
    validate_spot_nonce,
)
from mvp.autotrade_mvp.provider_core import ProviderCoreError


NOW = "2026-09-24T20:00:00Z"


class KrakenAdapterTests(unittest.TestCase):
    def test_spot_and_futures_environments_remain_explicitly_distinct(self):
        self.assertEqual(
            futures_base_url("LIVE"), "https://futures.kraken.com"
        )
        self.assertEqual(
            futures_base_url("DEMO"), "https://demo-futures.kraken.com"
        )
        with self.assertRaises(ProviderCoreError):
            futures_base_url("TESTNET")

    def test_spot_nonce_must_strictly_increase(self):
        self.assertEqual(validate_spot_nonce(candidate=101, last_accepted=100), 101)
        with self.assertRaisesRegex(ProviderCoreError, "strictly increase"):
            validate_spot_nonce(candidate=100, last_accepted=100)
        with self.assertRaisesRegex(ProviderCoreError, "strictly increase"):
            validate_spot_nonce(candidate=99, last_accepted=100)

    def test_spot_limit_payload_preserves_exact_decimal_and_client_identity(self):
        payload = build_spot_order_payload(
            pair="XBTUSD",
            side="BUY",
            order_type="LIMIT",
            volume=Decimal("0.0100"),
            price="65000.1200",
            client_order_id="flow-0001",
            nonce=123456,
            time_in_force="GTC",
        )
        self.assertEqual(payload["type"], "buy")
        self.assertEqual(payload["ordertype"], "limit")
        self.assertEqual(payload["volume"], "0.01")
        self.assertEqual(payload["price"], "65000.12")
        self.assertEqual(payload["cl_ord_id"], "flow-0001")
        self.assertEqual(payload["nonce"], "123456")

    def test_market_payload_cannot_hide_ignored_limit_price(self):
        with self.assertRaisesRegex(ProviderCoreError, "ignored"):
            build_spot_order_payload(
                pair="XBTUSD",
                side="BUY",
                order_type="MARKET",
                volume="0.01",
                price="65000",
                client_order_id="flow-0002",
                nonce=123457,
            )

    def test_client_id_and_binary_float_inputs_fail_closed(self):
        with self.assertRaisesRegex(ProviderCoreError, "client_order_id"):
            build_spot_order_payload(
                pair="XBTUSD",
                side="BUY",
                order_type="MARKET",
                volume="0.01",
                client_order_id="this id is much too long for free text",
                nonce=123458,
            )
        with self.assertRaisesRegex(ProviderCoreError, "exact decimal"):
            build_spot_order_payload(
                pair="XBTUSD",
                side="BUY",
                order_type="MARKET",
                volume=0.01,
                client_order_id="flow-0003",
                nonce=123459,
            )

    def test_futures_order_maps_only_bounded_market_and_limit_semantics(self):
        payload = build_futures_order_payload(
            environment="DEMO",
            symbol="PI_XBTUSD",
            side="SELL",
            order_type="LIMIT",
            size="2.00",
            price="70000.00",
            client_order_id="hedge-001",
            reduce_only=True,
        )
        self.assertEqual(payload["orderType"], "lmt")
        self.assertEqual(payload["side"], "sell")
        self.assertEqual(payload["size"], "2")
        self.assertEqual(payload["limitPrice"], "70000")
        self.assertEqual(payload["cliOrdId"], "hedge-001")
        self.assertEqual(payload["reduceOnly"], "true")

    def test_spot_success_is_acknowledgement_not_fill(self):
        result = parse_spot_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="flow-0004",
            observed_at=NOW,
            response={
                "error": [],
                "result": {
                    "descr": {"order": "buy 0.01 XBTUSD @ limit 65000"},
                    "txid": ["OABC12-DEF345-GHI678"],
                },
            },
        )
        self.assertEqual(result["outcome"], "ACKNOWLEDGED")
        self.assertEqual(result["provider_order_id"], "OABC12-DEF345-GHI678")
        self.assertEqual(result["retry_disposition"], "NEVER")
        self.assertNotIn("fill", repr(result).lower())

    def test_spot_explicit_error_is_rejected_but_transport_ambiguity_is_unknown(self):
        rejected = parse_spot_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="flow-0005",
            observed_at=NOW,
            response={"error": ["EOrder:Insufficient funds"], "result": None},
        )
        self.assertEqual(rejected["outcome"], "REJECTED")
        self.assertEqual(rejected["retry_disposition"], "NEVER")

        unknown = parse_spot_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="flow-0006",
            observed_at=NOW,
            response=None,
            transport_ambiguous=True,
        )
        self.assertEqual(unknown["outcome"], "UNKNOWN")
        self.assertEqual(unknown["retry_disposition"], "RECONCILE_FIRST")
        self.assertEqual(unknown["evidence"], [])

    def test_futures_success_is_acknowledgement_and_accepts_documented_sendstatus_shapes(self):
        for send_status in (
            {"order_id": "fut-order-1", "status": "placed"},
            '{"order_id":"fut-order-1","status":"placed"}',
        ):
            with self.subTest(send_status=send_status):
                result = parse_futures_submission_response(
                    attempt_id=str(uuid4()),
                    client_order_id="hedge-002",
                    environment="LIVE",
                    observed_at=NOW,
                    response={
                        "result": "success",
                        "sendStatus": send_status,
                        "serverTime": NOW,
                    },
                )
                self.assertEqual(result["outcome"], "ACKNOWLEDGED")
                self.assertEqual(result["provider_order_id"], "fut-order-1")
                self.assertEqual(result["retry_disposition"], "NEVER")

    def test_futures_transport_ambiguity_requires_reconciliation(self):
        result = parse_futures_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="hedge-003",
            environment="DEMO",
            observed_at=NOW,
            response=None,
            transport_ambiguous=True,
        )
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertEqual(result["retry_disposition"], "RECONCILE_FIRST")

    def test_futures_position_history_preserves_execution_identity_and_economics(self):
        response = {
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
        }
        fills = parse_futures_position_executions(
            response,
            instrument_versions={"PI_XBTUSD": "PI_XBTUSD@v1"},
            execution_client_ids={"exec-1": "hedge-004"},
        )
        self.assertEqual(len(fills), 1)
        fill = fills[0]
        self.assertEqual(fill.provider_execution_id, "exec-1")
        self.assertEqual(fill.client_order_id, "hedge-004")
        self.assertEqual(fill.quantity, Decimal("0.25"))
        self.assertEqual(fill.price, Decimal("65000.10"))
        self.assertEqual(fill.fee_amount, Decimal("1.25"))
        self.assertEqual(fill.trade_time, "2026-09-24T20:00:00.123Z")

    def test_futures_duplicate_execution_conflict_fails_closed(self):
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
            parse_futures_position_executions(
                {"elements": [base, {**base, "executionSize": "2"}]},
                instrument_versions={"PI_XBTUSD": "PI_XBTUSD@v1"},
            )

    def test_spot_trade_history_requires_explicit_fee_currency_mapping(self):
        fills = parse_spot_trades(
            {
                "error": [],
                "result": {
                    "trades": {
                        "T1": {
                            "ordertxid": "O1",
                            "pair": "XBTUSD",
                            "time": "1790280000.125000",
                            "type": "buy",
                            "price": "65000.10",
                            "vol": "0.20",
                            "fee": "2.50",
                        }
                    }
                },
            },
            instrument_versions={"XBTUSD": ("XBTUSD@v1", "USD")},
            order_client_ids={"O1": "flow-0007"},
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].provider_execution_id, "T1")
        self.assertEqual(fills[0].client_order_id, "flow-0007")
        self.assertEqual(fills[0].fee_currency, "USD")
        self.assertEqual(fills[0].trade_time, "2026-09-24T20:00:00.125000Z")

    def test_spot_trade_timestamp_refuses_silent_sub_microsecond_truncation(self):
        response = {
            "error": [],
            "result": {
                "trades": {
                    "T-too-precise": {
                        "ordertxid": "O1",
                        "pair": "XBTUSD",
                        "time": "1790280000.1234567",
                        "price": "65000",
                        "vol": "0.1",
                        "fee": "1",
                    }
                }
            },
        }
        with self.assertRaisesRegex(ProviderCoreError, "finer than one microsecond"):
            parse_spot_trades(
                response,
                instrument_versions={"XBTUSD": ("XBTUSD@v1", "USD")},
            )

    def test_absence_evidence_defaults_fail_closed(self):
        evidence = coverage_evidence(
            surface="EXECUTIONS",
            coverage_start="2026-09-24T19:00:00Z",
            coverage_end="2026-09-24T21:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
        )
        self.assertFalse(evidence.provider_semantics_exclude_execution)
        self.assertFalse(
            evidence.proves_absence_for(
                datetime(2026, 9, 24, 20, tzinfo=timezone.utc)
            )
        )


if __name__ == "__main__":
    unittest.main()
