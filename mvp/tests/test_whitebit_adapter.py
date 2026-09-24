import base64
import hashlib
import hmac
import json
import unittest
from decimal import Decimal

from mvp.autotrade_mvp.provider_core import ProviderCoreError
from mvp.autotrade_mvp.whitebit_adapter import (
    WhiteBitCapabilitySnapshot,
    WhiteBitHistoryCoverage,
    WhiteBitHistoryPageEvidence,
    WhiteBitMarketRules,
    WhiteBitOpenOrderCoverage,
    build_order_write_plan,
    build_exact_client_order_lookup,
    build_history_page_request,
    build_open_order_page_request,
    classify_whitebit_write,
    normalize_execution_deal,
    normalize_execution_history,
    normalize_order_observation,
    normalize_working_order,
    normalize_working_orders,
    parse_market_rules,
    redact_whitebit_debug,
    sign_private_request,
)


def capability(
    *,
    family="SPOT",
    types=frozenset({"MARKET", "LIMIT", "STOP_MARKET", "STOP_LIMIT"}),
    reduce_only=False,
):
    return WhiteBitCapabilitySnapshot(
        evidence_id="capability:acct:2026-09-24",
        environment="RECORDED_FIXTURE",
        product_family=family,
        allowed_order_types=types,
        reduce_only_supported=reduce_only,
    )


def rules():
    return WhiteBitMarketRules.create(
        market="BTC_USDT",
        amount_step="0.001",
        price_tick="0.1",
        minimum_amount="0.001",
        minimum_total="5",
        maximum_total="1000000",
    )


