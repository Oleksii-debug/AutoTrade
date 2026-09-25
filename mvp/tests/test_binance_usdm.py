from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.binance_usdm import (
    BinanceUsdmAdapterError,
    BinanceUsdmOrderIntent,
    coverage_evidence,
    parse_account_trades,
    parse_order_ack,
    prepare_order_request,
)
from mvp.autotrade_mvp.capabilities import CapabilitySnapshot


NOW = datetime(2026, 9, 25, 0, tzinfo=timezone.utc)


def capability(
    *,
    position_mode="NET",
    order_types=("LIMIT", "MARKET"),
    tif=("GTC", "IOC", "FOK", "NONE"),
    provider_id="BINANCE",
    instrument_version="BTCUSDT-PERP:v1",
    status="VERIFIED",
):
    evidence = {
        "artifact_id": str(uuid4()),
        "sha256": "sha256:" + "c" * 64,
        "observed_at": "2026-09-24T23:00:00Z",
        "source_uri": (
            "https://developers.binance.com/en/docs/catalog/"
            "core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade"
        ),
    }
    return CapabilitySnapshot(
        snapshot_id=str(uuid4()),
        provider_id=provider_id,
        account_id="account-1",
        entity_id="global",
        environment="PAPER",
        instrument_version=instrument_version,
        observed_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=1),
        supported_order_types=frozenset(order_types),
        time_in_force=frozenset(tif),
        permission_scopes=frozenset({"ORDER_WRITE"}),
        position_mode=position_mode,
        native_protection=frozenset(),
        rate_limit_policy_id="binance-usdm-foundation",
        data_entitlements=frozenset({"ORDERS", "TRADES"}),
        evidence=(evidence,),
        status=status,
        sources=frozenset({"DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT"}),
    )


