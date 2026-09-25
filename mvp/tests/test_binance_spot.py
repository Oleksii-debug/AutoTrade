from functools import partial
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.binance_spot import (
    BinanceSpotAdapterError,
    BinanceSpotOrderIntent,
    coverage_evidence,
    parse_account_trades,
    parse_order_ack,
    prepare_order_request,
)
from mvp.autotrade_mvp.capabilities import CapabilitySnapshot


NOW = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def capability(*, order_types=("LIMIT", "MARKET"), tif=("GTC", "IOC", "FOK", "NONE")):
    evidence = {
        "artifact_id": str(uuid4()),
        "sha256": "sha256:" + "b" * 64,
        "observed_at": "2026-09-24T19:00:00Z",
        "source_uri": "https://developers.binance.com/en/docs/products/spot",
    }
    return CapabilitySnapshot(
        snapshot_id=str(uuid4()),
        provider_id="BINANCE",
        account_id="account-1",
        entity_id="global",
        environment="PAPER",
        instrument_version="BTCUSDT:v1",
        observed_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=1),
        supported_order_types=frozenset(order_types),
        time_in_force=frozenset(tif),
        permission_scopes=frozenset({"ORDER_WRITE"}),
        position_mode="NET",
        native_protection=frozenset(),
        rate_limit_policy_id="binance-spot-foundation",
        data_entitlements=frozenset({"ORDERS", "TRADES"}),
        evidence=(evidence,),
        status="VERIFIED",
        sources=frozenset({"DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT"}),
    )



# Bind only test fixtures; production APIs require explicit account/environment scope.
parse_account_trades = partial(parse_account_trades, account_id="acct-binance-spot", environment="PAPER")
coverage_evidence = partial(coverage_evidence, account_id="acct-binance-spot", environment="PAPER")

class BinanceSpotFoundationTests(unittest.TestCase):
    def test_limit_request_preserves_exact_strings_and_requests_ack_only(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="0.0100",
            price="40000.2500",
            time_in_force="GTC",
        )
        request = prepare_order_request(
            intent,
            client_order_id="at-order-1",
            capability=capability(),
            at=NOW,
        )
        self.assertEqual(request.endpoint, "/api/v3/order")
        self.assertEqual(request.body["quantity"], "0.0100")
        self.assertEqual(request.body["price"], "40000.2500")
        self.assertEqual(request.body["newOrderRespType"], "ACK")
        self.assertNotIn("timestamp", request.body)
        self.assertNotIn("signature", request.body)

    def test_market_uses_base_quantity_and_rejects_price_or_time_in_force(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="SELL",
            order_type="MARKET",
            quantity="0.5",
        )
        request = prepare_order_request(
            intent,
            client_order_id="at-market-1",
            capability=capability(),
            at=NOW,
        )
        self.assertEqual(request.body["quantity"], "0.5")
        self.assertNotIn("quoteOrderQty", request.body)
        self.assertNotIn("timeInForce", request.body)
        with self.assertRaises(BinanceSpotAdapterError):
            BinanceSpotOrderIntent.create(
                instrument_version="BTCUSDT:v1",
                symbol="BTCUSDT",
                side="SELL",
                order_type="MARKET",
                quantity="0.5",
                price="40000",
            )

    def test_binary_float_economics_fail_closed(self):
        with self.assertRaises(BinanceSpotAdapterError):
            BinanceSpotOrderIntent.create(
                instrument_version="BTCUSDT:v1",
                symbol="BTCUSDT",
                side="BUY",
                order_type="MARKET",
                quantity=0.1,
            )

    def test_exact_capability_evidence_controls_admission(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="1",
            price="100",
        )
        with self.assertRaisesRegex(BinanceSpotAdapterError, "capability"):
            prepare_order_request(
                intent,
                client_order_id="at-order-2",
                capability=capability(order_types=("MARKET",)),
                at=NOW,
            )

    def test_ack_is_never_promoted_to_fill(self):
        result = parse_order_ack(
            attempt_id=str(uuid4()),
            client_order_id="at-ack-1",
            response={
                "symbol": "BTCUSDT",
                "orderId": 42,
                "orderListId": -1,
                "clientOrderId": "at-ack-1",
                "transactTime": 1790272800123,
            },
        )
        self.assertEqual(result["outcome"], "ACKNOWLEDGED")
        self.assertEqual(result["retry_disposition"], "NEVER")
        self.assertNotIn("fill", result)
        self.assertNotIn("executed_quantity", result)

    def test_account_trade_id_is_economic_identity_and_duplicates_are_idempotent(self):
        rows = [
            {
                "symbol": "BTCUSDT",
                "id": 7,
                "orderId": 42,
                "price": "100.25",
                "qty": "0.2",
                "commission": "0.001",
                "commissionAsset": "BNB",
                "time": 1790272800123,
            }
        ]
        fills = parse_account_trades(
            [rows[0], dict(rows[0])],
            instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
            client_ids_by_order_id={42: "at-ack-1"},
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].provider_execution_id, "BINANCE-SPOT:BTCUSDT:7")
        self.assertEqual(fills[0].client_order_id, "at-ack-1")
        self.assertEqual(fills[0].quantity, Decimal("0.2"))
        self.assertEqual(fills[0].fee_currency, "BNB")

    def test_conflicting_duplicate_trade_id_fails_closed(self):
        first = {
            "symbol": "BTCUSDT",
            "id": 7,
            "orderId": 42,
            "price": "100.25",
            "qty": "0.2",
            "commission": "0.001",
            "commissionAsset": "BNB",
            "time": 1790272800123,
        }
        changed = dict(first, qty="0.3")
        with self.assertRaisesRegex(BinanceSpotAdapterError, "conflicting"):
            parse_account_trades(
                [first, changed],
                instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
            )

    def test_absence_semantics_are_never_assumed_from_empty_surface(self):
        evidence = coverage_evidence(
            surface="ORDER_HISTORY",
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
        )
        self.assertFalse(evidence.provider_semantics_exclude_execution)
        qualified = coverage_evidence(
            surface="ORDER_HISTORY",
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
            qualified_exclusion_semantics=True,
        )
        self.assertTrue(qualified.provider_semantics_exclude_execution)


if __name__ == "__main__":
    unittest.main()