class WhiteBitAdapterTests(unittest.TestCase):
    def test_spot_limit_plan_is_exact_and_has_no_transport(self):
        plan = build_order_write_plan(
            capability=capability(),
            rules=rules(),
            client_order_id="at-order_1",
            side="BUY",
            order_type="LIMIT",
            quantity="0.010",
            price="50000.0",
            post_only=True,
        )
        self.assertEqual(plan.endpoint, "/api/v4/order/new")
        self.assertEqual(
            dict(plan.payload),
            {
                "market": "BTC_USDT",
                "side": "buy",
                "amount": "0.01",
                "clientOrderId": "at-order_1",
                "price": "50000",
                "postOnly": True,
            },
        )
        self.assertEqual(plan.adjusted_quantity, Decimal("0.010"))
        self.assertIsNone(plan.adjustment_reason)

    def test_capability_snapshot_is_authoritative_over_generic_product_defaults(self):
        restricted = capability(types=frozenset({"MARKET"}))
        with self.assertRaisesRegex(
            ProviderCoreError,
            "not allowed by the account capability snapshot",
        ):
            build_order_write_plan(
                capability=restricted,
                rules=rules(),
                client_order_id="at-limit",
                side="BUY",
                order_type="LIMIT",
                quantity="0.01",
                price="50000",
            )

    def test_reduce_only_clips_to_open_position_but_never_flips_it(self):
        collateral = capability(
            family="COLLATERAL",
            types=frozenset({"MARKET", "LIMIT"}),
            reduce_only=True,
        )
        plan = build_order_write_plan(
            capability=collateral,
            rules=rules(),
            client_order_id="at-close",
            side="SELL",
            order_type="LIMIT",
            quantity="0.020",
            price="50000",
            reduce_only=True,
            current_position="0.012",
        )
        self.assertEqual(plan.endpoint, "/api/v4/order/collateral/limit")
        self.assertEqual(plan.payload["amount"], "0.012")
        self.assertTrue(plan.payload["reduceOnly"])
        self.assertEqual(
            plan.adjustment_reason,
            "REDUCE_ONLY_CLIPPED_TO_POSITION",
        )

        with self.assertRaisesRegex(ProviderCoreError, "increase a long"):
            build_order_write_plan(
                capability=collateral,
                rules=rules(),
                client_order_id="at-wrong-side",
                side="BUY",
                order_type="MARKET",
                quantity="0.01",
                reduce_only=True,
                current_position="0.02",
            )

    def test_provider_steps_are_fail_closed_not_silently_rounded(self):
        with self.assertRaisesRegex(ProviderCoreError, "exact multiple"):
            build_order_write_plan(
                capability=capability(),
                rules=rules(),
                client_order_id="at-step",
                side="BUY",
                order_type="LIMIT",
                quantity="0.0105",
                price="50000",
            )
        with self.assertRaisesRegex(ProviderCoreError, "exact multiple"):
            build_order_write_plan(
                capability=capability(),
                rules=rules(),
                client_order_id="at-tick",
                side="BUY",
                order_type="LIMIT",
                quantity="0.01",
                price="50000.05",
            )

    def test_binary_float_economics_are_rejected(self):
        with self.assertRaisesRegex(ProviderCoreError, "exact decimal"):
            build_order_write_plan(
                capability=capability(),
                rules=rules(),
                client_order_id="at-float",
                side="BUY",
                order_type="MARKET",
                quantity=0.01,
            )

    def test_market_plan_cannot_carry_limit_price(self):
        with self.assertRaisesRegex(ProviderCoreError, "must not carry price"):
            build_order_write_plan(
                capability=capability(),
                rules=rules(),
                client_order_id="at-market",
                side="BUY",
                order_type="MARKET",
                quantity="0.01",
                price="50000",
            )

    def test_stop_limit_requires_aligned_trigger_and_limit(self):
        plan = build_order_write_plan(
            capability=capability(),
            rules=rules(),
            client_order_id="at-stop",
            side="SELL",
            order_type="STOP_LIMIT",
            quantity="0.01",
            price="49000.0",
            activation_price="49500.0",
        )
        self.assertEqual(plan.endpoint, "/api/v4/order/stop_limit")
        self.assertEqual(plan.payload["price"], "49000")
        self.assertEqual(plan.payload["activation_price"], "49500")

    def test_ioc_and_post_only_conflict_and_ioc_is_spot_limit_only(self):
        with self.assertRaisesRegex(ProviderCoreError, "mutually exclusive"):
            build_order_write_plan(
                capability=capability(),
                rules=rules(),
                client_order_id="at-flags",
                side="BUY",
                order_type="LIMIT",
                quantity="0.01",
                price="50000",
                post_only=True,
                immediate_or_cancel=True,
            )
        collateral = capability(
            family="COLLATERAL",
            types=frozenset({"LIMIT"}),
            reduce_only=True,
        )
        with self.assertRaisesRegex(ProviderCoreError, "spot limit"):
            build_order_write_plan(
                capability=collateral,
                rules=rules(),
                client_order_id="at-coll-ioc",
                side="SELL",
                order_type="LIMIT",
                quantity="0.01",
                price="50000",
                immediate_or_cancel=True,
            )

    def test_client_order_id_format_is_strict(self):
        for invalid in ("", "has space", "x" * 65, "bad/slash"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ProviderCoreError):
                    build_order_write_plan(
                        capability=capability(),
                        rules=rules(),
                        client_order_id=invalid,
                        side="BUY",
                        order_type="MARKET",
                        quantity="0.01",
                    )

    def test_taker_band_cancel_preserves_partial_execution_without_inventing_fill(self):
        observed = normalize_order_observation(
            {
                "orderId": 42,
                "clientOrderId": "at-partial",
                "status": "CANCELED_TAKER_BAND",
                "dealStock": "0.007",
                "left": "0.003",
            }
        )
        self.assertTrue(observed.terminal)
        self.assertTrue(observed.terminal_remainder_cancelled)
        self.assertEqual(observed.executed_quantity, Decimal("0.007"))
        self.assertEqual(observed.remaining_quantity, Decimal("0.003"))
        self.assertFalse(observed.economic_fill_authoritative)

    def test_http_acknowledgement_is_not_fill_and_timeout_after_send_is_unknown(self):
        ack = classify_whitebit_write(
            transport_started=True,
            http_response_received=True,
            accepted_order_response=True,
        )
        self.assertEqual(ack.status, "ACKNOWLEDGED")
        self.assertFalse(ack.retry_same_economic_action)

        ambiguous = classify_whitebit_write(
            transport_started=True,
            http_response_received=False,
        )
        self.assertEqual(ambiguous.status, "UNKNOWN")
        self.assertTrue(ambiguous.reconciliation_required)
        self.assertFalse(ambiguous.retry_same_economic_action)

        before_send = classify_whitebit_write(
            transport_started=False,
            http_response_received=False,
        )
        self.assertEqual(before_send.status, "NOT_SENT")
        self.assertTrue(before_send.retry_same_economic_action)

    def test_explicit_provider_rejection_is_not_blind_retry(self):
        rejected = classify_whitebit_write(
            transport_started=True,
            http_response_received=True,
            explicit_validation_rejection=True,
        )
        self.assertEqual(rejected.status, "REJECTED")
        self.assertFalse(rejected.retry_same_economic_action)

    def test_execution_deal_uses_unique_deal_id_and_exact_economics(self):
        deal = normalize_execution_deal(
            {
                "id": 123,
                "clientOrderId": "at-order-1",
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

    def test_execution_deal_rejects_inconsistent_notional(self):
        with self.assertRaisesRegex(ProviderCoreError, "amount \* price"):
            normalize_execution_deal(
                {
                    "id": 123,
                    "clientOrderId": "at-order-1",
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

    def test_execution_history_deduplicates_exact_deals_and_rejects_conflicts(self):
        first = {
            "id": 123,
            "clientOrderId": "at-order-1",
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
        deduped = normalize_execution_history(
            [first, dict(first)],
            market="BTC_USDT",
        )
        self.assertEqual(len(deduped), 1)

        conflict = dict(first)
        conflict["price"] = "41000"
        conflict["deal"] = "41"
        with self.assertRaisesRegex(ProviderCoreError, "conflicting observations"):
            normalize_execution_history(
                [first, conflict],
                market="BTC_USDT",
            )

    def test_execution_time_requires_exact_microsecond_precision(self):
        record = {
            "id": 123,
            "clientOrderId": "at-order-1",
            "time": "1593233939.1234567",
            "side": "sell",
            "role": 2,
            "amount": "0.001",
            "price": "40000",
            "deal": "40",
            "fee": "0",
            "orderId": 456,
            "feeAsset": "USDT",
        }
        with self.assertRaisesRegex(ProviderCoreError, "microsecond"):
            normalize_execution_deal(record, market="BTC_USDT")

    def test_exact_client_lookup_does_not_smuggle_date_filters(self):
        request = build_exact_client_order_lookup(
            market="BTC_USDT",
            client_order_id="at-exact-1",
        )
        self.assertTrue(request["single_order_lookup"])
        self.assertEqual(
            request["payload"],
            {
                "market": "BTC_USDT",
                "clientOrderId": "at-exact-1",
            },
        )

    def test_history_pagination_requires_a_short_final_page(self):
        coverage = WhiteBitHistoryCoverage()
        coverage.add_page(
            WhiteBitHistoryPageEvidence(
                offset=0,
                limit=50,
                record_count=50,
            )
        )
        self.assertFalse(coverage.complete)
        self.assertEqual(coverage.next_offset, 50)
        coverage.add_page(
            WhiteBitHistoryPageEvidence(
                offset=50,
                limit=50,
                record_count=7,
            )
        )
        self.assertTrue(coverage.complete)
        with self.assertRaisesRegex(ProviderCoreError, "already complete"):
            coverage.add_page(
                WhiteBitHistoryPageEvidence(
                    offset=100,
                    limit=50,
                    record_count=0,
                )
            )

    def test_history_pagination_gap_fails_closed(self):
        coverage = WhiteBitHistoryCoverage()
        coverage.add_page(
            WhiteBitHistoryPageEvidence(
                offset=0,
                limit=50,
                record_count=50,
            )
        )
        with self.assertRaisesRegex(ProviderCoreError, "pagination gap"):
            coverage.add_page(
                WhiteBitHistoryPageEvidence(
                    offset=75,
                    limit=50,
                    record_count=0,
                )
            )

    def test_history_request_enforces_provider_window_and_limit(self):
        request = build_history_page_request(
            market="BTC_USDT",
            start_unix=1_700_000_000,
            end_unix=1_700_000_000 + 31 * 24 * 60 * 60,
            offset=0,
            limit=500,
        )
        self.assertFalse(request["single_order_lookup"])
        self.assertEqual(request["payload"]["limit"], 500)
        with self.assertRaisesRegex(ProviderCoreError, "31 days"):
            build_history_page_request(
                market="BTC_USDT",
                start_unix=1_700_000_000,
                end_unix=1_700_000_000 + 31 * 24 * 60 * 60 + 1,
                offset=0,
            )

    def test_market_rule_metadata_is_exact_and_missing_fields_fail_closed(self):
        parsed = parse_market_rules(
            {
                "name": "BTC_USDT",
                "stepSize": "0.00001",
                "tickSize": "0.01",
                "minAmount": "0.0001",
                "minTotal": "5",
                "maxTotal": "100000",
            }
        )
        self.assertEqual(parsed.amount_step, Decimal("0.00001"))
        self.assertEqual(parsed.price_tick, Decimal("0.01"))
        with self.assertRaisesRegex(ProviderCoreError, "missing required field"):
            parse_market_rules(
                {
                    "name": "BTC_USDT",
                    "stepSize": "0.00001",
                    "minAmount": "0.0001",
                    "minTotal": "5",
                }
            )

    def test_active_order_snapshot_maps_to_reconciliation_evidence(self):
        order = normalize_working_order(
            {
                "orderId": 4180284841,
                "clientOrderId": "",
                "market": "BTC_USDT",
                "left": "0.003",
                "status": "PARTIALLY_FILLED",
            }
        )
        self.assertEqual(order.provider_order_id, "4180284841")
        self.assertIsNone(order.client_order_id)
        self.assertEqual(order.instrument, "BTC_USDT")
        self.assertEqual(order.remaining_quantity, Decimal("0.003"))

    def test_terminal_order_cannot_enter_working_snapshot(self):
        with self.assertRaisesRegex(ProviderCoreError, "terminal provider order"):
            normalize_working_order(
                {
                    "orderId": 4180284841,
                    "clientOrderId": "at-old",
                    "market": "BTC_USDT",
                    "left": "0.001",
                    "status": "CANCELED_TAKER_BAND",
                }
            )

    def test_working_order_duplicates_are_idempotent_but_conflicts_fail(self):
        first = {
            "orderId": 7,
            "clientOrderId": "at-live",
            "market": "BTC_USDT",
            "left": "0.01",
            "status": "PARTIALLY_FILLED",
        }
        self.assertEqual(len(normalize_working_orders([first, dict(first)])), 1)
        conflict = dict(first)
        conflict["left"] = "0.02"
        with self.assertRaisesRegex(ProviderCoreError, "conflicting observations"):
            normalize_working_orders([first, conflict])

    def test_active_order_pagination_has_100_record_provider_cap(self):
        request = build_open_order_page_request(offset=0, limit=100)
        self.assertTrue(request["all_markets"])
        self.assertEqual(request["payload"], {"offset": 0, "limit": 100})
        with self.assertRaisesRegex(ProviderCoreError, "between 1 and 100"):
            build_open_order_page_request(offset=0, limit=101)

        coverage = WhiteBitOpenOrderCoverage()
        coverage.add_page(
            WhiteBitHistoryPageEvidence(
                offset=0,
                limit=100,
                record_count=100,
            )
        )
        self.assertFalse(coverage.complete)
        coverage.add_page(
            WhiteBitHistoryPageEvidence(
                offset=100,
                limit=100,
                record_count=2,
            )
        )
        self.assertTrue(coverage.complete)

    def test_active_order_pagination_gap_blocks_full_snapshot(self):
        coverage = WhiteBitOpenOrderCoverage()
        coverage.add_page(
            WhiteBitHistoryPageEvidence(
                offset=0,
                limit=50,
                record_count=50,
            )
        )
        with self.assertRaisesRegex(ProviderCoreError, "pagination gap"):
            coverage.add_page(
                WhiteBitHistoryPageEvidence(
                    offset=100,
                    limit=50,
                    record_count=0,
                )
            )

    def test_private_signing_uses_exact_payload_and_caller_nonce(self):
        signed = sign_private_request(
            endpoint="/api/v4/order/new",
            parameters={
                "market": "BTC_USDT",
                "side": "buy",
                "amount": "0.01",
            },
            nonce=1_764_000_000_001,
            api_key="public-key",
            api_secret="private-secret",
            nonce_window=True,
        )
        body = json.loads(signed.body.decode("utf-8"))
        self.assertEqual(body["request"], "/api/v4/order/new")
        self.assertEqual(body["nonce"], 1_764_000_000_001)
        self.assertTrue(body["nonceWindow"])

        expected_payload = base64.b64encode(signed.body).decode("ascii")
        expected_signature = hmac.new(
            b"private-secret",
            expected_payload.encode("ascii"),
            hashlib.sha512,
        ).hexdigest()
        self.assertEqual(signed.headers["X-TXC-PAYLOAD"], expected_payload)
        self.assertEqual(signed.headers["X-TXC-SIGNATURE"], expected_signature)
        self.assertEqual(signed.headers["X-TXC-APIKEY"], "public-key")

        debug = signed.safe_debug()
        self.assertEqual(debug["headers"]["X-TXC-APIKEY"], "<redacted>")
        self.assertEqual(debug["headers"]["X-TXC-PAYLOAD"], "<redacted>")
        self.assertEqual(debug["headers"]["X-TXC-SIGNATURE"], "<redacted>")

    def test_signer_never_allocates_or_accepts_conflicting_nonce(self):
        with self.assertRaisesRegex(ProviderCoreError, "nonce must be"):
            sign_private_request(
                endpoint="/api/v4/orders",
                parameters={},
                nonce=0,
                api_key="key",
                api_secret="secret",
            )
        with self.assertRaisesRegex(ProviderCoreError, "conflicts"):
            sign_private_request(
                endpoint="/api/v4/orders",
                parameters={"nonce": 9},
                nonce=10,
                api_key="key",
                api_secret="secret",
            )

    def test_signer_rejects_binary_float_before_financial_request(self):
        with self.assertRaisesRegex(ProviderCoreError, "binary float"):
            sign_private_request(
                endpoint="/api/v4/order/new",
                parameters={"amount": 0.01},
                nonce=1,
                api_key="key",
                api_secret="secret",
            )

    def test_debug_redaction_covers_nested_auth_material(self):
        original = {
            "headers": {
                "X-TXC-APIKEY": "public-key",
                "X-TXC-PAYLOAD": "encoded-body",
                "X-TXC-SIGNATURE": "signature",
                "Content-Type": "application/json",
            },
            "body": {"market": "BTC_USDT", "secret": "do-not-log"},
        }
        redacted = redact_whitebit_debug(original)
        self.assertEqual(redacted["headers"]["X-TXC-APIKEY"], "<redacted>")
        self.assertEqual(redacted["headers"]["X-TXC-PAYLOAD"], "<redacted>")
        self.assertEqual(redacted["headers"]["X-TXC-SIGNATURE"], "<redacted>")
        self.assertEqual(redacted["body"]["secret"], "<redacted>")
        self.assertEqual(redacted["body"]["market"], "BTC_USDT")


if __name__ == "__main__":
    unittest.main()