class BinanceUsdmFoundationTests(unittest.TestCase):
    def test_net_limit_preserves_exact_strings_and_ack_only(self):
        intent = BinanceUsdmOrderIntent.create(
            instrument_version="BTCUSDT-PERP:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="0.0100",
            price="40000.2500",
            time_in_force="GTC",
            position_side="BOTH",
        )
        request = prepare_order_request(
            intent,
            client_order_id="at-usdm-1",
            capability=capability(),
            at=NOW,
        )
        self.assertEqual(request.endpoint, "/fapi/v1/order")
        self.assertEqual(request.body["quantity"], "0.0100")
        self.assertEqual(request.body["price"], "40000.2500")
        self.assertEqual(request.body["newOrderRespType"], "ACK")
        self.assertEqual(request.body["positionSide"], "BOTH")
        self.assertNotIn("timestamp", request.body)
        self.assertNotIn("signature", request.body)
        self.assertNotIn("reduceOnly", request.body)

    def test_market_has_no_price_or_time_in_force(self):
        intent = BinanceUsdmOrderIntent.create(
            instrument_version="BTCUSDT-PERP:v1",
            symbol="BTCUSDT",
            side="SELL",
            order_type="MARKET",
            quantity="0.5",
        )
        request = prepare_order_request(
            intent,
            client_order_id="at-usdm-market",
            capability=capability(),
            at=NOW,
        )
        self.assertEqual(request.body["quantity"], "0.5")
        self.assertNotIn("price", request.body)
        self.assertNotIn("timeInForce", request.body)

        with self.assertRaises(BinanceUsdmAdapterError):
            BinanceUsdmOrderIntent.create(
                instrument_version="BTCUSDT-PERP:v1",
                symbol="BTCUSDT",
                side="SELL",
                order_type="MARKET",
                quantity="0.5",
                price="40000",
            )

    def test_binary_float_and_non_boolean_reduce_only_fail_closed(self):
        with self.assertRaises(BinanceUsdmAdapterError):
            BinanceUsdmOrderIntent.create(
                instrument_version="BTCUSDT-PERP:v1",
                symbol="BTCUSDT",
                side="BUY",
                order_type="MARKET",
                quantity=0.1,
            )
        with self.assertRaises(BinanceUsdmAdapterError):
            BinanceUsdmOrderIntent.create(
                instrument_version="BTCUSDT-PERP:v1",
                symbol="BTCUSDT",
                side="BUY",
                order_type="MARKET",
                quantity="0.1",
                reduce_only=1,
            )

    def test_net_mode_requires_both_position_side(self):
        intent = BinanceUsdmOrderIntent.create(
            instrument_version="BTCUSDT-PERP:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.1",
            position_side="LONG",
        )
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "NET"):
            prepare_order_request(
                intent,
                client_order_id="at-usdm-net",
                capability=capability(position_mode="NET"),
                at=NOW,
            )

    def test_hedge_mode_requires_long_or_short_and_forbids_reduce_only(self):
        both = BinanceUsdmOrderIntent.create(
            instrument_version="BTCUSDT-PERP:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.1",
            position_side="BOTH",
        )
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "hedge"):
            prepare_order_request(
                both,
                client_order_id="at-usdm-hedge-both",
                capability=capability(position_mode="HEDGE"),
                at=NOW,
            )

        reduce = BinanceUsdmOrderIntent.create(
            instrument_version="BTCUSDT-PERP:v1",
            symbol="BTCUSDT",
            side="SELL",
            order_type="MARKET",
            quantity="0.1",
            position_side="LONG",
            reduce_only=True,
        )
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "reduceOnly"):
            prepare_order_request(
                reduce,
                client_order_id="at-usdm-hedge-reduce",
                capability=capability(position_mode="HEDGE"),
                at=NOW,
            )

        valid = BinanceUsdmOrderIntent.create(
            instrument_version="BTCUSDT-PERP:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="0.1",
            price="30000",
            position_side="LONG",
        )
        request = prepare_order_request(
            valid,
            client_order_id="at-usdm-hedge",
            capability=capability(position_mode="HEDGE"),
            at=NOW,
        )
        self.assertEqual(request.body["positionSide"], "LONG")

    def test_net_reduce_only_is_explicit_string_true(self):
        intent = BinanceUsdmOrderIntent.create(
            instrument_version="BTCUSDT-PERP:v1",
            symbol="BTCUSDT",
            side="SELL",
            order_type="MARKET",
            quantity="0.1",
            position_side="BOTH",
            reduce_only=True,
        )
        request = prepare_order_request(
            intent,
            client_order_id="at-usdm-reduce",
            capability=capability(position_mode="NET"),
            at=NOW,
        )
        self.assertEqual(request.body["reduceOnly"], "true")

    def test_capability_identity_and_admission_fail_closed(self):
        intent = BinanceUsdmOrderIntent.create(
            instrument_version="BTCUSDT-PERP:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="1",
            price="100",
        )
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "another provider"):
            prepare_order_request(
                intent,
                client_order_id="at-usdm-provider",
                capability=capability(provider_id="KRAKEN"),
                at=NOW,
            )
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "instrument version"):
            prepare_order_request(
                intent,
                client_order_id="at-usdm-version",
                capability=capability(instrument_version="ETHUSDT-PERP:v1"),
                at=NOW,
            )
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "capability"):
            prepare_order_request(
                intent,
                client_order_id="at-usdm-status",
                capability=capability(status="UNKNOWN"),
                at=NOW,
            )

    def test_ack_never_promotes_executed_qty_to_fill(self):
        result = parse_order_ack(
            attempt_id=str(uuid4()),
            client_order_id="at-usdm-ack",
            response={
                "symbol": "BTCUSDT",
                "orderId": 22542179,
                "clientOrderId": "at-usdm-ack",
                "executedQty": "10",
                "cumQty": "10",
                "status": "FILLED",
                "updateTime": 1790272800123,
            },
        )
        self.assertEqual(result["outcome"], "ACKNOWLEDGED")
        self.assertEqual(result["retry_disposition"], "NEVER")
        self.assertNotIn("fill", result)
        self.assertNotIn("executed_quantity", result)
        self.assertEqual(
            result["provider_order_id"],
            "BINANCE-USDM:BTCUSDT:22542179",
        )

    def test_provider_observation_symbol_identity_must_be_canonical_uppercase(self):
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "uppercase"):
            parse_order_ack(
                attempt_id=str(uuid4()),
                client_order_id="at-usdm-lower-ack",
                response={
                    "symbol": "btcusdt",
                    "orderId": 7,
                    "clientOrderId": "at-usdm-lower-ack",
                    "updateTime": 1790272800123,
                },
            )

        with self.assertRaisesRegex(BinanceUsdmAdapterError, "uppercase"):
            parse_account_trades(
                [
                    {
                        "commission": "0.01",
                        "commissionAsset": "USDT",
                        "id": 7,
                        "orderId": 42,
                        "price": "100",
                        "qty": "0.2",
                        "positionSide": "BOTH",
                        "symbol": "btcusdt",
                        "time": 1790272800123,
                    }
                ],
                instrument_versions={"btcusdt": "BTCUSDT-PERP:v1"},
            )

    def test_trade_identity_dedupes_and_preserves_exact_fee(self):
        row = {
            "commission": "0.07819010",
            "commissionAsset": "USDT",
            "id": 698759,
            "orderId": 25851813,
            "price": "7819.01",
            "qty": "0.002",
            "realizedPnl": "-0.91539999",
            "side": "SELL",
            "positionSide": "SHORT",
            "symbol": "BTCUSDT",
            "time": 1569514978020,
        }
        fills = parse_account_trades(
            [row, dict(row)],
            instrument_versions={"BTCUSDT": "BTCUSDT-PERP:v1"},
            client_ids_by_order_id={25851813: "at-usdm-fill"},
        )
        self.assertEqual(len(fills), 1)
        fill = fills[0]
        self.assertEqual(fill.provider_execution_id, "BINANCE-USDM:BTCUSDT:698759")
        self.assertEqual(fill.client_order_id, "at-usdm-fill")
        self.assertEqual(fill.quantity, Decimal("0.002"))
        self.assertEqual(fill.price, Decimal("7819.01"))
        self.assertEqual(fill.fee_amount, Decimal("0.07819010"))
        self.assertEqual(fill.fee_currency, "USDT")

    def test_client_order_identity_map_is_validated_before_fill_mapping(self):
        row = {
            "commission": "0.01",
            "commissionAsset": "USDT",
            "id": 7,
            "orderId": 42,
            "price": "100",
            "qty": "0.2",
            "positionSide": "BOTH",
            "symbol": "BTCUSDT",
            "time": 1790272800123,
        }
        for bad_map in (
            {"42": "at-usdm-fill"},
            {True: "at-usdm-fill"},
            {-1: "at-usdm-fill"},
        ):
            with self.subTest(bad_map=bad_map), self.assertRaisesRegex(
                BinanceUsdmAdapterError,
                "non-negative integer order ids",
            ):
                parse_account_trades(
                    [row],
                    instrument_versions={"BTCUSDT": "BTCUSDT-PERP:v1"},
                    client_ids_by_order_id=bad_map,
                )

        with self.assertRaisesRegex(
            BinanceUsdmAdapterError,
            "client_order_id",
        ):
            parse_account_trades(
                [row],
                instrument_versions={"BTCUSDT": "BTCUSDT-PERP:v1"},
                client_ids_by_order_id={42: "bad client id with spaces"},
            )

    def test_conflicting_trade_identity_fails_closed(self):
        first = {
            "commission": "0.01",
            "commissionAsset": "USDT",
            "id": 7,
            "orderId": 42,
            "price": "100",
            "qty": "0.2",
            "positionSide": "BOTH",
            "symbol": "BTCUSDT",
            "time": 1790272800123,
        }
        changed = dict(first, qty="0.3")
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "conflicting"):
            parse_account_trades(
                [first, changed],
                instrument_versions={"BTCUSDT": "BTCUSDT-PERP:v1"},
            )

    def test_empty_order_history_never_proves_absence_by_default(self):
        evidence = coverage_evidence(
            surface="ORDER_HISTORY",
            coverage_start="2026-09-24T20:00:00Z",
            coverage_end="2026-09-25T00:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
        )
        self.assertFalse(evidence.provider_semantics_exclude_execution)

        qualified = coverage_evidence(
            surface="ORDER_HISTORY",
            coverage_start="2026-09-24T20:00:00Z",
            coverage_end="2026-09-25T00:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
            qualified_exclusion_semantics=True,
        )
        self.assertTrue(qualified.provider_semantics_exclude_execution)

    def test_bad_position_mode_and_bad_trade_position_side_fail_closed(self):
        intent = BinanceUsdmOrderIntent.create(
            instrument_version="BTCUSDT-PERP:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="1",
        )
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "position mode"):
            prepare_order_request(
                intent,
                client_order_id="at-usdm-mode",
                capability=capability(position_mode="CONFLICTED"),
                at=NOW,
            )

        with self.assertRaisesRegex(BinanceUsdmAdapterError, "positionSide"):
            parse_account_trades(
                [
                    {
                        "commission": "0.01",
                        "commissionAsset": "USDT",
                        "id": 1,
                        "orderId": 2,
                        "price": "100",
                        "qty": "0.1",
                        "positionSide": "INVALID",
                        "symbol": "BTCUSDT",
                        "time": 1790272800123,
                    }
                ],
                instrument_versions={"BTCUSDT": "BTCUSDT-PERP:v1"},
            )


if __name__ == "__main__":
    unittest.main()
