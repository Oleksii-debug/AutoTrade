from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.capabilities import CapabilitySnapshot
from mvp.autotrade_mvp.whitebit import (
    WhiteBitAbsenceEvidence,
    WhiteBitAdapterError,
    WhiteBitOrderIntent,
    order_lookup_requests,
    parse_execution_deal,
    parse_execution_history,
    parse_order_snapshot,
    prepare_order_request,
    validate_client_order_id,
)


NOW = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def capability(*, order_types=("LIMIT", "MARKET", "STOP_MARKET", "STOP_LIMIT"), tif=("GTC", "IOC")):
    evidence = {
        "artifact_id": str(uuid4()),
        "sha256": "sha256:" + "a" * 64,
        "observed_at": "2026-09-24T19:00:00Z",
        "source_uri": "https://docs.whitebit.com/concepts/order-types",
    }
    return CapabilitySnapshot(
        snapshot_id=str(uuid4()),
        provider_id="WHITEBIT",
        account_id="account-1",
        entity_id="global",
        environment="PAPER",
        instrument_version="BTC_USDT:v1",
        observed_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=1),
        supported_order_types=frozenset(order_types),
        time_in_force=frozenset(tif),
        permission_scopes=frozenset({"ORDER_WRITE"}),
        position_mode="NET",
        native_protection=frozenset({"STOP"}),
        rate_limit_policy_id="whitebit-v4-test",
        data_entitlements=frozenset({"ORDERS"}),
        evidence=(evidence,),
        status="VERIFIED",
        sources=frozenset({"DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT"}),
    )


