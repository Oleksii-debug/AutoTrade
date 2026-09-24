from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.binance_spot import (
    BinanceSpotAbsenceEvidence,
    BinanceSpotAdapterError,
    BinanceSpotFillEvidence,
    BinanceSpotOrderIntent,
    derivatives_supported_by_this_module,
    parse_ack_response,
    prepare_spot_order_request,
)
from mvp.autotrade_mvp.capabilities import CapabilitySnapshot


NOW = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def capability(*, order_types=("MARKET", "LIMIT"), tif=("GTC", "IOC", "FOK")):
    evidence = {
        "artifact_id": str(uuid4()),
        "sha256": "sha256:" + "c" * 64,
        "observed_at": "2026-09-24T19:00:00Z",
        "source_uri": "https://developers.binance.com/docs/binance-spot-api-docs/rest-api/trading-endpoints",
    }
    return CapabilitySnapshot(
        snapshot_id=str(uuid4()),
        provider_id="BINANCE",
        account_id="spot-account",
        entity_id="binance-spot",
        environment="PAPER",
        instrument_version="BTCUSDT:v1",
        observed_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=1),
        supported_order_types=frozenset(order_types),
        time_in_force=frozenset(tif),
        permission_scopes=frozenset({"ORDER_WRITE"}),
        position_mode="CASH",
        native_protection=frozenset(),
        rate_limit_policy_id="binance-spot-test",
        data_entitlements=frozenset({"ORDERS", "TRADES"}),
        evidence=(evidence,),
        status="VERIFIED",
        sources=frozenset({"DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT"}),
    )


class BinanceSpotAdapterTests(unittest.TestCase):
    def test_limit_order_preserves_decimal_strings_and_requests_ack(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="btcusdt",
            side="buy",
            order_type="limit",
            amount="0.0100",
            price="60000.25",
            time_in_force="GTC",
        )
        request = prepare_spot_order_request(
            intent,
            client_order_id="at-0123456789abcdef",
            capability=capability(),
            at=NOW,
        )
        self.assertEqual(request.endpoint, "/api/v3/order")
        self.assertEqual(request.body["quantity"], "0.0100")
        self.assertEqual(request.body["price"], "60000.25")
        self.assertEqual(request.body["newOrderRespType"], "ACK")
        self.assertNotIn("timestamp", request.body)
        self.assertNotIn("signature", request.body)

    def test_market_base_and_quote_units_are_not_confused(self):
        base = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="SELL",
            order_type="MARKET",
            amount="0.01",
            market_amount_unit="BASE",
        )
        quote = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            amount="100",
            market_amount_unit="QUOTE",
        )
        base_request = prepare_spot_order_request(
            base, client_order_id="at-base-1", capability=capability(), at=NOW
        )
        quote_request = prepare_spot_order_request(
            quote, client_order_id="at-quote-1", capability=capability(), at=NOW
        )
        self.assertEqual(base_request.body["quantity"], "0.01")
        self.assertNotIn("quoteOrderQty", base_request.body)
        self.assertEqual(quote_request.body["quoteOrderQty"], "100")
        self.assertNotIn("quantity", quote_request.body)

    def test_limit_cannot_use_quote_quantity(self):
        with self.assertRaisesRegex(BinanceSpotAdapterError, "BASE"):
            BinanceSpotOrderIntent.create(
                instrument_version="BTCUSDT:v1",
                symbol="BTCUSDT",
                side="BUY",
                order_type="LIMIT",
                amount="100",
                market_amount_unit="QUOTE",
                price="60000",
                time_in_force="GTC",
            )

    def test_binary_float_money_is_rejected(self):
        with self.assertRaises(BinanceSpotAdapterError):
            BinanceSpotOrderIntent.create(
                instrument_version="BTCUSDT:v1",
                symbol="BTCUSDT",
                side="BUY",
                order_type="MARKET",
                amount=100.0,
                market_amount_unit="QUOTE",
            )

    def test_capability_evidence_controls_admission(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            amount="0.01",
            price="60000",
            time_in_force="GTC",
        )
        with self.assertRaisesRegex(BinanceSpotAdapterError, "capability"):
            prepare_spot_order_request(
                intent,
                client_order_id="at-order-1",
                capability=capability(order_types=("MARKET",)),
                at=NOW,
            )

    def test_ack_response_does_not_prove_fill(self):
        ack = parse_ack_response(
            {
                "symbol": "BTCUSDT",
                "orderId": 28,
                "clientOrderId": "at-order-28",
                "transactTime": 1507725176595,
            }
        )
        self.assertEqual(ack.provider_order_id, "28")
        self.assertFalse(ack.proves_fill)

    def test_fill_evidence_keeps_trade_identity_and_third_currency_fee(self):
        fill = BinanceSpotFillEvidence.create(
            trade_id=56,
            price="60000.25",
            quantity="0.01",
            commission="-0.000001",
            commission_asset="BNB",
        )
        self.assertEqual(fill.trade_id, "56")
        self.assertEqual(fill.price, Decimal("60000.25"))
        self.assertEqual(fill.commission, Decimal("-0.000001"))
        self.assertEqual(fill.commission_asset, "BNB")

    def test_client_id_safe_subset_rejects_spaces_and_overlength(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="SELL",
            order_type="MARKET",
            amount="0.01",
        )
        for invalid in ("bad id", "a" * 33):
            with self.subTest(invalid=invalid):
                with self.assertRaises(BinanceSpotAdapterError):
                    prepare_spot_order_request(
                        intent, client_order_id=invalid, capability=capability(), at=NOW
                    )

    def test_partial_absence_evidence_remains_inconclusive(self):
        evidence = BinanceSpotAbsenceEvidence(
            query_order_complete=True,
            open_orders_complete=True,
            all_orders_complete=True,
            account_trades_complete=False,
            consistency_horizon_satisfied=True,
            order_found=False,
        )
        self.assertEqual(evidence.verdict(), "INCONCLUSIVE")

    def test_complete_order_and_trade_coverage_can_prove_absence(self):
        evidence = BinanceSpotAbsenceEvidence(
            query_order_complete=True,
            open_orders_complete=True,
            all_orders_complete=True,
            account_trades_complete=True,
            consistency_horizon_satisfied=True,
            order_found=False,
        )
        self.assertEqual(evidence.verdict(), "PROVEN_ABSENT")

    def test_derivatives_are_not_claimed_by_spot_module(self):
        self.assertFalse(derivatives_supported_by_this_module())


if __name__ == "__main__":
    unittest.main()
