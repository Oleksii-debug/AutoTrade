from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.bybit_v5 import (
    BybitPreparedRequest,
    build_order_payload,
    coverage_evidence,
    parse_executions,
    parse_submission_response,
    prepare_order_request,
    server_time_from_response,
    validate_auth_timestamp,
)
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.provider_core import (
    ProviderCoreError,
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)


READ_AT = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def read_capability(*, account_id="paper-1", environment="PAPER", instrument_version="BTCUSDT@v1"):
    observed_at = READ_AT - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BYBIT",
            account_id=account_id,
            entity_id="bybit-reconciliation",
            environment=environment,
            instrument_version=instrument_version,
            observed_at=observed_at,
            expires_at=READ_AT + timedelta(hours=1),
            supported_order_types=frozenset({"LIMIT", "MARKET"}),
            time_in_force=frozenset({"GTC", "IOC"}),
            permission_scopes=frozenset({"ORDER.READ"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="bybit-read-test",
            data_entitlements=frozenset({"EXECUTIONS"}),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "a" * 64,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                "source_uri": "https://bybit-exchange.github.io/docs/v5/order/execution",
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=READ_AT,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def bound_execution_response(
    response,
    *,
    account_id="paper-1",
    environment="PAPER",
    instrument_version="BTCUSDT@v1",
):
    query = prepare_authenticated_read_query(
        capability=read_capability(
            account_id=account_id,
            environment=environment,
            instrument_version=instrument_version,
        ),
        surface=Surface.AUTHENTICATED_READ,
        endpoint="/v5/execution/list",
        query={"category": "spot", "limit": "100"},
        at=READ_AT,
        permission_scope="ORDER.READ",
    )
    raw = json.dumps(
        response,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return observe_authenticated_json_response(
        query_binding=query,
        response_bytes=raw,
        observed_at=READ_AT,
    )


def write_capability(
    *,
    account_id="bybit-account",
    environment="MAINNET",
    instrument_version="BTCUSDT@v1",
):
    observed_at = READ_AT - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BYBIT",
            account_id=account_id,
            entity_id="bybit-trading",
            environment=environment,
            instrument_version=instrument_version,
            observed_at=observed_at,
            expires_at=READ_AT + timedelta(hours=1),
            supported_order_types=frozenset({"LIMIT", "MARKET"}),
            time_in_force=frozenset({"GTC", "IOC", "POST_ONLY", "FOK"}),
            permission_scopes=frozenset({"ORDER_WRITE"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="bybit-write-test",
            data_entitlements=frozenset(),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "b" * 64,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                "source_uri": "https://bybit-exchange.github.io/docs/v5/order/create-order",
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=READ_AT,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def prepared_bybit_request(
    client_order_id: str,
    *,
    environment="MAINNET",
) -> BybitPreparedRequest:
    return prepare_order_request(
        capability=write_capability(environment=environment),
        account_id="bybit-account",
        environment=environment,
        instrument_version="BTCUSDT@v1",
        at=READ_AT,
        product_family="SPOT",
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity="0.01",
        client_order_id=client_order_id,
        time_in_force="IOC",
    )


def bybit_response_bytes(payload) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class BybitV5AdapterTests(unittest.TestCase):
    def test_spot_market_quantity_is_explicitly_base_coin(self):
        payload = build_order_payload(
            product_family="SPOT",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.0100",
            client_order_id="order_1",
            time_in_force="IOC",
        )
        self.assertEqual(payload["category"], "spot")
        self.assertEqual(payload["qty"], "0.01")
        self.assertEqual(payload["marketUnit"], "baseCoin")
        self.assertEqual(payload["isLeverage"], 0)
        self.assertNotIn("price", payload)

    def test_margin_is_explicit_and_limit_price_is_exact(self):
        payload = build_order_payload(
            product_family="MARGIN",
            symbol="ETHUSDT",
            side="SELL",
            order_type="LIMIT",
            quantity=Decimal("2.500"),
            price="3456.7000",
            client_order_id="margin-1",
            time_in_force="POST_ONLY",
        )
        self.assertEqual(payload["isLeverage"], 1)
        self.assertEqual(payload["qty"], "2.5")
        self.assertEqual(payload["price"], "3456.7")
        self.assertEqual(payload["timeInForce"], "PostOnly")

    def test_derivative_scope_maps_reduce_only_and_position_mode(self):
        payload = build_order_payload(
            product_family="LINEAR_DERIVATIVES",
            symbol="BTCUSDT",
            side="SELL",
            order_type="LIMIT",
            quantity="1",
            price="70000",
            client_order_id="reduce-1",
            time_in_force="GTC",
            reduce_only=True,
            position_idx=2,
        )
        self.assertEqual(payload["category"], "linear")
        self.assertTrue(payload["reduceOnly"])
        self.assertEqual(payload["positionIdx"], 2)

    def test_unsafe_or_ambiguous_request_shapes_fail_closed(self):
        common = dict(
            product_family="SPOT",
            symbol="BTCUSDT",
            side="BUY",
            client_order_id="safe-id",
        )
        with self.assertRaisesRegex(ProviderCoreError, "IOC"):
            build_order_payload(
                **common,
                order_type="MARKET",
                quantity="1",
                time_in_force="GTC",
            )
        with self.assertRaisesRegex(ProviderCoreError, "ignored"):
            build_order_payload(
                **common,
                order_type="MARKET",
                quantity="1",
                price="1",
                time_in_force="IOC",
            )
        with self.assertRaisesRegex(ProviderCoreError, "exact decimal"):
            build_order_payload(
                **common,
                order_type="LIMIT",
                quantity=0.1,
                price="1",
                time_in_force="GTC",
            )
        with self.assertRaisesRegex(ProviderCoreError, "1-36"):
            build_order_payload(
                **{**common, "client_order_id": "bad id"},
                order_type="LIMIT",
                quantity="1",
                price="1",
                time_in_force="GTC",
            )

    def test_success_response_is_acknowledgement_not_fill(self):
        attempt = str(uuid4())
        response = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "orderId": "provider-123",
                "orderLinkId": "client-123",
            },
            "retExtInfo": {},
            "time": 1790280000123,
        }
        result = parse_submission_response(
            attempt_id=attempt,
            prepared_request=prepared_bybit_request("client-123"),
            response_bytes=bybit_response_bytes(response),
        )
        self.assertEqual(result["outcome"], "ACKNOWLEDGED")
        self.assertEqual(result["retry_disposition"], "NEVER")
        self.assertEqual(result["provider_order_id"], "provider-123")
        self.assertNotIn("fill", repr(result).lower())
        self.assertTrue(result["evidence"][0]["sha256"].startswith("sha256:"))

    def test_response_evidence_is_bound_to_prepared_environment(self):
        base = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"orderId": "provider-env", "orderLinkId": "client-env"},
            "time": 1790280000123,
        }
        expected = {
            "MAINNET": "https://api.bybit.com/v5/order/create",
            "TESTNET": "https://api-testnet.bybit.com/v5/order/create",
            "DEMO": "https://api-demo.bybit.com/v5/order/create",
        }
        for environment, source_uri in expected.items():
            with self.subTest(environment=environment):
                result = parse_submission_response(
                    attempt_id=str(uuid4()),
                    prepared_request=prepared_bybit_request(
                        "client-env",
                        environment=environment,
                    ),
                    response_bytes=bybit_response_bytes(base),
                )
                evidence = result["evidence"][0]
                self.assertNotIn("provider_environment", evidence)
                self.assertEqual(evidence["source_uri"], source_uri)

    def test_explicit_observation_time_is_validated_and_normalized_to_utc(self):
        accepted = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=prepared_bybit_request("client-time"),
            response_bytes=bybit_response_bytes(
                {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "orderId": "provider-time",
                        "orderLinkId": "client-time",
                    },
                }
            ),
            observed_at="2026-09-24T22:00:00+02:00",
        )
        self.assertNotIn("provider_received_at", accepted)
        self.assertEqual(
            accepted["evidence"][0]["observed_at"],
            "2026-09-24T20:00:00Z",
        )
        provider_timed = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=prepared_bybit_request("client-provider-time"),
            response_bytes=bybit_response_bytes(
                {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "orderId": "provider-time-2",
                        "orderLinkId": "client-provider-time",
                    },
                    "time": 1790280000123,
                }
            ),
            observed_at="2026-09-24T21:00:00Z",
        )
        self.assertEqual(
            provider_timed["provider_received_at"],
            "2026-09-24T20:00:00.123Z",
        )
        self.assertEqual(
            provider_timed["evidence"][0]["observed_at"],
            "2026-09-24T21:00:00Z",
        )
        with self.assertRaisesRegex(ProviderCoreError, "timezone"):
            parse_submission_response(
                attempt_id=str(uuid4()),
                prepared_request=prepared_bybit_request("client-bad-time"),
                response_bytes=bybit_response_bytes(
                    {
                        "retCode": 0,
                        "result": {
                            "orderId": "provider-bad-time",
                            "orderLinkId": "client-bad-time",
                        },
                    }
                ),
                observed_at="2026-09-24T20:00:00",
            )

    def test_transport_loss_after_possible_write_is_unknown(self):
        result = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=prepared_bybit_request("client-transport-loss"),
            response_bytes=None,
            observed_at="2026-09-24T20:00:00Z",
            transport_ambiguous=True,
        )
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertEqual(result["retry_disposition"], "RECONCILE_FIRST")
        self.assertEqual(result["reason_code"], "BYBIT_TRANSPORT_AMBIGUOUS")
        self.assertNotIn("provider_received_at", result)
        self.assertEqual(result["evidence"], [])

    def test_transport_ambiguity_cannot_coexist_with_response_bytes(self):
        with self.assertRaisesRegex(ProviderCoreError, "response bytes"):
            parse_submission_response(
                attempt_id=str(uuid4()),
                prepared_request=prepared_bybit_request("client-contradiction"),
                response_bytes=bybit_response_bytes({"retCode": 0}),
                observed_at="2026-09-24T20:00:00Z",
                transport_ambiguous=True,
            )

    def test_missing_response_requires_explicit_ambiguity_and_timestamp(self):
        with self.assertRaisesRegex(ProviderCoreError, "response_bytes"):
            parse_submission_response(
                attempt_id=str(uuid4()),
                prepared_request=prepared_bybit_request("client-missing"),
                response_bytes=None,
                observed_at="2026-09-24T20:00:00Z",
            )
        with self.assertRaisesRegex(ProviderCoreError, "observed_at"):
            parse_submission_response(
                attempt_id=str(uuid4()),
                prepared_request=prepared_bybit_request("client-missing-time"),
                response_bytes=None,
                transport_ambiguous=True,
            )

    def test_ambiguous_bybit_codes_require_reconciliation(self):
        for code in (429, 10000, 10014, 10016):
            with self.subTest(code=code):
                client = f"client-{code}"
                result = parse_submission_response(
                    attempt_id=str(uuid4()),
                    prepared_request=prepared_bybit_request(client),
                    response_bytes=bybit_response_bytes(
                        {
                            "retCode": code,
                            "retMsg": "ambiguous",
                            "result": {},
                            "retExtInfo": {},
                            "time": 1790280000123,
                        }
                    ),
                )
                self.assertEqual(result["outcome"], "UNKNOWN")
                self.assertEqual(result["retry_disposition"], "RECONCILE_FIRST")

    def test_explicit_parameter_error_is_rejected(self):
        result = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=prepared_bybit_request("client-reject"),
            response_bytes=bybit_response_bytes(
                {
                    "retCode": 10001,
                    "retMsg": "parameter error",
                    "result": {},
                    "retExtInfo": {},
                    "time": 1790280000123,
                }
            ),
        )
        self.assertEqual(result["outcome"], "REJECTED")
        self.assertEqual(result["retry_disposition"], "NEVER")

    def test_success_response_must_echo_exact_guarded_client_identity(self):
        with self.assertRaisesRegex(ProviderCoreError, "guarded request"):
            parse_submission_response(
                attempt_id=str(uuid4()),
                prepared_request=prepared_bybit_request("expected"),
                response_bytes=bybit_response_bytes(
                    {
                        "retCode": 0,
                        "retMsg": "OK",
                        "result": {
                            "orderId": "provider-1",
                            "orderLinkId": "other",
                        },
                        "time": 1790280000123,
                    }
                ),
            )

    def test_submission_evidence_binds_request_response_and_attempt_identity(self):
        request = prepared_bybit_request("client-bind")
        payload = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "orderId": "provider-bind",
                "orderLinkId": "client-bind",
            },
            "time": 1790280000123,
        }
        raw_a = bybit_response_bytes(payload)
        raw_b = json.dumps(payload, indent=1).encode("utf-8")
        attempt = str(uuid4())
        a = parse_submission_response(
            attempt_id=attempt,
            prepared_request=request,
            response_bytes=raw_a,
        )
        b = parse_submission_response(
            attempt_id=attempt,
            prepared_request=request,
            response_bytes=raw_b,
        )
        other_attempt = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=request,
            response_bytes=raw_a,
        )
        self.assertNotEqual(a["evidence"][0]["sha256"], b["evidence"][0]["sha256"])
        self.assertNotEqual(
            a["evidence"][0]["artifact_id"],
            b["evidence"][0]["artifact_id"],
        )
        self.assertNotEqual(
            a["evidence"][0]["artifact_id"],
            other_attempt["evidence"][0]["artifact_id"],
        )

    def test_execution_rows_preserve_provider_identity_and_exact_economics(self):
        response = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"list": [
                {
                    "execId": "exec-1", "orderLinkId": "client-1", "symbol": "BTCUSDT",
                    "execQty": "0.25", "execPrice": "65000.10", "execFee": "1.23",
                    "feeCurrency": "USDT", "execTime": "1790280000123",
                },
                {
                    "execId": "exec-1", "orderLinkId": "client-1", "symbol": "BTCUSDT",
                    "execQty": "0.25", "execPrice": "65000.10", "execFee": "1.23",
                    "feeCurrency": "USDT", "execTime": "1790280000123",
                },
            ]},
            "time": 1790280001000,
        }
        fills = parse_executions(
            bound_execution_response(response),
            instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
        )
        self.assertEqual(len(fills), 1)
        fill = fills[0]
        self.assertEqual((fill.account_id, fill.environment), ("paper-1", "PAPER"))
        self.assertEqual(fill.provider_execution_id, "exec-1")
        self.assertEqual(fill.instrument, "BTCUSDT@v1")
        self.assertEqual(fill.quantity, Decimal("0.25"))
        self.assertEqual(fill.price, Decimal("65000.10"))
        self.assertEqual(fill.fee_amount, Decimal("1.23"))
        self.assertEqual(fill.trade_time, "2026-09-24T20:00:00.123Z")

    def test_execution_scope_is_derived_from_prepared_read_not_parser_labels(self):
        response = {"retCode": 0, "result": {"list": [{
            "execId": "scope-1", "orderLinkId": "", "symbol": "BTCUSDT",
            "execQty": "1", "execPrice": "10", "execFee": "0",
            "feeCurrency": "USDT", "execTime": "1790280000000",
        }]}}
        observation = bound_execution_response(response, account_id="account-a")
        fills = parse_executions(
            observation,
            instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
        )
        self.assertEqual((fills[0].account_id, fills[0].environment), ("account-a", "PAPER"))
        self.assertEqual(observation.query_binding.account_id, "account-a")

    def test_documented_linear_execution_requires_qualified_fee_currency(self):
        response = {"retCode": 0, "result": {"category": "linear", "list": [{
            "execId": "e0cbe81d-0f18-5866-9415-cf319b5dab3b", "orderLinkId": "",
            "symbol": "ETHPERP", "execQty": "0.1", "execPrice": "1190.15",
            "execFee": "0.071409", "feeCurrency": "", "extraFees": "",
            "execTime": "1672282722429",
        }]}}
        evidence = bound_execution_response(response, instrument_version="ETHPERP@v1")
        with self.assertRaisesRegex(ProviderCoreError, "fee currency is unresolved"):
            parse_executions(evidence, instrument_versions={"ETHPERP": "ETHPERP@v1"})
        fills = parse_executions(
            evidence,
            instrument_versions={"ETHPERP": "ETHPERP@v1"},
            qualified_fee_currencies={"ETHPERP@v1": "USDT"},
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].fee_amount, Decimal("0.071409"))
        self.assertEqual(fills[0].fee_currency, "USDT")

    def test_nonempty_extra_fees_cannot_silently_disappear(self):
        response = {"retCode": 0, "result": {"category": "spot", "list": [{
            "execId": "exec-extra-fee", "orderLinkId": "", "symbol": "BTCUSDT",
            "execQty": "0.01", "execPrice": "65000", "execFee": "0.5",
            "feeCurrency": "USDT",
            "extraFees": '[{"feeType":"tax","subFeeType":"regional"}]',
            "execTime": "1790280000000",
        }]}}
        with self.assertRaisesRegex(ProviderCoreError, "extraFees"):
            parse_executions(
                bound_execution_response(response),
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )

    def test_empty_extra_fee_shapes_remain_economically_complete(self):
        for extra_fees in (None, "", [], {}):
            with self.subTest(extra_fees=extra_fees):
                row = {
                    "execId": f"exec-{repr(extra_fees)}", "orderLinkId": "",
                    "symbol": "BTCUSDT", "execQty": "0.01", "execPrice": "65000",
                    "execFee": "0.5", "feeCurrency": "USDT", "execTime": "1790280000000",
                }
                if extra_fees is not None:
                    row["extraFees"] = extra_fees
                fills = parse_executions(
                    bound_execution_response({"retCode": 0, "result": {"list": [row]}}),
                    instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
                )
                self.assertEqual(fills[0].fee_currency, "USDT")

    def test_execution_conflict_and_unknown_symbol_fail_closed(self):
        conflict = {"retCode": 0, "result": {"list": [
            {"execId": "same", "orderLinkId": "", "symbol": "BTCUSDT", "execQty": "1", "execPrice": "10", "execFee": "0", "feeCurrency": "USDT", "execTime": "1790280000000"},
            {"execId": "same", "orderLinkId": "", "symbol": "BTCUSDT", "execQty": "2", "execPrice": "10", "execFee": "0", "feeCurrency": "USDT", "execTime": "1790280000000"},
        ]}}
        with self.assertRaisesRegex(ProviderCoreError, "conflicting"):
            parse_executions(
                bound_execution_response(conflict),
                instrument_versions={"BTCUSDT": "BTCUSDT@v1"},
            )
        unknown = {"retCode": 0, "result": {"list": [{
            "execId": "x", "orderLinkId": "", "symbol": "UNKNOWN", "execQty": "1",
            "execPrice": "10", "execFee": "0", "feeCurrency": "USD",
            "execTime": "1790280000000",
        }]}}
        with self.assertRaisesRegex(ProviderCoreError, "unmapped"):
            parse_executions(bound_execution_response(unknown), instrument_versions={})

    def test_auth_timestamp_window_matches_documented_boundaries(self):
        server = 1_000_000
        validate_auth_timestamp(
            request_timestamp_ms=995_000,
            server_time_ms=server,
            recv_window_ms=5000,
        )
        validate_auth_timestamp(
            request_timestamp_ms=1_000_999,
            server_time_ms=server,
            recv_window_ms=5000,
        )
        with self.assertRaisesRegex(ProviderCoreError, "authentication window"):
            validate_auth_timestamp(
                request_timestamp_ms=994_999,
                server_time_ms=server,
                recv_window_ms=5000,
            )
        with self.assertRaisesRegex(ProviderCoreError, "authentication window"):
            validate_auth_timestamp(
                request_timestamp_ms=1_001_000,
                server_time_ms=server,
                recv_window_ms=5000,
            )

    def test_server_time_uses_provider_nanosecond_value_without_float_rounding(self):
        result = server_time_from_response(
            {
                "retCode": 0,
                "result": {
                    "timeSecond": "1790280000",
                    "timeNano": "1790280000123456789",
                },
            }
        )
        self.assertEqual(result, "2026-09-24T20:00:00.123456Z")

    def test_absence_semantics_are_fail_closed_until_explicitly_qualified(self):
        evidence = coverage_evidence(
            surface="EXECUTIONS",
            coverage_start="2026-09-24T19:00:00Z",
            coverage_end="2026-09-24T21:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
        
            account_id="paper-1",
            environment="PAPER",)
        self.assertFalse(evidence.provider_semantics_exclude_execution)
        self.assertFalse(
            evidence.proves_absence_for(
                datetime(2026, 9, 24, 20, tzinfo=timezone.utc)
            )
        )

        qualified = coverage_evidence(
            surface="EXECUTIONS",
            coverage_start="2026-09-24T19:00:00Z",
            coverage_end="2026-09-24T21:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
            qualified_exclusion_semantics=True,
        
            account_id="paper-1",
            environment="PAPER",)
        self.assertTrue(
            qualified.proves_absence_for(
                datetime(2026, 9, 24, 20, tzinfo=timezone.utc)
            )
        )


if __name__ == "__main__":
    unittest.main()