class WhiteBitAdapterTests(unittest.TestCase):
    def test_spot_limit_request_uses_exact_strings_and_dispatcher_client_id(self):
        intent = WhiteBitOrderIntent.create(
            instrument_version="BTC_USDT:v1",
            product_family="SPOT",
            market="btc_usdt",
            side="buy",
            order_type="limit",
            amount="0.0100",
            price="40000.25",
        )
        request = prepare_order_request(
            intent,
            client_order_id="at-0123456789abcdef",
            capability=capability(),
            at=NOW,
        )
        self.assertEqual(request.endpoint, "/api/v4/order/new")
        self.assertEqual(request.body["amount"], "0.0100")
        self.assertEqual(request.body["price"], "40000.25")
        self.assertEqual(request.body["clientOrderId"], "at-0123456789abcdef")
        self.assertNotIn("nonce", request.body)
        self.assertNotIn("request", request.body)

    def test_binary_float_money_is_rejected(self):
        with self.assertRaises(WhiteBitAdapterError):
            WhiteBitOrderIntent.create(
                instrument_version="BTC_USDT:v1",
                product_family="SPOT",
                market="BTC_USDT",
                side="BUY",
                order_type="LIMIT",
                amount=0.01,
                price="40000",
            )

    def test_capability_evidence_not_region_guess_controls_order_admission(self):
        intent = WhiteBitOrderIntent.create(
            instrument_version="BTC_USDT:v1",
            product_family="SPOT",
            market="BTC_USDT",
            side="BUY",
            order_type="LIMIT",
            amount="0.01",
            price="40000",
        )
        with self.assertRaisesRegex(WhiteBitAdapterError, "capability"):
            prepare_order_request(
                intent,
                client_order_id="at-order-1",
                capability=capability(order_types=("MARKET",)),
                at=NOW,
            )

    def test_collateral_reduce_only_is_explicit_and_spot_rejects_it(self):
        collateral = WhiteBitOrderIntent.create(
            instrument_version="BTC_USDT:v1",
            product_family="FUTURES",
            market="BTC_USDT",
            side="SELL",
            order_type="MARKET",
            amount="0.2",
            reduce_only=True,
            position_side="LONG",
        )
        request = prepare_order_request(
            collateral,
            client_order_id="at-reduce-1",
            capability=capability(),
            at=NOW,
        )
        self.assertEqual(request.endpoint, "/api/v4/order/collateral/market")
        self.assertIs(request.body["reduceOnly"], True)
        with self.assertRaisesRegex(WhiteBitAdapterError, "spot"):
            WhiteBitOrderIntent.create(
                instrument_version="BTC_USDT:v1",
                product_family="SPOT",
                market="BTC_USDT",
                side="SELL",
                order_type="MARKET",
                amount="0.2",
                reduce_only=True,
            )

    def test_ioc_and_post_only_conflict_fails_before_send(self):
        with self.assertRaisesRegex(WhiteBitAdapterError, "mutually exclusive"):
            WhiteBitOrderIntent.create(
                instrument_version="BTC_USDT:v1",
                product_family="SPOT",
                market="BTC_USDT",
                side="BUY",
                order_type="LIMIT",
                amount="0.1",
                price="1",
                time_in_force="IOC",
                post_only=True,
            )

    def test_client_order_id_matches_provider_constraints(self):
        self.assertEqual(validate_client_order_id("at-A_1.2-3"), "at-A_1.2-3")
        with self.assertRaises(WhiteBitAdapterError):
            validate_client_order_id("bad id")
        with self.assertRaises(WhiteBitAdapterError):
            validate_client_order_id("a" * 65)

    def test_taker_band_cancellation_is_partial_fill_not_failure(self):
        snapshot = parse_order_snapshot(
            {
                "orderId": 42,
                "clientOrderId": "at-order-42",
                "market": "BTC_USDT",
                "status": "CANCELED_TAKER_BAND",
                "amount": "1.0",
                "dealStock": "0.4",
                "dealMoney": "40",
                "left": "0.6",
            }
        )
        self.assertEqual(snapshot.normalized_status, "PARTIALLY_FILLED_CANCELLED")
        self.assertEqual(snapshot.filled_quantity, Decimal("0.4"))
        self.assertEqual(snapshot.average_fill_price, Decimal("100"))
        self.assertTrue(snapshot.terminal_remainder_cancelled)

    def test_reduce_only_provider_amount_is_preserved_without_assuming_requested_size(self):
        snapshot = parse_order_snapshot(
            {
                "orderId": "43",
                "clientOrderId": "at-reduce-43",
                "market": "BTC_USDT",
                "status": "FILLED",
                "amount": "0.3",
                "dealStock": "0.3",
                "dealMoney": "30",
                "left": "0",
                "reduceOnly": True,
            }
        )
        self.assertEqual(snapshot.provider_amount, Decimal("0.3"))
        self.assertIs(snapshot.reduce_only, True)

    def test_external_order_without_client_id_remains_provider_truth(self):
        snapshot = parse_order_snapshot(
            {
                "orderId": "external-45",
                "clientOrderId": "",
                "market": "BTC_USDT",
                "status": "NEW",
                "amount": "0.2",
                "dealStock": "0",
                "left": "0.2",
            }
        )
        self.assertIsNone(snapshot.client_order_id)
        self.assertEqual(snapshot.normalized_status, "WORKING")

    def test_additional_provider_terminal_statuses_preserve_partial_fill(self):
        liquidation = parse_order_snapshot(
            {
                "orderId": "46",
                "clientOrderId": "",
                "market": "BTC_USDT",
                "status": "AUTO_CANCELED_LIQUIDATION",
                "amount": "1",
                "dealStock": "0.25",
                "dealMoney": "25",
                "left": "0.75",
            }
        )
        self.assertEqual(
            liquidation.normalized_status,
            "PARTIALLY_FILLED_CANCELLED",
        )
        self.assertTrue(liquidation.terminal_remainder_cancelled)

        stp = parse_order_snapshot(
            {
                "orderId": "47",
                "clientOrderId": "at-stp-47",
                "market": "BTC_USDT",
                "status": "CANCELED_STP",
                "amount": "1",
                "dealStock": "0",
                "left": "1",
            }
        )
        self.assertEqual(stp.normalized_status, "CANCELLED")

    def test_unknown_provider_status_fails_closed(self):
        with self.assertRaisesRegex(WhiteBitAdapterError, "unsupported provider"):
            parse_order_snapshot(
                {
                    "orderId": "44",
                    "clientOrderId": "at-44",
                    "market": "BTC_USDT",
                    "status": "MAYBE_FILLED",
                    "amount": "1",
                    "dealStock": "0",
                }
            )

    def test_unique_execution_deal_maps_to_reconciliation_fill(self):
        deal = parse_execution_deal(
            {
                "id": 123,
                "clientOrderId": "at-order-123",
                "time": "1593233939.123456",
                "side": "buy",
                "role": 2,
                "amount": "0.001",
                "price": "40000",
                "deal": "40",
                "fee": "0.04",
                "orderId": 456,
                "feeAsset": "USDT",
            },
            market="BTC_USDT",
        )
        self.assertEqual(deal.provider_execution_id, "123")
        self.assertEqual(deal.provider_order_id, "456")
        self.assertEqual(deal.role, "TAKER")
        self.assertEqual(deal.trade_time, "2020-06-27T07:38:59.123456Z")
        fill = deal.to_reconciliation_fill()
        self.assertEqual(fill.provider_execution_id, "123")
        self.assertEqual(fill.quantity, Decimal("0.001"))
        self.assertEqual(fill.price, Decimal("40000"))
        self.assertEqual(fill.fee_amount, Decimal("0.04"))

    def test_execution_deal_without_client_id_remains_reconcilable(self):
        deal = parse_execution_deal(
            {
                "id": "manual-1",
                "clientOrderId": "",
                "time": "1593233939",
                "side": "sell",
                "role": 1,
                "amount": "0.001",
                "price": "40000",
                "deal": "40",
                "fee": "0",
                "orderId": "external-order",
                "feeAsset": "USDT",
            },
            market="BTC_USDT",
        )
        self.assertIsNone(deal.client_order_id)
        self.assertIsNone(deal.to_reconciliation_fill().client_order_id)

    def test_execution_deal_requires_exact_economic_identity(self):
        with self.assertRaisesRegex(WhiteBitAdapterError, "multiplied by price"):
            parse_execution_deal(
                {
                    "id": 123,
                    "time": "1593233939",
                    "side": "buy",
                    "role": 1,
                    "amount": "0.001",
                    "price": "40000",
                    "deal": "41",
                    "fee": "0",
                    "orderId": 456,
                    "feeAsset": "USDT",
                },
                market="BTC_USDT",
            )

    def test_execution_history_deduplicates_exact_rows_and_rejects_conflicts(self):
        row = {
            "id": 123,
            "clientOrderId": "at-order-123",
            "time": "1593233939",
            "side": "buy",
            "role": 1,
            "amount": "0.001",
            "price": "40000",
            "deal": "40",
            "fee": "0.04",
            "orderId": 456,
            "feeAsset": "USDT",
        }
        self.assertEqual(
            len(parse_execution_history([row, dict(row)], market="BTC_USDT")),
            1,
        )
        conflicting = dict(row)
        conflicting["price"] = "41000"
        conflicting["deal"] = "41"
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "conflicting observations",
        ):
            parse_execution_history([row, conflicting], market="BTC_USDT")

    def test_execution_time_rejects_binary_float_and_excess_precision(self):
        row = {
            "id": 123,
            "time": 1593233939.123456,
            "side": "sell",
            "role": 2,
            "amount": "0.001",
            "price": "40000",
            "deal": "40",
            "fee": "0",
            "orderId": 456,
            "feeAsset": "USDT",
        }
        with self.assertRaisesRegex(WhiteBitAdapterError, "exact decimal"):
            parse_execution_deal(row, market="BTC_USDT")
        row["time"] = "1593233939.1234567"
        with self.assertRaisesRegex(WhiteBitAdapterError, "microsecond"):
            parse_execution_deal(row, market="BTC_USDT")

    def test_exact_id_lookup_uses_active_and_history_surfaces(self):
        requests = order_lookup_requests(market="btc_usdt", client_order_id="at-lookup-1")
        self.assertEqual([item.surface for item in requests], ["OPEN_ORDERS", "ORDER_HISTORY"])
        self.assertEqual(requests[0].body["clientOrderId"], "at-lookup-1")
        self.assertNotEqual(requests[0].endpoint, requests[1].endpoint)

    def test_one_or_two_empty_order_surfaces_do_not_prove_absence(self):
        evidence = WhiteBitAbsenceEvidence(
            order_found=False,
            open_orders_complete=True,
            order_history_complete=True,
            executions_complete=False,
            activities_complete=False,
            consistency_horizon_satisfied=True,
        )
        self.assertEqual(evidence.verdict(), "INCONCLUSIVE")

    def test_absence_requires_all_surfaces_and_consistency_horizon(self):
        evidence = WhiteBitAbsenceEvidence(
            order_found=False,
            open_orders_complete=True,
            order_history_complete=True,
            executions_complete=True,
            activities_complete=True,
            consistency_horizon_satisfied=True,
        )
        self.assertEqual(evidence.verdict(), "PROVEN_ABSENT")
        found = WhiteBitAbsenceEvidence(
            order_found=True,
            open_orders_complete=False,
            order_history_complete=False,
            executions_complete=False,
            activities_complete=False,
            consistency_horizon_satisfied=False,
        )
        self.assertEqual(found.verdict(), "FOUND")


if __name__ == "__main__":
    unittest.main()
