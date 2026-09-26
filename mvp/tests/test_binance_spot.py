from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.binance_spot import (
    BinanceSpotAdapterError,
    BinanceSpotOrderIntent,
    BinanceSpotReferencePrice,
    BinanceSpotSymbolRules,
    coverage_evidence,
    parse_account_trades,
    parse_order_ack,
    prepare_order_request,
)
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)
from mvp.autotrade_mvp.accounting import ScopedEconomicBook
from mvp.autotrade_mvp.fill_accounting import (
    ProjectedFillEvidence,
    build_provider_fill_financial_plan,
)
from mvp.autotrade_mvp.reservations import ReservationSnapshot
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)


NOW = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def execution_observation(
    rows,
    *,
    account_id="paper-1",
    environment="PAPER",
    instrument_version="BTCUSDT:v1",
    surface=Surface.ACTIVITIES,
):
    query = prepare_authenticated_read_query(
        capability=capability(
            account_id=account_id,
            environment=environment,
            instrument_version=instrument_version,
        ),
        surface=surface,
        endpoint="/api/v3/myTrades",
        query={"symbol": "BTCUSDT"},
        at=NOW,
        permission_scope="TRADE.READ",
    )
    raw = json.dumps(
        rows,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return observe_authenticated_json_response(
        query_binding=query,
        http_status=200,
        response_bytes=raw,
        observed_at=NOW,
    )

def capability(
    *,
    order_types=("LIMIT", "MARKET"),
    tif=("GTC", "IOC", "FOK", "NONE"),
    account_id="account-1",
    environment="PAPER",
    instrument_version="BTCUSDT:v1",
):
    observed_at = NOW - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BINANCE",
            account_id=account_id,
            entity_id="global",
            environment=environment,
            instrument_version=instrument_version,
            observed_at=observed_at,
            expires_at=NOW + timedelta(hours=1),
            supported_order_types=frozenset(order_types),
            time_in_force=frozenset(tif),
            permission_scopes=frozenset({"ORDER_WRITE", "ORDER.READ", "TRADE.READ"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="binance-spot-foundation",
            data_entitlements=frozenset({"ORDERS", "TRADES"}),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "b" * 64,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                "source_uri": "https://developers.binance.com/en/docs/products/spot",
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


def symbol_rules(
    *,
    instrument_version="BTCUSDT:v1",
    symbol="BTCUSDT",
    min_qty="0.001",
    max_qty="100",
    step_size="0.001",
    min_price="0.01",
    max_price="1000000",
    tick_size="0.01",
    min_notional="5",
    apply_to_market=True,
    avg_price_mins=5,
):
    return BinanceSpotSymbolRules.from_exchange_info(
        instrument_version=instrument_version,
        symbol_payload={
            "symbol": symbol,
            "filters": [
                {
                    "filterType": "PRICE_FILTER",
                    "minPrice": min_price,
                    "maxPrice": max_price,
                    "tickSize": tick_size,
                },
                {
                    "filterType": "LOT_SIZE",
                    "minQty": min_qty,
                    "maxQty": max_qty,
                    "stepSize": step_size,
                },
                {
                    "filterType": "MARKET_LOT_SIZE",
                    "minQty": min_qty,
                    "maxQty": max_qty,
                    "stepSize": step_size,
                },
                {
                    "filterType": "MIN_NOTIONAL",
                    "minNotional": min_notional,
                    "applyToMarket": apply_to_market,
                    "avgPriceMins": avg_price_mins,
                },
            ],
        },
    )


def reference_price(
    *,
    instrument_version="BTCUSDT:v1",
    symbol="BTCUSDT",
    price="40000",
    observed_at=NOW - timedelta(seconds=1),
    avg_price_mins=5,
):
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = observed_at - epoch
    timestamp_ms = (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )
    if avg_price_mins == 0:
        return BinanceSpotReferencePrice.from_last_trade_payload(
            instrument_version=instrument_version,
            symbol=symbol,
            payload={
                "price": price,
                "time": timestamp_ms,
            },
        )
    return BinanceSpotReferencePrice.from_average_price_payload(
        instrument_version=instrument_version,
        symbol=symbol,
        payload={
            "mins": avg_price_mins,
            "price": price,
            "closeTime": timestamp_ms,
        },
    )


def provider_reference(
    *,
    instrument_version="BTCUSDT:v1",
    symbol="BTCUSDT",
    price="40000",
    observed_at=NOW - timedelta(seconds=1),
):
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = observed_at - epoch
    timestamp_ms = (
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000
    )
    return BinanceSpotReferencePrice.from_reference_price_payload(
        instrument_version=instrument_version,
        symbol=symbol,
        payload={
            "symbol": symbol,
            "referencePrice": price,
            "timestamp": timestamp_ms,
        },
    )


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
            symbol_rules=symbol_rules(),
            at=NOW,
        )
        self.assertEqual(request.endpoint, "/api/v3/order")
        self.assertEqual(request.body["quantity"], "0.0100")
        self.assertEqual(request.body["price"], "40000.2500")
        self.assertEqual(request.body["newOrderRespType"], "ACK")
        self.assertRegex(request.filter_source_sha256, r"^sha256:[0-9a-f]{64}$")
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
        reference = reference_price()
        request = prepare_order_request(
            intent,
            client_order_id="at-market-1",
            capability=capability(),
            symbol_rules=symbol_rules(),
            at=NOW,
            reference_price_observation=provider_reference(price=None),
            market_reference=reference,
            maximum_market_reference_age_seconds=30,
        )
        self.assertEqual(request.body["quantity"], "0.5")
        self.assertNotIn("quoteOrderQty", request.body)
        self.assertNotIn("timeInForce", request.body)
        self.assertEqual(
            request.market_reference_source_sha256,
            reference.source_sha256,
        )
        self.assertEqual(request.market_reference_kind, "AVERAGE")
        self.assertEqual(request.market_reference_window_minutes, 5)
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
                symbol_rules=symbol_rules(),
                at=NOW,
            )

    def test_zero_price_tick_disables_tick_rule_without_disabling_bounds(self):
        rules = symbol_rules(
            min_price="1",
            max_price="100",
            tick_size="0",
            min_notional="0.001",
        )
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="0.001",
            price="1.23456789",
            time_in_force="GTC",
        )
        request = prepare_order_request(
            intent,
            client_order_id="at-filter-disabled-tick",
            capability=capability(),
            symbol_rules=rules,
            at=NOW,
        )
        self.assertEqual(request.body["price"], "1.23456789")

    def test_exchange_info_filters_reject_invalid_limit_before_send(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="0.0105",
            price="40000.255",
            time_in_force="GTC",
        )
        with self.assertRaisesRegex(BinanceSpotAdapterError, "quantity"):
            prepare_order_request(
                intent,
                client_order_id="at-filter-1",
                capability=capability(),
                symbol_rules=symbol_rules(),
                at=NOW,
            )

    def test_market_notional_filter_requires_causal_reference_price(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.001",
        )
        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "provider reference-price observation evidence",
        ):
            prepare_order_request(
                intent,
                client_order_id="at-filter-2",
                capability=capability(),
                symbol_rules=symbol_rules(min_notional="50"),
                at=NOW,
            )
        with self.assertRaisesRegex(BinanceSpotAdapterError, "notional"):
            prepare_order_request(
                intent,
                client_order_id="at-filter-3",
                capability=capability(),
                symbol_rules=symbol_rules(min_notional="50"),
                at=NOW,
                reference_price_observation=provider_reference(price=None),
                market_reference=reference_price(price="40000"),
                maximum_market_reference_age_seconds=30,
            )

    def test_market_reference_must_be_provider_evidence_and_fresh(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.01",
        )
        rules = symbol_rules(min_notional="5")

        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "provider reference-price evidence",
        ):
            rules.validate(
                intent,
                at=NOW,
                reference_price_observation=provider_reference(price=None),
                market_reference="40000",
                maximum_market_reference_age_seconds=30,
            )

        with self.assertRaisesRegex(BinanceSpotAdapterError, "stale"):
            prepare_order_request(
                intent,
                client_order_id="at-filter-stale-reference",
                capability=capability(),
                symbol_rules=rules,
                at=NOW,
                reference_price_observation=provider_reference(
                    price=None,
                    observed_at=NOW - timedelta(seconds=31),
                ),
                market_reference=reference_price(),
                maximum_market_reference_age_seconds=30,
            )

        with self.assertRaisesRegex(BinanceSpotAdapterError, "from the future"):
            prepare_order_request(
                intent,
                client_order_id="at-filter-future-reference",
                capability=capability(),
                symbol_rules=rules,
                at=NOW,
                reference_price_observation=provider_reference(
                    price=None,
                    observed_at=NOW + timedelta(seconds=1),
                ),
                market_reference=reference_price(),
                maximum_market_reference_age_seconds=30,
            )

    def test_market_reference_is_bound_to_exact_symbol_and_instrument(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.01",
        )
        with self.assertRaisesRegex(BinanceSpotAdapterError, "instrument version"):
            prepare_order_request(
                intent,
                client_order_id="at-filter-reference-instrument",
                capability=capability(),
                symbol_rules=symbol_rules(),
                at=NOW,
                reference_price_observation=provider_reference(
                    instrument_version="BTCUSDT:v2",
                    price=None,
                ),
                market_reference=reference_price(),
                maximum_market_reference_age_seconds=30,
            )
        with self.assertRaisesRegex(BinanceSpotAdapterError, "symbol"):
            prepare_order_request(
                intent,
                client_order_id="at-filter-reference-symbol",
                capability=capability(),
                symbol_rules=symbol_rules(),
                at=NOW,
                reference_price_observation=provider_reference(
                    symbol="ETHUSDT",
                    price=None,
                ),
                market_reference=reference_price(),
                maximum_market_reference_age_seconds=30,
            )

    def test_market_notional_reference_window_must_match_exchange_info(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="1",
        )
        rules = symbol_rules(min_notional="50", avg_price_mins=5)

        with self.assertRaisesRegex(BinanceSpotAdapterError, "averaging window"):
            prepare_order_request(
                intent,
                client_order_id="at-last-cannot-sub-average",
                capability=capability(),
                symbol_rules=rules,
                at=NOW,
                reference_price_observation=provider_reference(price=None),
                market_reference=reference_price(
                    price="60",
                    avg_price_mins=0,
                ),
                maximum_market_reference_age_seconds=30,
            )

        with self.assertRaisesRegex(BinanceSpotAdapterError, "notional"):
            prepare_order_request(
                intent,
                client_order_id="at-filter-required-window-average",
                capability=capability(),
                symbol_rules=rules,
                at=NOW,
                reference_price_observation=provider_reference(price=None),
                market_reference=reference_price(
                    price="40",
                    avg_price_mins=5,
                ),
                maximum_market_reference_age_seconds=30,
            )

        accepted = prepare_order_request(
            intent,
            client_order_id="at-filter-required-window-pass",
            capability=capability(),
            symbol_rules=rules,
            at=NOW,
            reference_price_observation=provider_reference(price=None),
            market_reference=reference_price(
                price="60",
                avg_price_mins=5,
            ),
            maximum_market_reference_age_seconds=30,
        )
        self.assertEqual(accepted.market_reference_kind, "AVERAGE")
        self.assertEqual(accepted.market_reference_window_minutes, 5)

    def test_zero_avg_price_mins_requires_last_price_evidence(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="1",
        )
        rules = symbol_rules(min_notional="50", avg_price_mins=0)

        with self.assertRaisesRegex(BinanceSpotAdapterError, "averaging window"):
            prepare_order_request(
                intent,
                client_order_id="at-average-cannot-sub-last",
                capability=capability(),
                symbol_rules=rules,
                at=NOW,
                reference_price_observation=provider_reference(price=None),
                market_reference=reference_price(
                    price="60",
                    avg_price_mins=5,
                ),
                maximum_market_reference_age_seconds=30,
            )

        accepted = prepare_order_request(
            intent,
            client_order_id="at-filter-last-price-pass",
            capability=capability(),
            symbol_rules=rules,
            at=NOW,
            reference_price_observation=provider_reference(price=None),
            market_reference=reference_price(
                price="60",
                avg_price_mins=0,
            ),
            maximum_market_reference_age_seconds=30,
        )
        self.assertEqual(accepted.market_reference_kind, "LAST")
        self.assertEqual(accepted.market_reference_window_minutes, 0)

    def test_provider_reference_price_precedes_average_fallback(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="1",
        )
        rules = symbol_rules(min_notional="50", avg_price_mins=5)

        with self.assertRaisesRegex(BinanceSpotAdapterError, "notional"):
            prepare_order_request(
                intent,
                client_order_id="at-provider-reference-wins-reject",
                capability=capability(),
                symbol_rules=rules,
                at=NOW,
                reference_price_observation=provider_reference(price="40"),
                market_reference=reference_price(
                    price="60",
                    avg_price_mins=5,
                ),
                maximum_market_reference_age_seconds=30,
            )

        provider = provider_reference(price="60")
        accepted = prepare_order_request(
            intent,
            client_order_id="at-provider-reference-wins-accept",
            capability=capability(),
            symbol_rules=rules,
            at=NOW,
            reference_price_observation=provider,
            market_reference=reference_price(
                price="40",
                avg_price_mins=5,
            ),
            maximum_market_reference_age_seconds=30,
        )
        self.assertEqual(accepted.market_reference_kind, "REFERENCE")
        self.assertIsNone(accepted.market_reference_window_minutes)
        self.assertEqual(
            accepted.reference_price_observation_source_sha256,
            provider.source_sha256,
        )
        self.assertEqual(
            accepted.market_reference_source_sha256,
            provider.source_sha256,
        )

    def test_null_provider_reference_permits_exact_avg_price_fallback_only(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="1",
        )
        rules = symbol_rules(min_notional="50", avg_price_mins=5)
        provider = provider_reference(price=None)
        fallback = reference_price(price="60", avg_price_mins=5)

        accepted = prepare_order_request(
            intent,
            client_order_id="at-null-reference-fallback",
            capability=capability(),
            symbol_rules=rules,
            at=NOW,
            reference_price_observation=provider,
            market_reference=fallback,
            maximum_market_reference_age_seconds=30,
        )
        self.assertEqual(accepted.market_reference_kind, "AVERAGE")
        self.assertEqual(accepted.market_reference_window_minutes, 5)
        self.assertEqual(
            accepted.reference_price_observation_source_sha256,
            provider.source_sha256,
        )
        self.assertEqual(
            accepted.market_reference_source_sha256,
            fallback.source_sha256,
        )
        self.assertNotEqual(provider.source_sha256, fallback.source_sha256)

        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "provider reference-price observation",
        ):
            prepare_order_request(
                intent,
                client_order_id="at-missing-reference-observation",
                capability=capability(),
                symbol_rules=rules,
                at=NOW,
                market_reference=fallback,
                maximum_market_reference_age_seconds=30,
            )

    def test_reference_price_endpoint_parser_binds_nullable_price_symbol_and_time(self):
        missing = provider_reference(price=None)
        self.assertEqual(missing.reference_kind, "REFERENCE")
        self.assertIsNone(missing.price)
        self.assertIsNone(missing.averaging_window_minutes)

        present = provider_reference(price="60000.25")
        self.assertEqual(present.price, Decimal("60000.25"))
        self.assertEqual(present.reference_kind, "REFERENCE")
        self.assertIsNone(present.averaging_window_minutes)

        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        timestamp_ms = int((NOW - epoch).total_seconds() * 1000)
        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "does not match requested symbol",
        ):
            BinanceSpotReferencePrice.from_reference_price_payload(
                instrument_version="BTCUSDT:v1",
                symbol="BTCUSDT",
                payload={
                    "symbol": "ETHUSDT",
                    "referencePrice": "60000",
                    "timestamp": timestamp_ms,
                },
            )
        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "fields are not canonical",
        ):
            BinanceSpotReferencePrice.from_reference_price_payload(
                instrument_version="BTCUSDT:v1",
                symbol="BTCUSDT",
                payload={
                    "symbol": "BTCUSDT",
                    "referencePrice": None,
                    "timestamp": timestamp_ms,
                    "unexpected": True,
                },
            )

    def test_exchange_info_avg_price_mins_is_strictly_validated(self):
        for invalid in (True, -1, "5"):
            with self.subTest(avg_price_mins=invalid):
                with self.assertRaisesRegex(
                    BinanceSpotAdapterError,
                    "avgPriceMins",
                ):
                    symbol_rules(avg_price_mins=invalid)

    def test_reference_price_cannot_be_forged_by_direct_construction(self):
        parsed = reference_price()
        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "canonical provider payload parsing",
        ):
            BinanceSpotReferencePrice(
                instrument_version=parsed.instrument_version,
                symbol=parsed.symbol,
                price=parsed.price,
                observed_at=parsed.observed_at,
                source_sha256=parsed.source_sha256,
                reference_kind=parsed.reference_kind,
                averaging_window_minutes=parsed.averaging_window_minutes,
            )

    def test_exchange_info_rules_are_bound_to_exact_instrument(self):
        intent = BinanceSpotOrderIntent.create(
            instrument_version="BTCUSDT:v2",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="0.01",
            price="40000.25",
            time_in_force="GTC",
        )
        with self.assertRaisesRegex(BinanceSpotAdapterError, "instrument version"):
            prepare_order_request(
                intent,
                client_order_id="at-filter-4",
                capability=capability(instrument_version="BTCUSDT:v2"),
                symbol_rules=symbol_rules(instrument_version="BTCUSDT:v1"),
                at=NOW,
            )

    def test_exchange_info_market_flags_must_be_real_booleans(self):
        with self.assertRaisesRegex(BinanceSpotAdapterError, "boolean"):
            symbol_rules(apply_to_market="false")

    def test_exchange_info_rules_cannot_be_forged_by_direct_construction(self):
        parsed = symbol_rules()
        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "canonical provider payload parsing",
        ):
            BinanceSpotSymbolRules(**parsed.__dict__)

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

    def test_ack_symbol_identity_must_be_canonical_uppercase(self):
        with self.assertRaisesRegex(BinanceSpotAdapterError, "canonical uppercase"):
            parse_order_ack(
                attempt_id=str(uuid4()),
                client_order_id="at-ack-lower-symbol",
                response={
                    "symbol": "btcusdt",
                    "orderId": 42,
                    "clientOrderId": "at-ack-lower-symbol",
                    "transactTime": 1790272800123,
                },
            )

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
                "isBuyer": True,
                "time": 1790272800123,
            }
        ]
        observation = execution_observation([rows[0], dict(rows[0])])
        fills = parse_account_trades(
            observation,
            instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
            client_ids_by_order_id={42: "at-ack-1"},
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].provider_execution_id, "BINANCE-SPOT:BTCUSDT:7")
        self.assertEqual(fills[0].client_order_id, "at-ack-1")
        self.assertEqual(fills[0].account_id, "paper-1")
        self.assertEqual(fills[0].environment, "PAPER")
        self.assertEqual(fills[0].evidence_refs, (observation.evidence_ref,))
        self.assertEqual(fills[0].quantity, Decimal("0.2"))
        self.assertEqual(fills[0].fee_currency, "BNB")
        self.assertEqual(fills[0].side, "BUY")
        self.assertIsNone(fills[0].position_side)

    def test_account_trade_requires_provider_evidenced_direction(self):
        base = {
            "symbol": "BTCUSDT",
            "id": 8,
            "orderId": 43,
            "price": "101.25",
            "qty": "0.1",
            "commission": "0.001",
            "commissionAsset": "BNB",
            "time": 1790272801123,
        }
        for value in (None, "true", 1):
            with self.subTest(isBuyer=value):
                row = dict(base)
                if value is not None:
                    row["isBuyer"] = value
                with self.assertRaisesRegex(
                    BinanceSpotAdapterError,
                    "isBuyer must be provider-evidenced boolean",
                ):
                    parse_account_trades(
                        execution_observation([row]),
                        instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
                    )

        sell = dict(base, isBuyer=False)
        fills = parse_account_trades(
            execution_observation([sell]),
            instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
        )
        self.assertEqual(fills[0].side, "SELL")
        self.assertIsNone(fills[0].position_side)

    def test_client_order_identity_map_is_fully_validated_before_fill_mapping(self):
        row = {
            "symbol": "BTCUSDT",
            "id": 8,
            "orderId": 43,
            "price": "101.25",
            "qty": "0.1",
            "commission": "0.001",
            "commissionAsset": "BNB",
            "isBuyer": True,
            "time": 1790272801123,
        }
        observation = execution_observation([row])

        with self.assertRaisesRegex(BinanceSpotAdapterError, "must be a mapping"):
            parse_account_trades(
                observation,
                instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
                client_ids_by_order_id=[],
            )

        for bad_map in (
            {"43": "at-spot-fill"},
            {True: "at-spot-fill"},
            {-1: "at-spot-fill"},
        ):
            with self.subTest(bad_map=bad_map), self.assertRaisesRegex(
                BinanceSpotAdapterError,
                "non-negative integer order ids",
            ):
                parse_account_trades(
                    observation,
                    instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
                    client_ids_by_order_id=bad_map,
                )

        with self.assertRaisesRegex(BinanceSpotAdapterError, "client_order_id"):
            parse_account_trades(
                observation,
                instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
                client_ids_by_order_id={99: "bad client id with spaces"},
            )

    def test_instrument_identity_map_is_fully_validated_before_fill_mapping(self):
        row = {
            "symbol": "BTCUSDT",
            "id": 8,
            "orderId": 43,
            "price": "101.25",
            "qty": "0.1",
            "commission": "0.001",
            "commissionAsset": "BNB",
            "isBuyer": True,
            "time": 1790272801123,
        }
        observation = execution_observation([row])

        with self.assertRaisesRegex(BinanceSpotAdapterError, "canonical uppercase"):
            parse_account_trades(
                observation,
                instrument_versions={
                    "BTCUSDT": "BTCUSDT:v1",
                    "ethusdt": "ETHUSDT:v1",
                },
            )

        with self.assertRaisesRegex(BinanceSpotAdapterError, "instrument_version"):
            parse_account_trades(
                observation,
                instrument_versions={
                    "BTCUSDT": "BTCUSDT:v1",
                    "ETHUSDT": "",
                },
            )

        with self.assertRaisesRegex(BinanceSpotAdapterError, "duplicate normalized"):
            parse_account_trades(
                observation,
                instrument_versions={
                    "BTCUSDT": "BTCUSDT:v1",
                    " BTCUSDT ": "BTCUSDT:v2",
                },
            )

    def test_spot_fill_remains_compatible_with_cash_equity_financial_plan(self):
        row = {
            "symbol": "BTCUSDT",
            "id": 9,
            "orderId": 44,
            "price": "101.25",
            "qty": "0.1",
            "commission": "0",
            "commissionAsset": "BNB",
            "isBuyer": True,
            "time": 1790272802123,
        }
        provider_fill = parse_account_trades(
            execution_observation([row]),
            instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
        )[0]
        self.assertIsNone(provider_fill.position_side)
        projected = ProjectedFillEvidence.create(
            fill_id="spot-fill-9",
            provider_execution_id=provider_fill.provider_execution_id,
            intent_id="spot-intent-9",
            client_order_id=provider_fill.client_order_id,
            side=provider_fill.side,
            quantity=provider_fill.quantity,
            price=provider_fill.price,
        )
        reservation = ReservationSnapshot(
            reservation_id="spot-reservation-9",
            intent_id="spot-intent-9",
            original={"CASH:USDT": Decimal("20")},
            remaining={"CASH:USDT": Decimal("20")},
            consumed={"CASH:USDT": Decimal("0")},
            state="WORKING",
        )
        plan = build_provider_fill_financial_plan(
            book=ScopedEconomicBook(environment="PAPER", account_id="paper-1"),
            provider_id="BINANCE",
            projected_fill=projected,
            provider_fill=provider_fill,
            expected_instrument="BTCUSDT:v1",
            settlement_currency="USDT",
            reservation_snapshot=reservation,
        )
        self.assertEqual(plan.usage["CASH:USDT"], Decimal("10.125"))

    def test_execution_parser_rejects_wrong_authenticated_read_surface(self):
        row = {
            "symbol": "BTCUSDT",
            "id": 7,
            "orderId": 42,
            "price": "100.25",
            "qty": "0.2",
            "commission": "0.001",
            "commissionAsset": "BNB",
            "isBuyer": True,
            "time": 1790272800123,
        }
        wrong_surface = execution_observation(
            [row],
            surface=Surface.AUTHENTICATED_READ,
        )
        with self.assertRaisesRegex(BinanceSpotAdapterError, "scope"):
            parse_account_trades(
                wrong_surface,
                instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
            )

    def test_conflicting_duplicate_trade_id_fails_closed(self):
        first = {
            "symbol": "BTCUSDT",
            "id": 7,
            "orderId": 42,
            "price": "100.25",
            "qty": "0.2",
            "commission": "0.001",
            "commissionAsset": "BNB",
            "isBuyer": True,
            "time": 1790272800123,
        }
        changed = dict(first, qty="0.3")
        with self.assertRaisesRegex(BinanceSpotAdapterError, "conflicting"):
            parse_account_trades(
                execution_observation([first, changed]),
                instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
            )

    def test_absence_semantics_are_never_assumed_from_empty_surface(self):
        evidence = coverage_evidence(
            account_id="paper-1",
            environment="PAPER",
            surface="ORDER_HISTORY",
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
        )
        self.assertFalse(evidence.provider_semantics_exclude_execution)
        qualified = coverage_evidence(
            account_id="paper-1",
            environment="PAPER",
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
