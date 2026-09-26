from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.binance_usdm import (
    BinanceUsdmAdapterError,
    BinanceUsdmMarkPrice,
    BinanceUsdmOrderIntent,
    BinanceUsdmSymbolRules,
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
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    CapabilitySnapshot,
    EvidenceVerification,
    derive_capability_snapshot,
)


NOW = datetime(2026, 9, 25, 0, tzinfo=timezone.utc)


def execution_observation(
    rows,
    *,
    account_id="paper-1",
    environment="PAPER",
    surface=Surface.AUTHENTICATED_READ,
):
    query = prepare_authenticated_read_query(
        capability=capability(
            account_id=account_id,
            environment=environment,
            instrument_version="BTCUSDT-PERP:v1",
        ),
        surface=surface,
        endpoint="/fapi/v1/userTrades",
        query={"symbol": "BTCUSDT"},
        at=NOW,
        permission_scope="ORDER.READ",
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
    position_mode="NET",
    order_types=("LIMIT", "MARKET"),
    tif=("GTC", "IOC", "FOK", "NONE"),
    provider_id="BINANCE",
    account_id="account-1",
    environment="PAPER",
    instrument_version="BTCUSDT-PERP:v1",
    status="VERIFIED",
):
    observed_at = NOW - timedelta(hours=1)
    common = dict(
        provider_id=provider_id,
        account_id=account_id,
        entity_id="global",
        environment=environment,
        instrument_version=instrument_version,
        observed_at=observed_at,
        expires_at=NOW + timedelta(hours=1),
        supported_order_types=frozenset(order_types),
        time_in_force=frozenset(tif),
        permission_scopes=frozenset({"ORDER_WRITE", "ORDER.READ"}),
        position_mode=position_mode,
        native_protection=frozenset(),
        rate_limit_policy_id="binance-usdm-foundation",
        data_entitlements=frozenset({"ORDERS", "TRADES"}),
    )
    if status != "VERIFIED":
        return CapabilitySnapshot(
            snapshot_id=str(uuid4()),
            **common,
            evidence=(),
            status=status,
            sources=frozenset(),
        )
    claims = tuple(
        CapabilityClaim(
            source=source,
            **common,
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "c" * 64,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                "source_uri": (
                    "https://developers.binance.com/en/docs/catalog/"
                    "core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade"
                ),
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
    status="TRADING",
    order_types=("LIMIT", "MARKET"),
    time_in_force=("GTC", "IOC", "FOK"),
    price_min="0.01",
    price_max="1000000",
    tick_size="0.01",
    lot_min="0.001",
    lot_max="1000",
    lot_step="0.001",
    market_min="0.001",
    market_max="1000",
    market_step="0.001",
    min_notional=None,
    price_precision=0,
    quantity_precision=0,
):
    filters = [
        {
            "filterType": "PRICE_FILTER",
            "minPrice": price_min,
            "maxPrice": price_max,
            "tickSize": tick_size,
        },
        {
            "filterType": "LOT_SIZE",
            "minQty": lot_min,
            "maxQty": lot_max,
            "stepSize": lot_step,
        },
        {
            "filterType": "MARKET_LOT_SIZE",
            "minQty": market_min,
            "maxQty": market_max,
            "stepSize": market_step,
        },
    ]
    if min_notional is not None:
        filters.append(
            {
                "filterType": "MIN_NOTIONAL",
                "notional": min_notional,
            }
        )
    return BinanceUsdmSymbolRules.from_exchange_info(
        instrument_version="BTCUSDT-PERP:v1",
        symbol_payload={
            "symbol": "BTCUSDT",
            "status": status,
            "contractType": "PERPETUAL",
            "pricePrecision": price_precision,
            "quantityPrecision": quantity_precision,
            "orderTypes": list(order_types),
            "timeInForce": list(time_in_force),
            "filters": filters,
        },
    )


def mark_price(
    *,
    price="40000",
    observed_at=NOW,
    symbol="BTCUSDT",
    instrument_version="BTCUSDT-PERP:v1",
):
    timestamp_ms = int(observed_at.timestamp() * 1000)
    return BinanceUsdmMarkPrice.from_premium_index(
        instrument_version=instrument_version,
        symbol=symbol,
        payload={
            "symbol": symbol,
            "markPrice": price,
            "indexPrice": price,
            "estimatedSettlePrice": price,
            "lastFundingRate": "0.0001",
            "interestRate": "0.0001",
            "nextFundingTime": timestamp_ms + 28_800_000,
            "time": timestamp_ms,
        },
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
            symbol_rules=symbol_rules(),
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
            symbol_rules=symbol_rules(),
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
                symbol_rules=symbol_rules(),
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
                symbol_rules=symbol_rules(),
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
                symbol_rules=symbol_rules(),
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
            symbol_rules=symbol_rules(),
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
            symbol_rules=symbol_rules(),
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
                symbol_rules=symbol_rules(),
                at=NOW,
            )
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "instrument version"):
            prepare_order_request(
                intent,
                client_order_id="at-usdm-version",
                capability=capability(instrument_version="ETHUSDT-PERP:v1"),
                symbol_rules=symbol_rules(),
                at=NOW,
            )
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "capability"):
            prepare_order_request(
                intent,
                client_order_id="at-usdm-status",
                capability=capability(status="UNKNOWN"),
                symbol_rules=symbol_rules(),
                at=NOW,
            )

    def test_exchange_info_filters_are_required_and_precision_fields_are_not_rules(self):
        intent = BinanceUsdmOrderIntent.create(
            instrument_version="BTCUSDT-PERP:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="0.123",
            price="40000.25",
            time_in_force="GTC",
        )
        rules = symbol_rules(
            price_precision=0,
            quantity_precision=0,
            tick_size="0.01",
            lot_step="0.001",
        )
        request = prepare_order_request(
            intent,
            client_order_id="at-usdm-filter-evidence",
            capability=capability(),
            symbol_rules=rules,
            at=NOW,
        )
        self.assertEqual(request.body["quantity"], "0.123")
        self.assertEqual(request.body["price"], "40000.25")
        self.assertEqual(request.filter_source_sha256, rules.source_sha256)
        self.assertRegex(request.filter_source_sha256, r"^sha256:[0-9a-f]{64}$")

        with self.assertRaises(TypeError):
            prepare_order_request(
                intent,
                client_order_id="at-usdm-missing-rules",
                capability=capability(),
                symbol_rules=None,
                at=NOW,
            )

    def test_exchange_info_quantity_price_status_and_capabilities_fail_closed(self):
        off_step = BinanceUsdmOrderIntent.create(
            instrument_version="BTCUSDT-PERP:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="0.0105",
            price="40000.25",
            time_in_force="GTC",
        )
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "quantity"):
            prepare_order_request(
                off_step,
                client_order_id="at-usdm-off-step",
                capability=capability(),
                symbol_rules=symbol_rules(lot_step="0.001"),
                at=NOW,
            )

        off_tick = BinanceUsdmOrderIntent.create(
            instrument_version="BTCUSDT-PERP:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="0.010",
            price="40000.255",
            time_in_force="GTC",
        )
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "price"):
            prepare_order_request(
                off_tick,
                client_order_id="at-usdm-off-tick",
                capability=capability(),
                symbol_rules=symbol_rules(tick_size="0.01"),
                at=NOW,
            )

        valid = BinanceUsdmOrderIntent.create(
            instrument_version="BTCUSDT-PERP:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="0.010",
            price="40000.25",
            time_in_force="GTC",
        )
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "not TRADING"):
            prepare_order_request(
                valid,
                client_order_id="at-usdm-halted",
                capability=capability(),
                symbol_rules=symbol_rules(status="SETTLING"),
                at=NOW,
            )
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "order type"):
            prepare_order_request(
                valid,
                client_order_id="at-usdm-order-type-filter",
                capability=capability(),
                symbol_rules=symbol_rules(order_types=("MARKET",)),
                at=NOW,
            )
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "time_in_force"):
            prepare_order_request(
                valid,
                client_order_id="at-usdm-tif-filter",
                capability=capability(),
                symbol_rules=symbol_rules(time_in_force=("IOC",)),
                at=NOW,
            )

    def test_market_lot_size_overrides_limit_lot_size(self):
        intent = BinanceUsdmOrderIntent.create(
            instrument_version="BTCUSDT-PERP:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.005",
        )
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "quantity"):
            prepare_order_request(
                intent,
                client_order_id="at-usdm-market-lot",
                capability=capability(),
                symbol_rules=symbol_rules(
                    lot_min="0.001",
                    lot_step="0.001",
                    market_min="0.01",
                    market_step="0.01",
                ),
                at=NOW,
            )

    def test_market_min_notional_requires_fresh_bound_mark_price_evidence(self):
        intent = BinanceUsdmOrderIntent.create(
            instrument_version="BTCUSDT-PERP:v1",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.001",
        )
        rules = symbol_rules(min_notional="20")

        with self.assertRaisesRegex(BinanceUsdmAdapterError, "mark-price evidence"):
            prepare_order_request(
                intent,
                client_order_id="at-usdm-market-no-mark",
                capability=capability(),
                symbol_rules=rules,
                at=NOW,
            )

        with self.assertRaisesRegex(BinanceUsdmAdapterError, "stale"):
            prepare_order_request(
                intent,
                client_order_id="at-usdm-market-stale-mark",
                capability=capability(),
                symbol_rules=rules,
                at=NOW,
                mark_price=mark_price(observed_at=NOW - timedelta(seconds=31)),
                maximum_mark_price_age_seconds=30,
            )

        with self.assertRaisesRegex(BinanceUsdmAdapterError, "notional"):
            prepare_order_request(
                intent,
                client_order_id="at-usdm-market-small",
                capability=capability(),
                symbol_rules=rules,
                at=NOW,
                mark_price=mark_price(price="10000"),
                maximum_mark_price_age_seconds=30,
            )

        provider_mark = mark_price(price="40000")
        accepted = prepare_order_request(
            intent,
            client_order_id="at-usdm-market-fresh-mark",
            capability=capability(),
            symbol_rules=rules,
            at=NOW,
            mark_price=provider_mark,
            maximum_mark_price_age_seconds=30,
        )
        self.assertEqual(
            accepted.mark_price_source_sha256,
            provider_mark.source_sha256,
        )

    def test_exchange_info_rules_and_mark_price_cannot_be_forged_directly(self):
        parsed_rules = symbol_rules()
        with self.assertRaisesRegex(
            BinanceUsdmAdapterError,
            "canonical provider payload parsing",
        ):
            BinanceUsdmSymbolRules(**parsed_rules.__dict__)

        provider_mark = mark_price()
        with self.assertRaisesRegex(
            BinanceUsdmAdapterError,
            "canonical provider payload parsing",
        ):
            BinanceUsdmMarkPrice(**provider_mark.__dict__)

    def test_exchange_info_parser_rejects_missing_duplicate_and_invalid_filters(self):
        base = {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "orderTypes": ["LIMIT", "MARKET"],
            "timeInForce": ["GTC", "IOC"],
        }
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "required PRICE_FILTER"):
            BinanceUsdmSymbolRules.from_exchange_info(
                instrument_version="BTCUSDT-PERP:v1",
                symbol_payload={
                    **base,
                    "filters": [
                        {
                            "filterType": "LOT_SIZE",
                            "minQty": "0.001",
                            "maxQty": "100",
                            "stepSize": "0.001",
                        }
                    ],
                },
            )

        duplicate_price = {
            "filterType": "PRICE_FILTER",
            "minPrice": "1",
            "maxPrice": "100000",
            "tickSize": "0.1",
        }
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "duplicate"):
            BinanceUsdmSymbolRules.from_exchange_info(
                instrument_version="BTCUSDT-PERP:v1",
                symbol_payload={
                    **base,
                    "filters": [
                        duplicate_price,
                        dict(duplicate_price),
                        {
                            "filterType": "LOT_SIZE",
                            "minQty": "0.001",
                            "maxQty": "100",
                            "stepSize": "0.001",
                        },
                    ],
                },
            )

        with self.assertRaisesRegex(BinanceUsdmAdapterError, "MIN_NOTIONAL"):
            BinanceUsdmSymbolRules.from_exchange_info(
                instrument_version="BTCUSDT-PERP:v1",
                symbol_payload={
                    **base,
                    "filters": [
                        duplicate_price,
                        {
                            "filterType": "LOT_SIZE",
                            "minQty": "0.001",
                            "maxQty": "100",
                            "stepSize": "0.001",
                        },
                        {
                            "filterType": "MIN_NOTIONAL",
                            "notional": "0",
                        },
                    ],
                },
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
                execution_observation(
                    [
                        {
                            "commission": "0.01",
                            "commissionAsset": "USDT",
                            "id": 7,
                            "orderId": 42,
                            "price": "100",
                            "qty": "0.2",
                            "positionSide": "BOTH",
                            "side": "BUY",
                            "symbol": "btcusdt",
                            "time": 1790272800123,
                        }
                    ]
                ),
                instrument_versions={"BTCUSDT": "BTCUSDT-PERP:v1"},
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
        observation = execution_observation([row, dict(row)])
        fills = parse_account_trades(
            observation,
            instrument_versions={"BTCUSDT": "BTCUSDT-PERP:v1"},
            client_ids_by_order_id={25851813: "at-usdm-fill"},
        )
        self.assertEqual(len(fills), 1)
        fill = fills[0]
        self.assertEqual(fill.provider_execution_id, "BINANCE-USDM:BTCUSDT:698759")
        self.assertEqual(fill.account_id, "paper-1")
        self.assertEqual(fill.environment, "PAPER")
        self.assertEqual(fill.evidence_refs, (observation.evidence_ref,))
        self.assertEqual(fill.client_order_id, "at-usdm-fill")
        self.assertEqual(fill.quantity, Decimal("0.002"))
        self.assertEqual(fill.price, Decimal("7819.01"))
        self.assertEqual(fill.fee_amount, Decimal("0.07819010"))
        self.assertEqual(fill.fee_currency, "USDT")
        self.assertEqual(fill.side, "SELL")
        self.assertEqual(fill.position_side, "SHORT")

        conflicting = dict(row)
        conflicting["side"] = "BUY"
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "conflicting economic content"):
            parse_account_trades(
                execution_observation([row, conflicting]),
                instrument_versions={"BTCUSDT": "BTCUSDT-PERP:v1"},
                client_ids_by_order_id={25851813: "at-usdm-fill"},
            )

    def test_trade_direction_is_required_from_provider_bytes(self):
        row = {
            "commission": "0.01",
            "commissionAsset": "USDT",
            "id": 8,
            "orderId": 43,
            "price": "100",
            "qty": "0.2",
            "positionSide": "BOTH",
            "symbol": "BTCUSDT",
            "time": 1790272800123,
        }
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "side"):
            parse_account_trades(
                execution_observation([row]),
                instrument_versions={"BTCUSDT": "BTCUSDT-PERP:v1"},
            )

    def test_client_order_identity_map_is_fully_validated_before_fill_mapping(self):
        row = {
            "commission": "0.01",
            "commissionAsset": "USDT",
            "id": 7,
            "orderId": 42,
            "price": "100",
            "qty": "0.2",
            "positionSide": "BOTH",
            "side": "BUY",
            "symbol": "BTCUSDT",
            "time": 1790272800123,
        }
        observation = execution_observation([row])

        with self.assertRaisesRegex(BinanceUsdmAdapterError, "must be a mapping"):
            parse_account_trades(
                observation,
                instrument_versions={"BTCUSDT": "BTCUSDT-PERP:v1"},
                client_ids_by_order_id=[],
            )

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
                    observation,
                    instrument_versions={"BTCUSDT": "BTCUSDT-PERP:v1"},
                    client_ids_by_order_id=bad_map,
                )

        with self.assertRaisesRegex(BinanceUsdmAdapterError, "client_order_id"):
            parse_account_trades(
                observation,
                instrument_versions={"BTCUSDT": "BTCUSDT-PERP:v1"},
                client_ids_by_order_id={42: "bad client id with spaces"},
            )

        with self.assertRaisesRegex(BinanceUsdmAdapterError, "multiple provider order ids"):
            parse_account_trades(
                observation,
                instrument_versions={"BTCUSDT": "BTCUSDT-PERP:v1"},
                client_ids_by_order_id={
                    43: "at-usdm-same-client",
                    44: "at-usdm-same-client",
                },
            )

    def test_instrument_identity_map_is_fully_validated_before_fill_mapping(self):
        row = {
            "commission": "0.01",
            "commissionAsset": "USDT",
            "id": 8,
            "orderId": 43,
            "price": "101",
            "qty": "0.1",
            "positionSide": "BOTH",
            "side": "BUY",
            "symbol": "BTCUSDT",
            "time": 1790272800123,
        }
        observation = execution_observation([row])

        with self.assertRaisesRegex(BinanceUsdmAdapterError, "uppercase"):
            parse_account_trades(
                observation,
                instrument_versions={
                    "BTCUSDT": "BTCUSDT-PERP:v1",
                    "ethusdt": "ETHUSDT-PERP:v1",
                },
            )

        with self.assertRaisesRegex(BinanceUsdmAdapterError, "instrument_version"):
            parse_account_trades(
                observation,
                instrument_versions={
                    "BTCUSDT": "BTCUSDT-PERP:v1",
                    "ETHUSDT": "",
                },
            )

        with self.assertRaisesRegex(BinanceUsdmAdapterError, "duplicate normalized"):
            parse_account_trades(
                observation,
                instrument_versions={
                    "BTCUSDT": "BTCUSDT-PERP:v1",
                    " BTCUSDT ": "BTCUSDT-PERP:v2",
                },
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
            "side": "BUY",
            "symbol": "BTCUSDT",
            "time": 1790272800123,
        }
        changed = dict(first, qty="0.3")
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "conflicting"):
            parse_account_trades(
                execution_observation([first, changed]),
                instrument_versions={"BTCUSDT": "BTCUSDT-PERP:v1"},
            )

    def test_empty_order_history_never_proves_absence_by_default(self):
        evidence = coverage_evidence(
            account_id="paper-1",
            environment="PAPER",
            surface="ORDER_HISTORY",
            coverage_start="2026-09-24T20:00:00Z",
            coverage_end="2026-09-25T00:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
        )
        self.assertFalse(evidence.provider_semantics_exclude_execution)

        with self.assertRaisesRegex(
            BinanceUsdmAdapterError,
            "cannot self-assert provider exclusion semantics",
        ):
            coverage_evidence(
                account_id="paper-1",
                environment="PAPER",
                surface="ORDER_HISTORY",
                coverage_start="2026-09-24T20:00:00Z",
                coverage_end="2026-09-25T00:00:00Z",
                pagination_complete=True,
                consistency_horizon_satisfied=True,
                qualified_exclusion_semantics=True,
            )

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
                symbol_rules=symbol_rules(),
                at=NOW,
            )

        with self.assertRaisesRegex(BinanceUsdmAdapterError, "positionSide"):
            parse_account_trades(
                execution_observation([
                    {
                        "commission": "0.01",
                        "commissionAsset": "USDT",
                        "id": 1,
                        "orderId": 2,
                        "price": "100",
                        "qty": "0.1",
                        "positionSide": "INVALID",
                        "side": "BUY",
                        "symbol": "BTCUSDT",
                        "time": 1790272800123,
                    }
                ]),
                instrument_versions={"BTCUSDT": "BTCUSDT-PERP:v1"},
            )


if __name__ == "__main__":
    unittest.main()
