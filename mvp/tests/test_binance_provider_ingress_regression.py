from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json
from types import MappingProxyType
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.binance_spot import (
    BinanceSpotAdapterError,
    BinanceSpotDepthCursor,
    BinanceSpotDepthRange,
    BinanceSpotOrderIntent,
    BinanceSpotPreparedRequest,
    BinanceSpotReferencePrice,
    BinanceSpotSymbolRules,
    parse_account_trades as parse_spot_account_trades,
    parse_order_ack as parse_spot_order_ack,
)
from mvp.autotrade_mvp.binance_usdm import (
    BinanceUsdmAdapterError,
    BinanceUsdmPreparedRequest,
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

    def test_ack_rejects_hostile_exact_dict_keys_before_equality_callbacks(self):
        callbacks = []

        class HostileKey(str):
            __hash__ = str.__hash__

            def __eq__(self, other):
                callbacks.append(other)
                raise AssertionError("hostile ACK key equality executed")

        cases = (
            (
                BinanceSpotAdapterError,
                parse_spot_order_ack,
                "spot-hostile-key",
                {
                    HostileKey("clientOrderId"): "spot-hostile-key",
                    "symbol": "BTCUSDT",
                    "orderId": 7,
                    "transactTime": 1791187200123,
                },
            ),
            (
                BinanceUsdmAdapterError,
                parse_usdm_order_ack,
                "usdm-hostile-key",
                {
                    HostileKey("clientOrderId"): "usdm-hostile-key",
                    "symbol": "BTCUSDT",
                    "orderId": 8,
                    "updateTime": 1791187200123,
                },
            ),
        )
        for error_type, parser, client_id, response in cases:
            with self.subTest(parser=parser.__name__), self.assertRaisesRegex(
                error_type,
                "keys must be exact decoded strings",
            ):
                parser(
                    attempt_id=str(uuid4()),
                    client_order_id=client_id,
                    response=response,
                )
        self.assertEqual(callbacks, [])

    def test_ack_rejects_nested_executable_json_values_before_callbacks(self):
        callbacks = []

        class HostileList(list):
            def __iter__(self):
                callbacks.append("iter")
                raise AssertionError("hostile ACK nested-list callback executed")

        cases = (
            (
                BinanceSpotAdapterError,
                parse_spot_order_ack,
                "spot-hostile-nested",
                {
                    "clientOrderId": "spot-hostile-nested",
                    "symbol": "BTCUSDT",
                    "orderId": 9,
                    "transactTime": 1791187200123,
                    "extra": HostileList(["value"]),
                },
            ),
            (
                BinanceUsdmAdapterError,
                parse_usdm_order_ack,
                "usdm-hostile-nested",
                {
                    "clientOrderId": "usdm-hostile-nested",
                    "symbol": "BTCUSDT",
                    "orderId": 10,
                    "updateTime": 1791187200123,
                    "extra": HostileList(["value"]),
                },
            ),
        )
        for error_type, parser, client_id, response in cases:
            with self.subTest(parser=parser.__name__), self.assertRaisesRegex(
                error_type,
                "exact decoded JSON values",
            ):
                parser(
                    attempt_id=str(uuid4()),
                    client_order_id=client_id,
                    response=response,
                )
        self.assertEqual(callbacks, [])

    def test_ack_rejects_aliased_or_cyclic_decoded_container_graphs(self):
        for error_type, parser, client_id, timestamp_name in (
            (
                BinanceSpotAdapterError,
                parse_spot_order_ack,
                "spot-cycle",
                "transactTime",
            ),
            (
                BinanceUsdmAdapterError,
                parse_usdm_order_ack,
                "usdm-cycle",
                "updateTime",
            ),
        ):
            extra = []
            extra.append(extra)
            response = {
                "clientOrderId": client_id,
                "symbol": "BTCUSDT",
                "orderId": 11,
                timestamp_name: 1791187200123,
                "extra": extra,
            }
            with self.subTest(parser=parser.__name__), self.assertRaisesRegex(
                error_type,
                "without aliases or cycles",
            ):
                parser(
                    attempt_id=str(uuid4()),
                    client_order_id=client_id,
                    response=response,
                )

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


    def test_spot_raw_provider_parsers_reject_mapping_subclass_before_callbacks(self):
        callbacks = []

        class HostileMapping(dict):
            def get(self, key, default=None):
                callbacks.append(("get", key))
                raise AssertionError("hostile provider mapping callback executed")

            def __iter__(self):
                callbacks.append(("iter", None))
                raise AssertionError("hostile provider mapping iteration executed")

        hostile = HostileMapping({})
        parsers = (
            lambda: BinanceSpotDepthRange.from_diff_depth_payload(hostile),
            lambda: BinanceSpotDepthCursor.from_snapshot(
                symbol="BTCUSDT",
                payload=hostile,
            ),
            lambda: BinanceSpotReferencePrice.from_reference_price_payload(
                instrument_version="BTCUSDT:v1",
                symbol="BTCUSDT",
                payload=hostile,
            ),
            lambda: BinanceSpotReferencePrice.from_average_price_payload(
                instrument_version="BTCUSDT:v1",
                symbol="BTCUSDT",
                payload=hostile,
            ),
            lambda: BinanceSpotReferencePrice.from_last_trade_payload(
                instrument_version="BTCUSDT:v1",
                symbol="BTCUSDT",
                payload=hostile,
            ),
            lambda: BinanceSpotSymbolRules.from_exchange_info(
                instrument_version="BTCUSDT:v1",
                symbol_payload=hostile,
            ),
        )
        for parse in parsers:
            with self.subTest(parse=parse):
                with self.assertRaisesRegex(
                    BinanceSpotAdapterError,
                    "exact decoded object",
                ):
                    parse()
        self.assertEqual(callbacks, [])

    def test_spot_exchange_info_rejects_list_subclass_before_iteration_callback(self):
        callbacks = []

        class HostileList(list):
            def __iter__(self):
                callbacks.append("iter")
                raise AssertionError("hostile filter-list callback executed")

        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "exact decoded array",
        ):
            BinanceSpotSymbolRules.from_exchange_info(
                instrument_version="BTCUSDT:v1",
                symbol_payload={
                    "symbol": "BTCUSDT",
                    "filters": HostileList([]),
                },
            )
        self.assertEqual(callbacks, [])

    def test_spot_exchange_info_rejects_filter_mapping_subclass_before_get_callback(self):
        callbacks = []

        class HostileFilter(dict):
            def get(self, key, default=None):
                callbacks.append(key)
                raise AssertionError("hostile filter mapping callback executed")

        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            r"exchangeInfo filter\[0\].*exact decoded object",
        ):
            BinanceSpotSymbolRules.from_exchange_info(
                instrument_version="BTCUSDT:v1",
                symbol_payload={
                    "symbol": "BTCUSDT",
                    "filters": [HostileFilter({"filterType": "PRICE_FILTER"})],
                },
            )
        self.assertEqual(callbacks, [])


    def test_prepared_requests_reject_caller_minted_mapping_before_callbacks(self):
        callbacks = []

        class HostileBody(dict):
            def items(self):
                callbacks.append("items")
                raise AssertionError("hostile prepared-body callback executed")

        cases = (
            (
                BinanceSpotAdapterError,
                lambda body: BinanceSpotPreparedRequest(
                    endpoint="/api/v3/order",
                    body=body,
                    capability_snapshot_id="cap-spot",
                    filter_source_sha256="sha256:" + "a" * 64,
                ),
            ),
            (
                BinanceUsdmAdapterError,
                lambda body: BinanceUsdmPreparedRequest(
                    endpoint="/fapi/v1/order",
                    body=body,
                    capability_snapshot_id="cap-usdm",
                    documentation_refs=("https://developers.binance.com/",),
                ),
            ),
        )
        for error_type, construct in cases:
            with self.subTest(error_type=error_type.__name__):
                with self.assertRaisesRegex(
                    error_type,
                    "canonical order preparation",
                ):
                    construct(HostileBody({"symbol": "BTCUSDT"}))
        self.assertEqual(callbacks, [])

    def test_prepared_requests_reject_caller_minted_text_before_callbacks(self):
        callbacks = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                callbacks.append("strip")
                raise AssertionError("hostile request text callback executed")

        cases = (
            (
                BinanceSpotAdapterError,
                lambda body: BinanceSpotPreparedRequest(
                    endpoint="/api/v3/order",
                    body=body,
                    capability_snapshot_id="cap-spot",
                    filter_source_sha256="sha256:" + "b" * 64,
                ),
            ),
            (
                BinanceUsdmAdapterError,
                lambda body: BinanceUsdmPreparedRequest(
                    endpoint="/fapi/v1/order",
                    body=body,
                    capability_snapshot_id="cap-usdm",
                    documentation_refs=("https://developers.binance.com/",),
                ),
            ),
        )
        for error_type, construct in cases:
            with self.subTest(error_type=error_type.__name__):
                with self.assertRaisesRegex(
                    error_type,
                    "canonical order preparation",
                ):
                    construct({"symbol": HostileText("BTCUSDT")})
        self.assertEqual(callbacks, [])


    def test_usdm_ack_rejects_noncanonical_digit_timestamp_alias(self):
        with self.assertRaisesRegex(
            BinanceUsdmAdapterError,
            "canonical non-negative integer millisecond timestamp",
        ):
            parse_usdm_order_ack(
                attempt_id=str(uuid4()),
                client_order_id="usdm-leading-zero-time",
                response={
                    "symbol": "BTCUSDT",
                    "orderId": 13,
                    "clientOrderId": "usdm-leading-zero-time",
                    "updateTime": "01791187200123",
                },
            )


    def test_spot_raw_provider_object_rejects_text_key_subclass_before_equality_callback(self):
        callbacks = []

        class HostileKey(str):
            __hash__ = str.__hash__

            def __eq__(self, other):
                callbacks.append(other)
                raise AssertionError("hostile provider-key equality executed")

        payload = {
            HostileKey("symbol"): "BTCUSDT",
            "referencePrice": "100",
            "timestamp": 1791187200123,
        }
        with self.assertRaisesRegex(
            BinanceSpotAdapterError,
            "keys must be exact decoded strings",
        ):
            BinanceSpotReferencePrice.from_reference_price_payload(
                instrument_version="BTCUSDT:v1",
                symbol="BTCUSDT",
                payload=payload,
            )
        self.assertEqual(callbacks, [])


    def test_spot_exchange_filters_are_decimal_context_invariant(self):
        def rules(*, minimum_notional=None):
            filters = [
                {
                    "filterType": "PRICE_FILTER",
                    "minPrice": "0",
                    "maxPrice": "0",
                    "tickSize": "0",
                },
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.0000000000000000001",
                    "maxQty": "100",
                    "stepSize": "0.0000000000000000001",
                },
            ]
            if minimum_notional is not None:
                filters.append(
                    {
                        "filterType": "MIN_NOTIONAL",
                        "minNotional": minimum_notional,
                        "applyToMarket": False,
                        "avgPriceMins": 0,
                    }
                )
            return BinanceSpotSymbolRules.from_exchange_info(
                instrument_version="BTCUSDT:v1",
                symbol_payload={"symbol": "BTCUSDT", "filters": filters},
            )

        def limit_intent(quantity):
            return BinanceSpotOrderIntent.create(
                instrument_version="BTCUSDT:v1",
                symbol="BTCUSDT",
                side="BUY",
                order_type="LIMIT",
                quantity=quantity,
                price="1",
                time_in_force="GTC",
            )

        exact_grid = rules()
        below_minimum = rules(minimum_notional="10")
        for precision in (5, 50):
            with self.subTest(precision=precision):
                with localcontext() as context:
                    context.prec = precision
                    # A large coefficient divided by a tiny step used to make
                    # Decimal remainder depend on the ambient context.
                    self.assertIsNone(
                        exact_grid.validate(
                            limit_intent("9.9999999999999999999"),
                            at=NOW,
                        )
                    )
                    # Low precision would round this product to 10 under plain
                    # Decimal multiplication; exact arithmetic must still
                    # reject the true 9.999... notional.
                    with self.assertRaisesRegex(
                        BinanceSpotAdapterError,
                        "notional is below exchangeInfo minimum",
                    ):
                        below_minimum.validate(
                            limit_intent("9.9999999999999999999"),
                            at=NOW,
                        )


if __name__ == "__main__":
    unittest.main()
