from datetime import datetime, timedelta, timezone
from decimal import Decimal
import base64
import hashlib
import hmac
import json
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.whitebit import (
    WhiteBitAbsenceEvidence,
    WhiteBitAdapterError,
    WhiteBitMarketRules,
    WhiteBitOrderIntent,
    WhiteBitPreparedRequest,
    WhiteBitPageEvidence,
    WhiteBitRecoveryCheckpoint,
    absence_evidence_from_coverages,
    collateral_balance_request,
    decode_whitebit_json,
    execution_history_coverage,
    funding_history_request,
    market_fee_request,
    open_order_coverage,
    open_positions_request,
    order_history_coverage,
    order_lookup_requests,
    paged_execution_history_request,
    paged_open_orders_request,
    paged_order_history_request,
    parse_execution_deal,
    parse_execution_history,
    parse_fee_schedule,
    parse_collateral_balances,
    parse_funding_page,
    parse_hedge_mode,
    parse_spot_balances,
    parse_open_position,
    parse_open_positions,
    parse_order_snapshot,
    parse_submission_result,
    prepare_order_request,
    redact_whitebit_debug,
    provider_collateral_borrow,
    provider_collateral_cash,
    provider_spot_cash,
    sign_private_request,
    signed_position_quantities,
    spot_balance_request,
    validate_client_order_id,
    validate_intent_market_rules,
    validate_websocket_endpoint,
    websocket_recovery_policy,
)


NOW = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def capability(
    *,
    order_types=("LIMIT", "MARKET", "STOP_MARKET", "STOP_LIMIT"),
    tif=("GTC", "IOC"),
    account_id="account-1",
    environment="PAPER",
):
    observed_at = NOW - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="WHITEBIT",
            account_id=account_id,
            entity_id="global",
            environment=environment,
            instrument_version="BTC_USDT:v1",
            observed_at=observed_at,
            expires_at=NOW + timedelta(hours=1),
            supported_order_types=frozenset(order_types),
            time_in_force=frozenset(tif),
            permission_scopes=frozenset({"ORDER_WRITE"}),
            position_mode="NET",
            native_protection=frozenset({"STOP"}),
            rate_limit_policy_id="whitebit-v4-test",
            data_entitlements=frozenset({"ORDERS"}),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "a" * 64,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                "source_uri": "https://docs.whitebit.com/concepts/order-types",
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=observed_at,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def market_rules(
    *,
    market_type="spot",
    is_collateral=True,
    step_size="0.001",
    tick_size="0.01",
    min_amount="0.001",
    min_total="5",
    max_total="1000000",
    delisted_at=None,
):
    return WhiteBitMarketRules.from_provider(
        {
            "name": "BTC_USDT",
            "type": market_type,
            "isCollateral": is_collateral,
            "tradesEnabled": True,
            "stepSize": step_size,
            "tickSize": tick_size,
            "minAmount": min_amount,
            "minTotal": min_total,
            "maxTotal": max_total,
            "delistedAt": delisted_at,
        }
    )


