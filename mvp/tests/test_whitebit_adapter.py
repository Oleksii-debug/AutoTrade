import unittest
from decimal import Decimal

from mvp.autotrade_mvp.provider_core import ProviderCoreError
from mvp.autotrade_mvp.whitebit_adapter import (
    WhiteBitCapabilitySnapshot,
    WhiteBitHistoryCoverage,
    WhiteBitHistoryPageEvidence,
    WhiteBitMarketRules,
    build_order_write_plan,
    build_exact_client_order_lookup,
    build_history_page_request,
    classify_whitebit_write,
    normalize_order_observation,
    parse_market_rules,
    redact_whitebit_debug,
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
