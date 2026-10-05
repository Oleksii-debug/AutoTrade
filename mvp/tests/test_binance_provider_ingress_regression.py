from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from types import MappingProxyType
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.binance_spot import (
    BinanceSpotAdapterError,
    BinanceSpotOrderIntent,
    BinanceSpotReferencePrice,
    BinanceSpotSymbolRules,
    parse_account_trades as parse_spot_account_trades,
    parse_order_ack as parse_spot_order_ack,
)
from mvp.autotrade_mvp.binance_usdm import (
    BinanceUsdmAdapterError,
    parse_account_trades as parse_usdm_account_trades,
    parse_order_ack as parse_usdm_order_ack,
)
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)


NOW = datetime(2026, 10, 5, 8, tzinfo=timezone.utc)


def _spot_capability():
    observed_at = NOW - timedelta(hours=1)
    common = dict(
        provider_id="BINANCE",
        account_id="paper-1",
        entity_id="global",
        environment="PAPER",
        instrument_version="BTCUSDT:v1",
        observed_at=observed_at,
        expires_at=NOW + timedelta(hours=1),
        supported_order_types=frozenset({"LIMIT", "MARKET"}),
        time_in_force=frozenset({"GTC", "IOC", "FOK", "NONE"}),
        permission_scopes=frozenset({"TRADE.READ"}),
        position_mode="NET",
        native_protection=frozenset(),
        rate_limit_policy_id="binance-provider-ingress-regression",
        data_entitlements=frozenset({"TRADES"}),
    )
    claims = tuple(
        CapabilityClaim(
            source=source,
            **common,
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "d" * 64,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                "source_uri": "https://developers.binance.com/docs/binance-spot-api-docs/rest-api/account-endpoints",
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


def _spot_trade_observation():
    query = prepare_authenticated_read_query(
        capability=_spot_capability(),
        surface=Surface.ACTIVITIES,
        endpoint="/api/v3/myTrades",
        query={"symbol": "BTCUSDT"},
        at=NOW,
        permission_scope="TRADE.READ",
    )
    rows = [
        {
            "symbol": "BTCUSDT",
            "id": 7,
            "orderId": 42,
            "isBuyer": True,
            "qty": "0.100",
            "price": "40000.00",
            "commission": "0.01",
            "commissionAsset": "USDT",
            "time": 1791187200123,
        }
    ]
    return observe_authenticated_json_response(
        query_binding=query,
        http_status=200,
        response_bytes=json.dumps(
            rows,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8"),
        observed_at=NOW,
    )


def _usdm_trade_observation():
    query = prepare_authenticated_read_query(
        capability=_spot_capability(),
        surface=Surface.AUTHENTICATED_READ,
        endpoint="/fapi/v1/userTrades",
        query={"symbol": "BTCUSDT"},
        at=NOW,
        permission_scope="TRADE.READ",
    )
    rows = [
        {
            "symbol": "BTCUSDT",
            "id": 8,
            "orderId": 43,
            "side": "BUY",
            "positionSide": "BOTH",
            "qty": "0.100",
            "price": "40000.00",
            "commission": "0.01",
            "commissionAsset": "USDT",
            "time": 1791187200123,
        }
    ]
    return observe_authenticated_json_response(
        query_binding=query,
        http_status=200,
        response_bytes=json.dumps(
            rows,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8"),
        observed_at=NOW,
    )


def _spot_notional_rules():
    return BinanceSpotSymbolRules.from_exchange_info(
        instrument_version="BTCUSDT:v1",
        symbol_payload={
            "symbol": "BTCUSDT",
            "filters": [
                {
                    "filterType": "PRICE_FILTER",
                    "minPrice": "0",
                    "maxPrice": "0",
                    "tickSize": "0",
                },
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.001",
                    "maxQty": "1000",
                    "stepSize": "0.001",
                },
                {
                    "filterType": "MIN_NOTIONAL",
                    "minNotional": "10",
                    "applyToMarket": True,
                    "avgPriceMins": 5,
                },
            ],
        },
    )


def _spot_market_intent():
    return BinanceSpotOrderIntent.create(
        instrument_version="BTCUSDT:v1",
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity="1",
    )


def _canonical_spot_reference():
    return BinanceSpotReferencePrice.from_reference_price_payload(
        instrument_version="BTCUSDT:v1",
        symbol="BTCUSDT",
        payload={
            "symbol": "BTCUSDT",
            "referencePrice": "60000",
            "timestamp": 1791187199000,
        },
    )


class BinanceProviderIngressRegressionTests(unittest.TestCase):
    def test_spot_accepts_canonical_frozen_authenticated_trade_payload(self):
        observation = _spot_trade_observation()

        self.assertIs(type(observation.payload), tuple)
        self.assertEqual(len(observation.payload), 1)
        self.assertIs(type(observation.payload[0]), MappingProxyType)
        self.assertIs(type(observation.payload[0]["id"]), int)
        self.assertIs(type(observation.payload[0]["orderId"]), int)

        fills = parse_spot_account_trades(
            observation,
            instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
            client_ids_by_order_id={42: "spot-client-42"},
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].client_order_id, "spot-client-42")
        self.assertEqual(fills[0].provider_execution_id, "BINANCE-SPOT:BTCUSDT:7")

    def test_usdm_accepts_canonical_frozen_authenticated_trade_payload(self):
        observation = _usdm_trade_observation()

        self.assertIs(type(observation.payload), tuple)
        self.assertEqual(len(observation.payload), 1)
        self.assertIs(type(observation.payload[0]), MappingProxyType)
        self.assertIs(type(observation.payload[0]["id"]), int)
        self.assertIs(type(observation.payload[0]["orderId"]), int)

        fills = parse_usdm_account_trades(
            observation,
            instrument_versions={"BTCUSDT": "BTCUSDT-PERP:v1"},
            client_ids_by_order_id={43: "usdm-client-43"},
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].client_order_id, "usdm-client-43")
        self.assertEqual(fills[0].provider_execution_id, "BINANCE-USDM:BTCUSDT:8")

    def test_spot_identity_maps_reject_mapping_subclass_before_items_callback(self):
        observation = _spot_trade_observation()
        callbacks = []

        class HostileMapping(dict):
            def items(self):
                callbacks.append("items")
                raise AssertionError("hostile identity-map callback executed")

        with self.assertRaisesRegex(BinanceSpotAdapterError, "instrument_versions must be an exact dict"):
            parse_spot_account_trades(
                observation,
                instrument_versions=HostileMapping({"BTCUSDT": "BTCUSDT:v1"}),
            )
        self.assertEqual(callbacks, [])

        with self.assertRaisesRegex(BinanceSpotAdapterError, "client_ids_by_order_id must be an exact dict"):
            parse_spot_account_trades(
                observation,
                instrument_versions={"BTCUSDT": "BTCUSDT:v1"},
                client_ids_by_order_id=HostileMapping({42: "spot-client-42"}),
            )
        self.assertEqual(callbacks, [])

    def test_usdm_identity_maps_reject_mapping_subclass_before_items_callback(self):
        observation = _usdm_trade_observation()
        callbacks = []

        class HostileMapping(dict):
            def items(self):
                callbacks.append("items")
                raise AssertionError("hostile identity-map callback executed")

        with self.assertRaisesRegex(BinanceUsdmAdapterError, "instrument_versions must be an exact dict"):
            parse_usdm_account_trades(
                observation,
                instrument_versions=HostileMapping({"BTCUSDT": "BTCUSDT-PERP:v1"}),
            )
        self.assertEqual(callbacks, [])

        with self.assertRaisesRegex(BinanceUsdmAdapterError, "client_ids_by_order_id must be an exact dict"):
            parse_usdm_account_trades(
                observation,
                instrument_versions={"BTCUSDT": "BTCUSDT-PERP:v1"},
                client_ids_by_order_id=HostileMapping({43: "usdm-client-43"}),
            )
        self.assertEqual(callbacks, [])

    def test_spot_market_notional_rejects_forged_reference_subclass_before_callback(self):
        callbacks = []

        class ForgedReference(BinanceSpotReferencePrice):
            def __post_init__(self, _verification_token=None):
                # Bypass the base class private factory-token check. Exact-type
                # admission must reject this object before any economic field is read.
                return None

            def __getattribute__(self, name):
                if name in {
                    "instrument_version",
                    "symbol",
                    "observed_at",
                    "reference_kind",
                    "price",
                }:
                    callbacks.append(name)
                return super().__getattribute__(name)

        forged = ForgedReference(
            instrument_version="BTCUSDT:v1",
            symbol="BTCUSDT",
            price=Decimal("60000"),
            observed_at=NOW - timedelta(seconds=1),
            source_sha256="sha256:" + "e" * 64,
            reference_kind="REFERENCE",
            averaging_window_minutes=None,
        )

        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "provider reference-price observation evidence",
        ):
            _spot_notional_rules().validate(
                _spot_market_intent(),
                at=NOW,
                reference_price_observation=forged,
                maximum_market_reference_age_seconds=30,
            )
        self.assertEqual(callbacks, [])

    def test_spot_market_notional_rejects_age_int_subclass_before_comparison_callback(self):
        callbacks = []

        class HostileInt(int):
            def __lt__(self, other):
                callbacks.append(("lt", other))
                raise AssertionError("hostile freshness-window comparison executed")

        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "maximum_market_reference_age_seconds",
        ):
            _spot_notional_rules().validate(
                _spot_market_intent(),
                at=NOW,
                reference_price_observation=_canonical_spot_reference(),
                maximum_market_reference_age_seconds=HostileInt(30),
            )
        self.assertEqual(callbacks, [])

    def test_usdm_ack_rejects_mapping_subclass_before_get_callback(self):
        callbacks = []

        class HostileResponse(dict):
            def get(self, key, default=None):
                callbacks.append(key)
                raise AssertionError("hostile response mapping callback executed")

        response = HostileResponse(
            {
                "symbol": "BTCUSDT",
                "orderId": 7,
                "clientOrderId": "usdm-client-7",
                "updateTime": 1791187200123,
            }
        )
        with self.assertRaisesRegex(BinanceUsdmAdapterError, "exact decoded object"):
            parse_usdm_order_ack(
                attempt_id=str(uuid4()),
                client_order_id="usdm-client-7",
                response=response,
            )
        self.assertEqual(callbacks, [])

    def test_spot_ack_rejects_oversized_integer_timestamp_before_datetime_conversion(self):
        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "supported UTC millisecond range",
        ):
            parse_spot_order_ack(
                attempt_id=str(uuid4()),
                client_order_id="spot-huge-time",
                response={
                    "symbol": "BTCUSDT",
                    "orderId": 9,
                    "clientOrderId": "spot-huge-time",
                    "transactTime": 10 ** 1000,
                },
            )

    def test_usdm_ack_rejects_oversized_digit_text_before_integer_materialization(self):
        with self.assertRaisesRegex(
            BinanceUsdmAdapterError,
            "supported UTC millisecond range",
        ):
            parse_usdm_order_ack(
                attempt_id=str(uuid4()),
                client_order_id="usdm-huge-time",
                response={
                    "symbol": "BTCUSDT",
                    "orderId": 10,
                    "clientOrderId": "usdm-huge-time",
                    "updateTime": "9" * 5000,
                },
            )

    def test_timestamp_conversion_is_platform_independent_inside_supported_domain(self):
        timestamp_ms = 32_503_680_000_000  # 3000-01-01T00:00:00Z
        spot = parse_spot_order_ack(
            attempt_id=str(uuid4()),
            client_order_id="spot-year-3000",
            response={
                "symbol": "BTCUSDT",
                "orderId": 11,
                "clientOrderId": "spot-year-3000",
                "transactTime": timestamp_ms,
            },
        )
        usdm = parse_usdm_order_ack(
            attempt_id=str(uuid4()),
            client_order_id="usdm-year-3000",
            response={
                "symbol": "BTCUSDT",
                "orderId": 12,
                "clientOrderId": "usdm-year-3000",
                "updateTime": timestamp_ms,
            },
        )
        self.assertEqual(
            spot["provider_received_at"],
            "3000-01-01T00:00:00.000Z",
        )
        self.assertEqual(
            usdm["provider_received_at"],
            "3000-01-01T00:00:00.000Z",
        )

    def test_usdm_ack_rejects_int_subclass_before_comparison_callback(self):
        callbacks = []

        class HostileInt(int):
            def __lt__(self, other):
                callbacks.append(("lt", other))
                raise AssertionError("hostile integer comparison executed")

        with self.assertRaisesRegex(BinanceUsdmAdapterError, "non-negative integer"):
            parse_usdm_order_ack(
                attempt_id=str(uuid4()),
                client_order_id="usdm-client-8",
                response={
                    "symbol": "BTCUSDT",
                    "orderId": HostileInt(8),
                    "clientOrderId": "usdm-client-8",
                    "updateTime": 1791187200123,
                },
            )
        self.assertEqual(callbacks, [])


if __name__ == "__main__":
    unittest.main()