class WhiteBitAdapterTests(unittest.TestCase):
    def test_direct_intent_construction_cannot_bypass_provider_invariants(self):
        with self.assertRaisesRegex(WhiteBitAdapterError, "exact decimal"):
            WhiteBitOrderIntent(
                instrument_version="BTC_USDT:v1",
                product_family="SPOT",
                market="BTC_USDT",
                side="BUY",
                order_type="LIMIT",
                amount=0.01,
                price=Decimal("40000"),
            )
        with self.assertRaisesRegex(WhiteBitAdapterError, "reduce_only"):
            WhiteBitOrderIntent(
                instrument_version="BTC_USDT:v1",
                product_family="SPOT",
                market="BTC_USDT",
                side="SELL",
                order_type="MARKET",
                amount=Decimal("0.01"),
                reduce_only=True,
            )
        normalized = WhiteBitOrderIntent(
            instrument_version=" BTC_USDT:v1 ",
            product_family="spot",
            market="btc_usdt",
            side="buy",
            order_type="limit",
            amount="0.0100",
            price="40000.25",
        )
        self.assertEqual(normalized.product_family, "SPOT")
        self.assertEqual(normalized.market, "BTC_USDT")
        self.assertEqual(normalized.side, "BUY")
        self.assertEqual(normalized.amount, Decimal("0.0100"))

    def test_direct_prepared_request_cannot_bypass_guarded_scope_or_provenance(self):
        base = {
            "endpoint": "/api/v4/order/new",
            "body": {
                "market": "BTC_USDT",
                "clientOrderId": "at-direct-guard",
            },
            "account_id": "account-1",
            "environment": "PAPER",
            "capability_snapshot_id": "cap-1",
            "documentation_refs": ("https://docs.whitebit.com/order",),
        }
        request = WhiteBitPreparedRequest(**base)
        self.assertEqual(request.environment, "PAPER")
        self.assertEqual(request.capability_snapshot_id, "cap-1")
        with self.assertRaisesRegex(WhiteBitAdapterError, "environment"):
            WhiteBitPreparedRequest(**{**base, "environment": "STAGING"})
        with self.assertRaisesRegex(WhiteBitAdapterError, "capability_snapshot_id"):
            WhiteBitPreparedRequest(**{**base, "capability_snapshot_id": " "})
        with self.assertRaisesRegex(WhiteBitAdapterError, "documentation_refs"):
            WhiteBitPreparedRequest(**{**base, "documentation_refs": ()})
        with self.assertRaisesRegex(WhiteBitAdapterError, "client_order_id|clientOrderId"):
            WhiteBitPreparedRequest(
                **{**base, "body": {"market": "BTC_USDT", "clientOrderId": ""}}
            )
        with self.assertRaisesRegex(WhiteBitAdapterError, "endpoint"):
            WhiteBitPreparedRequest(**{**base, "endpoint": "https://evil.test/order"})
        with self.assertRaisesRegex(WhiteBitAdapterError, "order path"):
            WhiteBitPreparedRequest(
                **{**base, "endpoint": "/api/v4/main-account/withdraw"}
            )
        for field, value in (("clientOrderId", None), ("clientOrderId", 123), ("market", True)):
            with self.subTest(field=field, value=value), self.assertRaises(
                WhiteBitAdapterError
            ):
                WhiteBitPreparedRequest(
                    **{**base, "body": {**base["body"], field: value}}
                )

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
            account_id="account-1",
            environment="PAPER",
            capability=capability(),
            market_rules=market_rules(),
            at=NOW,
        )
        self.assertEqual(request.endpoint, "/api/v4/order/new")
        self.assertEqual(request.account_id, "account-1")
        self.assertEqual(request.environment, "PAPER")
        self.assertEqual(request.body["amount"], "0.0100")
        self.assertEqual(request.body["price"], "40000.25")
        self.assertEqual(request.body["clientOrderId"], "at-0123456789abcdef")
        self.assertNotIn("nonce", request.body)
        self.assertNotIn("request", request.body)

    def test_prepared_and_recorded_submission_share_exact_account_environment_scope(self):
        intent = WhiteBitOrderIntent.create(
            instrument_version="BTC_USDT:v1",
            product_family="SPOT",
            market="BTC_USDT",
            side="BUY",
            order_type="MARKET",
            amount="0.01",
        )
        with self.assertRaisesRegex(WhiteBitAdapterError, "account"):
            prepare_order_request(
                intent,
                client_order_id="at-cross-account",
                account_id="other-account",
                environment="PAPER",
                capability=capability(),
                market_rules=market_rules(),
                at=NOW,
            )
        request = prepare_order_request(
            intent,
            client_order_id="at-scope-bind",
            account_id="account-1",
            environment="PAPER",
            capability=capability(),
            market_rules=market_rules(),
            at=NOW,
        )
        with self.assertRaisesRegex(WhiteBitAdapterError, "submission account"):
            parse_submission_result(
                request,
                attempt_id="attempt-cross-account",
                account_id="other-account",
                environment="PAPER",
                observed_at=NOW,
                response_body=None,
                http_status=None,
                transport_ambiguous=True,
            )
        with self.assertRaisesRegex(WhiteBitAdapterError, "submission environment"):
            parse_submission_result(
                request,
                attempt_id="attempt-cross-environment",
                account_id="account-1",
                environment="LIVE",
                observed_at=NOW,
                response_body=None,
                http_status=None,
                transport_ambiguous=True,
            )

    def test_spot_market_buy_uses_base_quantity_stock_market_endpoint(self):
        intent = WhiteBitOrderIntent.create(
            instrument_version="BTC_USDT:v1",
            product_family="SPOT",
            market="BTC_USDT",
            side="BUY",
            order_type="MARKET",
            amount="0.010",
        )
        request = prepare_order_request(
            intent,
            client_order_id="at-stock-buy",
            account_id="account-1",
            environment="PAPER",
            capability=capability(),
            market_rules=market_rules(),
            at=NOW,
        )
        self.assertEqual(request.endpoint, "/api/v4/order/stock_market")
        self.assertEqual(request.body["amount"], "0.010")

    def test_spot_stop_market_buy_fails_closed_on_quote_amount_semantics(self):
        intent = WhiteBitOrderIntent.create(
            instrument_version="BTC_USDT:v1",
            product_family="SPOT",
            market="BTC_USDT",
            side="BUY",
            order_type="STOP_MARKET",
            amount="0.010",
            activation_price="40000.00",
        )
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "quote-currency amount",
        ):
            prepare_order_request(
                intent,
                client_order_id="at-stop-buy",
                account_id="account-1",
                environment="PAPER",
                capability=capability(),
                market_rules=market_rules(),
                at=NOW,
            )

    def test_market_rules_fail_closed_on_amount_and_price_steps(self):
        bad_amount = WhiteBitOrderIntent.create(
            instrument_version="BTC_USDT:v1",
            product_family="SPOT",
            market="BTC_USDT",
            side="BUY",
            order_type="LIMIT",
            amount="0.0105",
            price="40000.00",
        )
        with self.assertRaisesRegex(WhiteBitAdapterError, "amount step"):
            validate_intent_market_rules(
                bad_amount,
                market_rules(),
                at=NOW,
            )

        bad_price = WhiteBitOrderIntent.create(
            instrument_version="BTC_USDT:v1",
            product_family="SPOT",
            market="BTC_USDT",
            side="BUY",
            order_type="LIMIT",
            amount="0.010",
            price="40000.005",
        )
        with self.assertRaisesRegex(WhiteBitAdapterError, "price step"):
            validate_intent_market_rules(
                bad_price,
                market_rules(),
                at=NOW,
            )

    def test_market_rules_validate_total_and_delisting(self):
        too_small_total = WhiteBitOrderIntent.create(
            instrument_version="BTC_USDT:v1",
            product_family="SPOT",
            market="BTC_USDT",
            side="BUY",
            order_type="LIMIT",
            amount="0.001",
            price="1.00",
        )
        with self.assertRaisesRegex(WhiteBitAdapterError, "minTotal"):
            validate_intent_market_rules(
                too_small_total,
                market_rules(),
                at=NOW,
            )
        with self.assertRaisesRegex(WhiteBitAdapterError, "delisting"):
            validate_intent_market_rules(
                WhiteBitOrderIntent.create(
                    instrument_version="BTC_USDT:v1",
                    product_family="SPOT",
                    market="BTC_USDT",
                    side="BUY",
                    order_type="LIMIT",
                    amount="0.010",
                    price="40000.00",
                ),
                market_rules(delisted_at=int(NOW.timestamp()) - 1),
                at=NOW,
            )

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
                account_id="account-1",
                environment="PAPER",
                capability=capability(order_types=("MARKET",)),
                market_rules=market_rules(),
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
            account_id="account-1",
            environment="PAPER",
            capability=capability(),
            market_rules=market_rules(market_type="futures"),
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

    def test_tradfi_futures_market_type_uses_guarded_collateral_route(self):
        intent = WhiteBitOrderIntent.create(
            instrument_version="RIVN_PERP:v1",
            product_family="FUTURES",
            market="RIVN_PERP",
            side="BUY",
            order_type="MARKET",
            amount="1",
        )
        rules = WhiteBitMarketRules.from_provider(
            {
                "name": "RIVN_PERP",
                "type": "tradfiFutures",
                "isCollateral": False,
                "tradesEnabled": True,
                "stepSize": "1",
                "tickSize": "0.01",
                "minAmount": "1",
                "minTotal": "1",
                "maxTotal": "0",
                "delistedAt": None,
            }
        )
        request = prepare_order_request(
            intent,
            client_order_id="at-tradfi-1",
            account_id="account-1",
            environment="PAPER",
            capability=capability(
                instrument_version="RIVN_PERP:v1",
                order_types=("MARKET",),
            ),
            market_rules=rules,
            at=NOW,
        )
        self.assertEqual(request.endpoint, "/api/v4/order/collateral/market")
        self.assertEqual(request.body["market"], "RIVN_PERP")

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

    def test_submission_success_is_acknowledged_not_fill(self):
        intent = WhiteBitOrderIntent.create(
            instrument_version="BTC_USDT:v1",
            product_family="SPOT",
            market="BTC_USDT",
            side="BUY",
            order_type="LIMIT",
            amount="0.010",
            price="40000.00",
        )
        request = prepare_order_request(
            intent,
            client_order_id="at-submit-1",
            account_id="account-1",
            environment="PAPER",
            capability=capability(),
            market_rules=market_rules(),
            at=NOW,
        )
        raw = (
            '{"orderId":4180284841,"clientOrderId":"at-submit-1",'
            '"market":"BTC_USDT","side":"buy","type":"limit",'
            '"timestamp":1595792396.165973,"dealMoney":"400",'
            '"dealStock":"0.010","amount":"0.010","left":"0",'
            '"dealFee":"0.4","price":"40000","status":"FILLED"}'
        )
        result = parse_submission_result(
            request,
            attempt_id="attempt-1",
            account_id="account-1",
            environment="PAPER",
            observed_at=NOW,
            response_body=raw,
            http_status=200,
        )
        self.assertEqual(result.outcome, "ACKNOWLEDGED")
        self.assertEqual(result.next_action, "OBSERVE_OR_RECONCILE")
        self.assertEqual(result.provider_order_id, "4180284841")
        self.assertEqual(result.provider_reported_status, "FILLED")
        self.assertTrue(result.response_sha256.startswith("sha256:"))
        self.assertFalse(hasattr(result, "filled_quantity"))

    def test_submission_response_must_match_guarded_client_and_market(self):
        intent = WhiteBitOrderIntent.create(
            instrument_version="BTC_USDT:v1",
            product_family="SPOT",
            market="BTC_USDT",
            side="BUY",
            order_type="LIMIT",
            amount="0.010",
            price="40000.00",
        )
        request = prepare_order_request(
            intent,
            client_order_id="at-submit-2",
            account_id="account-1",
            environment="PAPER",
            capability=capability(),
            market_rules=market_rules(),
            at=NOW,
        )
        with self.assertRaisesRegex(WhiteBitAdapterError, "clientOrderId"):
            parse_submission_result(
                request,
                attempt_id="attempt-2",
                account_id="account-1",
                environment="PAPER",
                observed_at=NOW,
                response_body=(
                    '{"orderId":1,"clientOrderId":"other-id",'
                    '"market":"BTC_USDT","status":"NEW"}'
                ),
                http_status=200,
            )
        with self.assertRaisesRegex(WhiteBitAdapterError, "market"):
            parse_submission_result(
                request,
                attempt_id="attempt-2b",
                account_id="account-1",
                environment="PAPER",
                observed_at=NOW,
                response_body=(
                    '{"orderId":1,"clientOrderId":"at-submit-2",'
                    '"market":"ETH_USDT","status":"NEW"}'
                ),
                http_status=200,
            )

    def test_ambiguous_transport_requires_reconcile_before_retry(self):
        intent = WhiteBitOrderIntent.create(
            instrument_version="BTC_USDT:v1",
            product_family="SPOT",
            market="BTC_USDT",
            side="BUY",
            order_type="LIMIT",
            amount="0.010",
            price="40000.00",
        )
        request = prepare_order_request(
            intent,
            client_order_id="at-unknown-1",
            account_id="account-1",
            environment="PAPER",
            capability=capability(),
            market_rules=market_rules(),
            at=NOW,
        )
        result = parse_submission_result(
            request,
            attempt_id="attempt-unknown",
            account_id="account-1",
            environment="PAPER",
            observed_at=NOW,
            response_body=None,
            http_status=None,
            transport_ambiguous=True,
        )
        self.assertEqual(result.outcome, "UNKNOWN")
        self.assertEqual(result.next_action, "RECONCILE_FIRST")
        self.assertIsNone(result.provider_order_id)
        self.assertIsNone(result.response_sha256)

    def test_documented_422_is_definitive_rejection_with_provenance(self):
        intent = WhiteBitOrderIntent.create(
            instrument_version="BTC_USDT:v1",
            product_family="SPOT",
            market="BTC_USDT",
            side="BUY",
            order_type="LIMIT",
            amount="0.010",
            price="40000.00",
        )
        request = prepare_order_request(
            intent,
            client_order_id="at-reject-1",
            account_id="account-1",
            environment="PAPER",
            capability=capability(),
            market_rules=market_rules(),
            at=NOW,
        )
        raw = (
            '{"code":30,"message":"Validation failed",'
            '"errors":{"amount":["Invalid argument."]}}'
        )
        result = parse_submission_result(
            request,
            attempt_id="attempt-reject",
            account_id="account-1",
            environment="PAPER",
            observed_at=NOW,
            response_body=raw,
            http_status=422,
        )
        self.assertEqual(result.outcome, "REJECTED")
        self.assertEqual(result.next_action, "DO_NOT_RETRY_BLINDLY")
        self.assertEqual(result.rejection_code, "30")
        self.assertEqual(result.rejection_message, "Validation failed")
        self.assertIsNone(result.provider_order_id)
        self.assertTrue(result.response_sha256.startswith("sha256:"))

    def test_unqualified_http_failure_cannot_be_guessed_as_rejected(self):
        intent = WhiteBitOrderIntent.create(
            instrument_version="BTC_USDT:v1",
            product_family="SPOT",
            market="BTC_USDT",
            side="BUY",
            order_type="LIMIT",
            amount="0.010",
            price="40000.00",
        )
        request = prepare_order_request(
            intent,
            client_order_id="at-http-500",
            account_id="account-1",
            environment="PAPER",
            capability=capability(),
            market_rules=market_rules(),
            at=NOW,
        )
        with self.assertRaisesRegex(WhiteBitAdapterError, "not qualified"):
            parse_submission_result(
                request,
                attempt_id="attempt-500",
                account_id="account-1",
                environment="PAPER",
                observed_at=NOW,
                response_body='{"message":"server error"}',
                http_status=500,
            )

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

    def test_provider_json_decoder_preserves_decimal_timestamp(self):
        decoded = decode_whitebit_json(
            '{"time":1593233939.123456,"amount":"0.001","id":123}'
        )
        self.assertIsInstance(decoded["time"], Decimal)
        self.assertEqual(decoded["time"], Decimal("1593233939.123456"))
        self.assertIsInstance(decoded["id"], int)

    def test_provider_json_decoder_rejects_nonfinite_and_invalid_input(self):
        with self.assertRaisesRegex(WhiteBitAdapterError, "non-finite"):
            decode_whitebit_json('{"value":NaN}')
        with self.assertRaisesRegex(WhiteBitAdapterError, "invalid"):
            decode_whitebit_json('{"broken":')

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
        self.assertEqual(deal.trade_time, "2020-06-27T04:58:59.123456Z")
        fill = deal.to_reconciliation_fill(account_id="paper-1", environment="PAPER")
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
        self.assertIsNone(deal.to_reconciliation_fill(account_id="paper-1", environment="PAPER").client_order_id)

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
        deals = parse_execution_history([row, dict(row)], market="BTC_USDT")
        self.assertEqual(len(deals), 1)
        provider_fill = deals[0].to_reconciliation_fill(
            account_id="paper-1",
            environment="PAPER",
        )
        self.assertEqual(provider_fill.side, "BUY")
        self.assertIsNone(provider_fill.position_side)
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

    def test_history_pagination_requires_short_final_page(self):
        coverage = order_history_coverage()
        coverage.add_page(
            WhiteBitPageEvidence(offset=0, limit=500, record_count=500)
        )
        self.assertFalse(coverage.complete)
        self.assertEqual(coverage.next_offset, 500)
        coverage.add_page(
            WhiteBitPageEvidence(offset=500, limit=500, record_count=7)
        )
        self.assertTrue(coverage.complete)
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "already complete",
        ):
            coverage.add_page(
                WhiteBitPageEvidence(offset=1000, limit=500, record_count=0)
            )

    def test_pagination_gap_or_wrong_surface_limit_fails_closed(self):
        coverage = execution_history_coverage()
        coverage.add_page(
            WhiteBitPageEvidence(offset=0, limit=50, record_count=50)
        )
        with self.assertRaisesRegex(WhiteBitAdapterError, "pagination gap"):
            coverage.add_page(
                WhiteBitPageEvidence(offset=75, limit=50, record_count=0)
            )

        open_coverage = open_order_coverage()
        with self.assertRaisesRegex(WhiteBitAdapterError, "cannot exceed 100"):
            open_coverage.add_page(
                WhiteBitPageEvidence(offset=0, limit=101, record_count=0)
            )

    def test_provider_page_builders_preserve_surface_and_time_bounds(self):
        order_history = paged_order_history_request(
            start_unix=1_700_000_000,
            end_unix=1_700_000_000 + 31 * 24 * 60 * 60,
            offset=0,
            limit=500,
            market="btc_usdt",
        )
        self.assertEqual(order_history.surface, "ORDER_HISTORY")
        self.assertEqual(order_history.body["limit"], 500)

        execution_history = paged_execution_history_request(
            start_unix=1_700_000_000,
            end_unix=1_700_000_100,
            offset=0,
            limit=500,
        )
        self.assertEqual(execution_history.surface, "EXECUTIONS")

        active = paged_open_orders_request(offset=0, limit=100)
        self.assertEqual(active.surface, "OPEN_ORDERS")
        self.assertNotIn("market", active.body)

        with self.assertRaisesRegex(WhiteBitAdapterError, "31 days"):
            paged_order_history_request(
                start_unix=1_700_000_000,
                end_unix=1_700_000_000 + 31 * 24 * 60 * 60 + 1,
                offset=0,
            )

    def test_open_position_preserves_exact_provider_economics(self):
        payload = decode_whitebit_json(
            '{"positionId":527,"market":"BTC_USDT",'
            '"openDate":1651568067.789679,"modifyDate":1651568068.123456,'
            '"amount":"0.1","basePrice":"45658.349",'
            '"liquidationPrice":null,"liquidationState":"margin_call",'
            '"pnl":"-168.42","margin":"8316.74","freeMargin":"619385.67",'
            '"funding":"-0.5","unrealizedFunding":"0.0019142920201966",'
            '"unrealizedPnl":"12.34","positionSide":"LONG"}'
        )
        position = parse_open_position(payload)
        self.assertEqual(position.position_id, "527")
        self.assertEqual(position.amount, Decimal("0.1"))
        self.assertEqual(position.realized_pnl, Decimal("-168.42"))
        self.assertEqual(
            position.unrealized_funding,
            Decimal("0.0019142920201966"),
        )
        self.assertEqual(
            position.opened_at,
            "2022-05-03T08:54:27.789679Z",
        )
        self.assertEqual(position.liquidation_state, "margin_call")

    def test_hedge_positions_project_to_signed_quantities_only_with_mode_evidence(self):
        long_payload = decode_whitebit_json(
            '{"positionId":1,"market":"BTC_USDT","openDate":1651568067,'
            '"modifyDate":1651568068,"amount":"0.2","basePrice":"40000",'
            '"pnl":"0","margin":"10","freeMargin":"100","funding":"0",'
            '"unrealizedFunding":"0","positionSide":"LONG"}'
        )
        short_payload = decode_whitebit_json(
            '{"positionId":2,"market":"BTC_USDT","openDate":1651568067,'
            '"modifyDate":1651568068,"amount":"0.05","basePrice":"41000",'
            '"pnl":"0","margin":"10","freeMargin":"100","funding":"0",'
            '"unrealizedFunding":"0","positionSide":"SHORT"}'
        )
        positions = parse_open_positions([long_payload, short_payload])
        projected = signed_position_quantities(
            positions,
            hedge_mode=True,
        )
        self.assertEqual(projected["BTC_USDT"], Decimal("0.15"))

    def test_one_way_both_position_is_not_guessed_into_signed_quantity(self):
        payload = decode_whitebit_json(
            '{"positionId":3,"market":"BTC_USDT","openDate":1651568067,'
            '"modifyDate":1651568068,"amount":"0.2","basePrice":"40000",'
            '"pnl":"0","margin":"10","freeMargin":"100","funding":"0",'
            '"unrealizedFunding":"0","positionSide":"BOTH"}'
        )
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "not qualified",
        ):
            signed_position_quantities(
                [parse_open_position(payload)],
                hedge_mode=False,
            )

    def test_position_identity_conflicts_fail_closed(self):
        row = decode_whitebit_json(
            '{"positionId":4,"market":"BTC_USDT","openDate":1651568067,'
            '"modifyDate":1651568068,"amount":"0.2","basePrice":"40000",'
            '"pnl":"0","margin":"10","freeMargin":"100","funding":"0",'
            '"unrealizedFunding":"0","positionSide":"LONG"}'
        )
        conflict = dict(row)
        conflict["amount"] = "0.3"
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "conflicting observations",
        ):
            parse_open_positions([row, conflict])

    def test_position_and_hedge_mode_lookup_shapes_are_network_free(self):
        request = open_positions_request()
        self.assertEqual(
            request.endpoint,
            "/api/v4/collateral-account/positions/open",
        )
        self.assertEqual(dict(request.body), {})
        self.assertTrue(parse_hedge_mode({"hedgeMode": True}))
        with self.assertRaisesRegex(WhiteBitAdapterError, "hedgeMode"):
            parse_hedge_mode({"hedgeMode": "true"})

    def test_spot_balance_cash_includes_frozen_owned_funds(self):
        observations = parse_spot_balances(
            {
                "BTC": {"available": "0.123", "freeze": "0.01"},
                "USDT": {"available": "1000.50", "freeze": "100.00"},
            }
        )
        cash = provider_spot_cash(observations)
        self.assertEqual(cash["BTC"], Decimal("0.133"))
        self.assertEqual(cash["USDT"], Decimal("1100.50"))
        self.assertEqual(observations[0].available, Decimal("0.123"))
        self.assertEqual(observations[0].frozen, Decimal("0.01"))

    def test_spot_balance_request_is_filterable_and_network_free(self):
        all_assets = spot_balance_request()
        one_asset = spot_balance_request(asset="btc")
        self.assertEqual(
            all_assets.endpoint,
            "/api/v4/trade-account/balance",
        )
        self.assertEqual(dict(all_assets.body), {})
        self.assertEqual(dict(one_asset.body), {"ticker": "BTC"})

    def test_spot_balance_schema_and_negative_values_fail_closed(self):
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "available and freeze",
        ):
            parse_spot_balances(
                {"BTC": {"available": "1", "freeze": "0", "extra": "x"}}
            )
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "cannot be negative",
        ):
            parse_spot_balances(
                {"BTC": {"available": "-0.1", "freeze": "0"}}
            )

    def test_collateral_balance_keeps_borrow_capacity_out_of_cash_truth(self):
        observations = parse_collateral_balances(
            [
                {
                    "asset": "BTC",
                    "balance": "0.5",
                    "borrow": "0.1",
                    "availableWithoutBorrow": "0.4",
                    "availableWithBorrow": "123.456",
                },
                {
                    "asset": "USDT",
                    "balance": "1000",
                    "borrow": "25",
                    "availableWithoutBorrow": "975",
                    "availableWithBorrow": "5000",
                },
            ]
        )
        cash = provider_collateral_cash(observations)
        liabilities = provider_collateral_borrow(observations)
        self.assertEqual(cash["BTC"], Decimal("0.5"))
        self.assertEqual(cash["USDT"], Decimal("1000"))
        self.assertEqual(liabilities["BTC"], Decimal("0.1"))
        self.assertEqual(liabilities["USDT"], Decimal("25"))
        self.assertNotEqual(
            cash["USDT"],
            observations[1].available_with_borrow,
        )

    def test_collateral_balance_duplicate_conflict_fails_closed(self):
        first = {
            "asset": "BTC",
            "balance": "0.5",
            "borrow": "0",
            "availableWithoutBorrow": "0.5",
            "availableWithBorrow": "123.456",
        }
        self.assertEqual(
            len(parse_collateral_balances([first, dict(first)])),
            1,
        )
        conflict = dict(first)
        conflict["borrow"] = "0.01"
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "conflicting balance",
        ):
            parse_collateral_balances([first, conflict])

    def test_collateral_balance_rejects_impossible_borrow_capacity(self):
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "cannot be below",
        ):
            parse_collateral_balances(
                [
                    {
                        "asset": "BTC",
                        "balance": "0.5",
                        "borrow": "0",
                        "availableWithoutBorrow": "0.5",
                        "availableWithBorrow": "0.4",
                    }
                ]
            )

    def test_collateral_balance_request_is_network_free_and_filterable(self):
        all_assets = collateral_balance_request()
        one_asset = collateral_balance_request(asset="btc")
        self.assertEqual(
            all_assets.endpoint,
            "/api/v4/collateral-account/balance-summary",
        )
        self.assertEqual(dict(all_assets.body), {})
        self.assertEqual(dict(one_asset.body), {"ticker": "BTC"})

    def test_current_websocket_host_is_required(self):
        self.assertEqual(
            validate_websocket_endpoint("wss://wss.whitebit.com/ws"),
            "wss://wss.whitebit.com/ws",
        )
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "deprecated",
        ):
            validate_websocket_endpoint("wss://api.whitebit.com/ws")
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "unqualified",
        ):
            validate_websocket_endpoint("wss://example.invalid/ws")

    def test_positions_recover_only_after_new_full_snapshot(self):
        policy = websocket_recovery_policy("positions")
        self.assertTrue(policy.full_snapshot_on_subscribe)
        self.assertFalse(policy.requires_backfill)
        before_snapshot = WhiteBitRecoveryCheckpoint(
            channel="POSITIONS",
            baseline_observed=False,
            subscription_confirmed=True,
            backfill_complete=False,
            full_snapshot_observed=False,
        )
        self.assertFalse(before_snapshot.recovered)
        after_snapshot = WhiteBitRecoveryCheckpoint(
            channel="POSITIONS",
            baseline_observed=False,
            subscription_confirmed=True,
            backfill_complete=False,
            full_snapshot_observed=True,
        )
        self.assertTrue(after_snapshot.recovered)

    def test_incremental_balance_requires_baseline_and_subscription(self):
        policy = websocket_recovery_policy("balance_spot")
        self.assertEqual(policy.query_method, "balanceSpot_request")
        self.assertTrue(policy.requires_backfill)
        no_baseline = WhiteBitRecoveryCheckpoint(
            channel="BALANCE_SPOT",
            baseline_observed=False,
            subscription_confirmed=True,
            backfill_complete=True,
            full_snapshot_observed=False,
        )
        self.assertFalse(no_baseline.recovered)
        complete = WhiteBitRecoveryCheckpoint(
            channel="BALANCE_SPOT",
            baseline_observed=True,
            subscription_confirmed=True,
            backfill_complete=True,
            full_snapshot_observed=False,
        )
        self.assertTrue(complete.recovered)

    def test_event_stream_reconnect_requires_backfill_not_just_resubscribe(self):
        for channel in ("DEALS", "ORDERS_EXECUTED"):
            with self.subTest(channel=channel):
                policy = websocket_recovery_policy(channel)
                self.assertEqual(policy.state_model, "EVENT_STREAM")
                incomplete = WhiteBitRecoveryCheckpoint(
                    channel=channel,
                    baseline_observed=True,
                    subscription_confirmed=True,
                    backfill_complete=False,
                    full_snapshot_observed=False,
                )
                self.assertFalse(incomplete.recovered)
                complete = WhiteBitRecoveryCheckpoint(
                    channel=channel,
                    baseline_observed=True,
                    subscription_confirmed=True,
                    backfill_complete=True,
                    full_snapshot_observed=False,
                )
                self.assertTrue(complete.recovered)

    def test_unknown_stream_channel_is_not_assumed_recoverable(self):
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "unqualified",
        ):
            websocket_recovery_policy("orders_magic")

    def test_fee_schedule_uses_account_defaults_and_market_override(self):
        schedule = parse_fee_schedule(
            {
                "error": None,
                "taker": "0.1",
                "maker": "0.1",
                "futures_taker": "0.035",
                "futures_maker": "0.01",
                "rpi_maker_fee_premium": "0.015",
                "futures_rpi_maker_fee_premium": "0.01",
                "custom_fee": {
                    "BTC_PERP": {
                        "taker": "0.055",
                        "maker": "0.01",
                    }
                },
            },
            evidence_id="fee-snapshot:1",
            observed_at=NOW,
        )
        self.assertEqual(
            schedule.effective_percent(
                product_family="SPOT",
                role="TAKER",
                market="BTC_USDT",
            ),
            Decimal("0.1"),
        )
        self.assertEqual(
            schedule.effective_percent(
                product_family="FUTURES",
                role="TAKER",
                market="BTC_PERP",
            ),
            Decimal("0.055"),
        )
        self.assertEqual(
            schedule.effective_fraction(
                product_family="FUTURES",
                role="TAKER",
                market="BTC_PERP",
            ),
            Decimal("0.00055"),
        )

    def test_rpi_fee_adds_account_premium_only_to_maker(self):
        schedule = parse_fee_schedule(
            {
                "error": None,
                "taker": "0.1",
                "maker": "0.1",
                "futures_taker": "0.035",
                "futures_maker": "0.01",
                "rpi_maker_fee_premium": "0.015",
                "futures_rpi_maker_fee_premium": None,
                "custom_fee": {},
            },
            evidence_id="fee-snapshot:2",
            observed_at=NOW,
        )
        self.assertEqual(
            schedule.effective_percent(
                product_family="SPOT",
                role="MAKER",
                market="BTC_USDT",
                rpi=True,
            ),
            Decimal("0.115"),
        )
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "maker-only",
        ):
            schedule.effective_percent(
                product_family="SPOT",
                role="TAKER",
                market="BTC_USDT",
                rpi=True,
            )
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "not configured",
        ):
            schedule.effective_percent(
                product_family="FUTURES",
                role="MAKER",
                market="BTC_PERP",
                rpi=True,
            )

    def test_fee_schedule_rejects_provider_error_and_invalid_percentage(self):
        base = {
            "error": None,
            "taker": "0.1",
            "maker": "0.1",
            "futures_taker": "0.035",
            "futures_maker": "0.01",
            "rpi_maker_fee_premium": None,
            "futures_rpi_maker_fee_premium": None,
            "custom_fee": {},
        }
        errored = dict(base)
        errored["error"] = "provider-error"
        with self.assertRaisesRegex(WhiteBitAdapterError, "provider error"):
            parse_fee_schedule(
                errored,
                evidence_id="fee:error",
                observed_at=NOW,
            )
        invalid = dict(base)
        invalid["maker"] = "101"
        with self.assertRaisesRegex(WhiteBitAdapterError, "between 0 and 100"):
            parse_fee_schedule(
                invalid,
                evidence_id="fee:invalid",
                observed_at=NOW,
            )

    def test_market_fee_request_is_network_free(self):
        request = market_fee_request()
        self.assertEqual(request.endpoint, "/api/v4/market/fee")
        self.assertEqual(dict(request.body), {})

    def test_funding_history_preserves_signed_cash_effect_without_fake_event_id(self):
        page = parse_funding_page(
            {
                "records": [
                    {
                        "market": "BTC_PERP",
                        "fundingTime": "1734451200",
                        "fundingRate": "0.00017674",
                        "fundingAmount": "-0.171053531892",
                        "positionAmount": "0.019",
                        "settlementPrice": "50938.2",
                        "rateCalculatedTime": "1734364800",
                    }
                ],
                "limit": 100,
                "offset": 0,
            },
            expected_offset=0,
            expected_limit=100,
        )
        self.assertEqual(len(page.records), 1)
        observation = page.records[0]
        self.assertEqual(
            observation.funding_amount,
            Decimal("-0.171053531892"),
        )
        self.assertEqual(
            observation.position_amount,
            Decimal("0.019"),
        )
        self.assertTrue(
            observation.observation_fingerprint.startswith("sha256:")
        )
        self.assertFalse(
            observation.economic_event_identity_authoritative
        )
        self.assertTrue(page.short_page)

    def test_funding_fingerprint_is_deterministic_but_changes_with_correction(self):
        record = {
            "market": "BTC_PERP",
            "fundingTime": "1734451200",
            "fundingRate": "0.00017674",
            "fundingAmount": "-0.171053531892",
            "positionAmount": "0.019",
            "settlementPrice": "50938.2",
            "rateCalculatedTime": "1734364800",
        }
        first = parse_funding_page(
            {"records": [record], "limit": 100, "offset": 0},
            expected_offset=0,
            expected_limit=100,
        ).records[0]
        second = parse_funding_page(
            {"records": [dict(record)], "limit": 100, "offset": 0},
            expected_offset=0,
            expected_limit=100,
        ).records[0]
        self.assertEqual(
            first.observation_fingerprint,
            second.observation_fingerprint,
        )
        corrected = dict(record)
        corrected["fundingAmount"] = "-0.17"
        changed = parse_funding_page(
            {"records": [corrected], "limit": 100, "offset": 0},
            expected_offset=0,
            expected_limit=100,
        ).records[0]
        self.assertNotEqual(
            first.observation_fingerprint,
            changed.observation_fingerprint,
        )

    def test_funding_page_must_match_requested_pagination(self):
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "pagination does not match",
        ):
            parse_funding_page(
                {"records": [], "limit": 50, "offset": 100},
                expected_offset=0,
                expected_limit=50,
            )
        request = funding_history_request(
            market="btc_perp",
            offset=100,
            limit=50,
        )
        self.assertEqual(
            request.endpoint,
            "/api/v4/collateral-account/funding-history",
        )
        self.assertEqual(
            dict(request.body),
            {"market": "BTC_PERP", "offset": 100, "limit": 50},
        )

    def test_funding_rate_calculation_time_cannot_follow_payment(self):
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "cannot follow",
        ):
            parse_funding_page(
                {
                    "records": [
                        {
                            "market": "BTC_PERP",
                            "fundingTime": "1734451200",
                            "fundingRate": "0.0001",
                            "fundingAmount": "-0.1",
                            "positionAmount": "0.01",
                            "settlementPrice": "50000",
                            "rateCalculatedTime": "1734451201",
                        }
                    ],
                    "limit": 100,
                    "offset": 0,
                },
                expected_offset=0,
                expected_limit=100,
            )

    def test_private_signer_uses_exact_body_and_caller_owned_nonce(self):
        nonce = 1_790_280_000_123
        signed = sign_private_request(
            endpoint="/api/v4/order/new",
            parameters={
                "market": "BTC_USDT",
                "side": "buy",
                "amount": "0.01",
            },
            nonce=nonce,
            api_key="public-key",
            api_secret="private-secret",
            nonce_window=True,
            server_time_ms=nonce + 1000,
        )
        body = json.loads(signed.body.decode("utf-8"))
        self.assertEqual(body["request"], "/api/v4/order/new")
        self.assertEqual(body["nonce"], nonce)
        self.assertIs(body["nonceWindow"], True)

        encoded = base64.b64encode(signed.body)
        expected = hmac.new(
            b"private-secret",
            encoded,
            hashlib.sha512,
        ).hexdigest()
        self.assertEqual(signed.headers["X-TXC-PAYLOAD"], encoded.decode("ascii"))
        self.assertEqual(signed.headers["X-TXC-SIGNATURE"], expected)

        safe = signed.safe_debug()
        self.assertEqual(safe["headers"]["X-TXC-APIKEY"], "<redacted>")
        self.assertEqual(safe["headers"]["X-TXC-PAYLOAD"], "<redacted>")
        self.assertEqual(safe["headers"]["X-TXC-SIGNATURE"], "<redacted>")

    def test_nonce_window_requires_current_server_time_evidence(self):
        with self.assertRaisesRegex(WhiteBitAdapterError, "server_time_ms"):
            sign_private_request(
                endpoint="/api/v4/orders",
                parameters={},
                nonce=1_790_280_000_000,
                api_key="key",
                api_secret="secret",
                nonce_window=True,
            )
        with self.assertRaisesRegex(WhiteBitAdapterError, "5 second"):
            sign_private_request(
                endpoint="/api/v4/orders",
                parameters={},
                nonce=1_790_280_000_000,
                api_key="key",
                api_secret="secret",
                nonce_window=True,
                server_time_ms=1_790_280_010_001,
            )

    def test_private_signer_rejects_nonce_conflict_and_binary_float(self):
        with self.assertRaisesRegex(WhiteBitAdapterError, "conflicts"):
            sign_private_request(
                endpoint="/api/v4/orders",
                parameters={"nonce": 9},
                nonce=10,
                api_key="key",
                api_secret="secret",
            )
        with self.assertRaisesRegex(WhiteBitAdapterError, "binary float"):
            sign_private_request(
                endpoint="/api/v4/order/new",
                parameters={"amount": 0.01},
                nonce=10,
                api_key="key",
                api_secret="secret",
            )

    def test_recursive_debug_redaction_covers_nested_auth_fields(self):
        redacted = redact_whitebit_debug(
            {
                "headers": {
                    "X-TXC-APIKEY": "key",
                    "X-TXC-PAYLOAD": "payload",
                    "X-TXC-SIGNATURE": "signature",
                    "Content-Type": "application/json",
                },
                "nested": [{"api_secret": "secret", "market": "BTC_USDT"}],
            }
        )
        self.assertEqual(redacted["headers"]["X-TXC-APIKEY"], "<redacted>")
        self.assertEqual(redacted["nested"][0]["api_secret"], "<redacted>")
        self.assertEqual(redacted["nested"][0]["market"], "BTC_USDT")

    def test_absence_builder_uses_surface_typed_complete_coverages(self):
        open_orders = open_order_coverage()
        order_history = order_history_coverage()
        executions = execution_history_coverage()
        open_orders.add_page(
            WhiteBitPageEvidence(offset=0, limit=100, record_count=0)
        )
        order_history.add_page(
            WhiteBitPageEvidence(offset=0, limit=500, record_count=0)
        )
        executions.add_page(
            WhiteBitPageEvidence(offset=0, limit=500, record_count=0)
        )
        evidence = absence_evidence_from_coverages(
            order_found=False,
            open_orders=open_orders,
            order_history=order_history,
            executions=executions,
            activities_complete=True,
            consistency_horizon_satisfied=True,
        )
        self.assertEqual(evidence.verdict(), "INCONCLUSIVE")
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "cannot self-assert provider exclusion semantics",
        ):
            absence_evidence_from_coverages(
                order_found=False,
                open_orders=open_orders,
                order_history=order_history,
                executions=executions,
                activities_complete=True,
                consistency_horizon_satisfied=True,
                qualified_exclusion_semantics=True,
            )

    def test_absence_builder_stays_inconclusive_on_full_nonterminal_page(self):
        open_orders = open_order_coverage()
        order_history = order_history_coverage()
        executions = execution_history_coverage()
        open_orders.add_page(
            WhiteBitPageEvidence(offset=0, limit=100, record_count=100)
        )
        order_history.add_page(
            WhiteBitPageEvidence(offset=0, limit=500, record_count=0)
        )
        executions.add_page(
            WhiteBitPageEvidence(offset=0, limit=500, record_count=0)
        )
        evidence = absence_evidence_from_coverages(
            order_found=False,
            open_orders=open_orders,
            order_history=order_history,
            executions=executions,
            activities_complete=True,
            consistency_horizon_satisfied=True,
        )
        self.assertEqual(evidence.verdict(), "INCONCLUSIVE")

    def test_absence_builder_rejects_swapped_surface_evidence(self):
        open_orders = open_order_coverage()
        order_history = order_history_coverage()
        executions = execution_history_coverage()
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "coverage surface mismatch",
        ):
            absence_evidence_from_coverages(
                order_found=False,
                open_orders=order_history,
                order_history=open_orders,
                executions=executions,
                activities_complete=True,
                consistency_horizon_satisfied=True,
            )

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

    def test_complete_surfaces_do_not_self_qualify_provider_absence(self):
        evidence = WhiteBitAbsenceEvidence(
            order_found=False,
            open_orders_complete=True,
            order_history_complete=True,
            executions_complete=True,
            activities_complete=True,
            consistency_horizon_satisfied=True,
        )
        self.assertEqual(evidence.verdict(), "INCONCLUSIVE")
        with self.assertRaisesRegex(
            WhiteBitAdapterError,
            "cannot self-assert provider exclusion semantics",
        ):
            WhiteBitAbsenceEvidence(
                order_found=False,
                open_orders_complete=True,
                order_history_complete=True,
                executions_complete=True,
                activities_complete=True,
                consistency_horizon_satisfied=True,
                qualified_exclusion_semantics=True,
            )
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
