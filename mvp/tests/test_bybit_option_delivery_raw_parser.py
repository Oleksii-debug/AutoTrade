from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest

from mvp.autotrade_mvp.bybit_v5 import (
    BYBIT_OPTION_DELIVERY_PARSER_CONTRACT_DIGEST,
    BYBIT_OPTION_DELIVERY_PARSER_IDENTITY,
    BYBIT_OPTION_DELIVERY_PARSER_VERSION,
    BybitOptionDeliveryPage,
    parse_option_delivery_page as _parse_option_delivery_page,
)
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.instruments import InstrumentRegistry, InstrumentVersion
from mvp.autotrade_mvp.provider_core import (
    ProviderCoreError,
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)


NOW = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
ENDPOINT = "/v5/asset/delivery-record"
DELIVERY_TIME_MS = 1672300800860
_INSTRUMENT_VERSION = "95555555-5555-4555-8555-555555555555@1"
_ARTIFACT_IDS = {
    "DOCUMENTED": "91111111-1111-4111-8111-111111111111",
    "API": "92222222-2222-4222-8222-222222222222",
    "ACCOUNT": "93333333-3333-4333-8333-333333333333",
    "INSTRUMENT": "94444444-4444-4444-8444-444444444444",
}


def option_registry(
    *,
    provider_id="BYBIT",
    venue_id="OPTIONS",
    asset_class="OPTION",
    provider_symbol="BTC-29DEC22-16000-P",
    instrument_id="95555555-5555-4555-8555-555555555555",
):
    version = InstrumentVersion(
        instrument_id=instrument_id,
        version=1,
        provider_id=provider_id,
        venue_id=venue_id,
        provider_symbol=provider_symbol,
        asset_class=asset_class,
        base_currency="BTC",
        quote_currency="USDC",
        settlement_currency="USDC",
        quantity_unit="contract",
        contract_multiplier=Decimal("1"),
        price_tick=Decimal("0.01"),
        quantity_step=Decimal("0.01"),
        minimum_quantity=Decimal("0.01"),
        calendar_id="CONTINUOUS_24_7",
        timezone_id="UTC",
        effective_from=datetime(2022, 1, 1, tzinfo=timezone.utc),
        status="EXPIRED",
        payoff="OPTION",
        underlying_id="96666666-6666-4666-8666-666666666666@1",
        expiry=datetime(2022, 12, 29, 8, tzinfo=timezone.utc),
        delivery_cutoff=datetime(2022, 12, 29, 8, tzinfo=timezone.utc),
        settlement_method="CASH",
        margin_model_id="bybit-option-test-v1",
        strike=Decimal("16000"),
        option_right="PUT",
        exercise_style="EUROPEAN",
    )
    return InstrumentRegistry(versions=(version,))


def parse_option_delivery_page(observation_value, *, instrument_registry=None):
    if instrument_registry is None:
        instrument_registry = option_registry()
    return _parse_option_delivery_page(
        observation_value,
        instrument_registry=instrument_registry,
    )


def capability():
    observed = NOW - timedelta(minutes=2)
    expires = NOW + timedelta(minutes=10)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BYBIT",
            account_id="acct-option",
            entity_id="option-lifecycle-btc",
            environment="PAPER",
            provider_environment="TESTNET",
            instrument_version=_INSTRUMENT_VERSION,
            observed_at=observed,
            expires_at=expires,
            supported_order_types=frozenset({"LIMIT"}),
            time_in_force=frozenset({"GTC"}),
            permission_scopes=frozenset({"ACCOUNT.READ"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="bybit-option-delivery-test-v1",
            data_entitlements=frozenset({"ACTIVITIES"}),
            evidence_ref={
                "artifact_id": _ARTIFACT_IDS[source],
                "sha256": "sha256:" + "b" * 64,
                "observed_at": observed.isoformat().replace("+00:00", "Z"),
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id="99999999-9999-4999-8999-999999999999",
        claims=claims,
        observed_at=NOW - timedelta(minutes=1),
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def observation(
    payload,
    *,
    endpoint=ENDPOINT,
    surface=Surface.ACTIVITIES,
    permission_scope="ACCOUNT.READ",
    query=None,
    include_time_window=True,
):
    effective_query = (
        {
            "category": "option",
            "symbol": "BTC-29DEC22-16000-P",
            "startTime": str(DELIVERY_TIME_MS - 1000),
            "endTime": str(DELIVERY_TIME_MS + 1000),
        }
        if query is None
        else dict(query)
    )
    if (
        include_time_window
        and "startTime" not in effective_query
        and "endTime" not in effective_query
    ):
        effective_query["startTime"] = str(DELIVERY_TIME_MS - 1000)
        effective_query["endTime"] = str(DELIVERY_TIME_MS + 1000)
    binding = prepare_authenticated_read_query(
        capability=capability(),
        surface=surface,
        endpoint=endpoint,
        query=effective_query,
        at=NOW,
        permission_scope=permission_scope,
    )
    return observe_authenticated_json_response(
        query_binding=binding,
        http_status=200,
        response_bytes=json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        observed_at=NOW + timedelta(seconds=1),
    )


def response(*, entry_price="150.25", cursor="132791%3A0%2C132791%3A0"):
    row = {
        "symbol": "BTC-29DEC22-16000-P",
        "side": "Buy",
        "deliveryTime": 1672300800860,
        "strike": "16000",
        "fee": "0.00000000",
        "position": "0.01",
        "deliveryPrice": "16541.86369547",
        "deliveryRpl": "-3.5",
    }
    if entry_price is not None:
        row["entryPrice"] = entry_price
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "nextPageCursor": cursor,
            "category": "option",
            "list": [row],
        },
        "retExtInfo": {},
        "time": 1672362116184,
    }


class BybitOptionDeliveryRawParserTests(unittest.TestCase):
    def test_parser_identity_is_explicit_and_does_not_claim_lifecycle_kind(self):
        self.assertEqual(
            BYBIT_OPTION_DELIVERY_PARSER_IDENTITY,
            "BYBIT_OPTION_DELIVERY_V5_JSON_V1",
        )
        self.assertEqual(BYBIT_OPTION_DELIVERY_PARSER_VERSION, "1.2.0")
        self.assertEqual(
            BYBIT_OPTION_DELIVERY_PARSER_CONTRACT_DIGEST,
            "sha256:89ec7fade832492c068d3f67e11e6a6ff77764e59950c2195c9e8848920c3e3f",
        )
        parsed = parse_option_delivery_page(observation(response()))
        self.assertIsInstance(parsed, BybitOptionDeliveryPage)
        self.assertEqual(len(parsed.records), 1)
        self.assertFalse(hasattr(parsed.records[0], "event_kind"))

    def test_parser_binds_provider_symbol_to_canonical_instrument_version(self):
        parsed = parse_option_delivery_page(observation(response()))
        self.assertEqual(parsed.instrument_version, _INSTRUMENT_VERSION)
        self.assertEqual(parsed.provider_symbol, "BTC-29DEC22-16000-P")

        for registry, message in (
            (
                option_registry(provider_symbol="ETH-29DEC22-1000-P"),
                "symbol does not match canonical instrument_version",
            ),
            (
                option_registry(provider_id="OTHER"),
                "symbol does not match canonical instrument_version",
            ),
            (
                option_registry(venue_id="OTHER"),
                "symbol does not match canonical instrument_version",
            ),
            (
                option_registry(
                    instrument_id="97777777-7777-4777-8777-777777777777"
                ),
                "instrument_version is not present in canonical registry",
            ),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                ProviderCoreError,
                message,
            ):
                parse_option_delivery_page(
                    observation(response()),
                    instrument_registry=registry,
                )

    def test_parser_rejects_registry_subclass_before_virtual_lookup(self):
        class HostileRegistry(InstrumentRegistry):
            exact_called = False

            def exact(self, instrument_version):
                type(self).exact_called = True
                raise AssertionError("hostile registry callback executed")

        forged = object.__new__(HostileRegistry)
        with self.assertRaisesRegex(
            TypeError,
            "instrument_registry must be exact InstrumentRegistry",
        ):
            _parse_option_delivery_page(
                observation(response()),
                instrument_registry=forged,
            )
        self.assertFalse(HostileRegistry.exact_called)

    def test_parser_class_qualifies_instrument_registry_lookup(self):
        registry = option_registry()
        registry.exact = lambda _instrument_version: (_ for _ in ()).throw(
            AssertionError("instance method shadow must not execute")
        )
        parsed = parse_option_delivery_page(
            observation(response()),
            instrument_registry=registry,
        )
        self.assertEqual(parsed.instrument_version, _INSTRUMENT_VERSION)

    def test_documented_delivery_row_preserves_exact_provider_facts(self):
        source = observation(response())
        parsed = parse_option_delivery_page(source)
        record = parsed.records[0]

        self.assertEqual(record.delivery_time_ms, 1672300800860)
        self.assertEqual(record.symbol, "BTC-29DEC22-16000-P")
        self.assertEqual(record.side, "Buy")
        self.assertEqual(record.position, "0.01")
        self.assertEqual(record.entry_price, "150.25")
        self.assertEqual(record.delivery_price, "16541.86369547")
        self.assertEqual(record.strike, "16000")
        self.assertEqual(record.fee, "0.00000000")
        self.assertEqual(record.delivery_rpl, "-3.5")
        self.assertEqual(
            parsed.next_page_cursor,
            "132791%3A0%2C132791%3A0",
        )
        self.assertEqual(parsed.provider_id, source.provider_id)
        self.assertEqual(parsed.account_id, source.account_id)
        self.assertEqual(parsed.entity_id, source.query_binding.entity_id)
        self.assertEqual(parsed.environment, source.environment)
        self.assertEqual(
            parsed.capability_snapshot_id,
            source.query_binding.capability_snapshot_id,
        )
        self.assertEqual(
            parsed.instrument_version,
            source.query_binding.instrument_version,
        )
        self.assertEqual(parsed.provider_symbol, "BTC-29DEC22-16000-P")
        self.assertIs(parsed.surface, Surface.ACTIVITIES)
        self.assertEqual(parsed.endpoint, ENDPOINT)
        self.assertEqual(parsed.permission_scope, "ACCOUNT.READ")
        self.assertEqual(parsed.query_digest, source.query_binding.query_digest)
        self.assertEqual(
            parsed.parser_identity,
            BYBIT_OPTION_DELIVERY_PARSER_IDENTITY,
        )
        self.assertEqual(
            parsed.parser_version,
            BYBIT_OPTION_DELIVERY_PARSER_VERSION,
        )
        self.assertEqual(
            parsed.parser_contract_digest,
            BYBIT_OPTION_DELIVERY_PARSER_CONTRACT_DIGEST,
        )
        self.assertRegex(
            parsed.parser_contract_digest,
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertEqual(parsed.evidence_ref, source.evidence_ref)
        self.assertEqual(parsed.response_sha256, source.response_sha256)
        self.assertEqual(parsed.observed_at, source.observed_at)

    def test_entry_price_is_optional_for_historical_provider_rows(self):
        parsed = parse_option_delivery_page(
            observation(response(entry_price=None, cursor=""))
        )
        self.assertIsNone(parsed.records[0].entry_price)
        self.assertEqual(parsed.next_page_cursor, "")

        payload = response()
        payload["result"]["list"][0]["entryPrice"] = None
        with self.assertRaisesRegex(
            ProviderCoreError,
            "entryPrice must be canonical provider decimal text",
        ):
            parse_option_delivery_page(observation(payload))

    def test_nonzero_provider_result_is_rejected(self):
        payload = response()
        payload["retCode"] = 10001
        with self.assertRaisesRegex(
            ProviderCoreError,
            "requires exact integer retCode=0",
        ):
            parse_option_delivery_page(observation(payload))

        payload = response()
        payload["retCode"] = "0"
        with self.assertRaisesRegex(
            ProviderCoreError,
            "requires exact integer retCode=0",
        ):
            parse_option_delivery_page(observation(payload))

    def test_result_category_must_remain_option(self):
        payload = response()
        payload["result"]["category"] = "linear"
        with self.assertRaisesRegex(
            ProviderCoreError,
            "result category must be option",
        ):
            parse_option_delivery_page(observation(payload))

    def test_parser_requires_exact_delivery_scope_and_option_query(self):
        cases = (
            (
                {"endpoint": "/v5/account/transaction-log"},
                "provenance endpoint mismatch",
            ),
            (
                {"surface": Surface.AUTHENTICATED_READ},
                "provenance surface mismatch",
            ),
            (
                {
                    "query": {
                        "category": "linear",
                        "symbol": "BTC-29DEC22-16000-P",
                    }
                },
                "requires ACCOUNT.READ category=option",
            ),
            (
                {"query": {"category": "option"}},
                "query symbol is required for bounded delivery evidence",
            ),
            (
                {
                    "query": {
                        "category": "option",
                        "symbol": "BTC-29DEC22-16000-C",
                    }
                },
                "violates bound instrument symbol",
            ),
        )
        for kwargs, message in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ProviderCoreError, message):
                    parse_option_delivery_page(
                        observation(response(), **kwargs)
                    )

    def test_parser_rejects_delivery_row_schema_drift(self):
        payload = response()
        payload["result"]["list"][0]["unexpectedField"] = "surprise"
        with self.assertRaisesRegex(
            ProviderCoreError,
            "fields do not match the qualified delivery schema",
        ):
            parse_option_delivery_page(observation(payload))

        payload = response()
        del payload["result"]["list"][0]["strike"]
        with self.assertRaisesRegex(
            ProviderCoreError,
            "fields do not match the qualified delivery schema",
        ):
            parse_option_delivery_page(observation(payload))

    def test_parser_requires_explicit_symbol_binding(self):
        with self.assertRaisesRegex(
            ProviderCoreError,
            "query symbol is required for bounded delivery evidence",
        ):
            parse_option_delivery_page(
                observation(
                    response(),
                    query={"category": "option"},
                )
            )

    def test_parser_binds_rows_to_requested_symbol(self):
        payload = response()
        payload["result"]["list"][0]["symbol"] = "ETH-29DEC22-1600-P"
        with self.assertRaisesRegex(
            ProviderCoreError,
            "violates bound instrument symbol",
        ):
            parse_option_delivery_page(
                observation(
                    payload,
                    query={
                        "category": "option",
                        "symbol": "BTC-29DEC22-16000-P",
                    },
                )
            )

    def test_parser_preserves_canonical_instrument_version_identity(self):
        parsed = parse_option_delivery_page(observation(response()))
        self.assertEqual(parsed.instrument_version, _INSTRUMENT_VERSION)

    def test_parser_requires_explicit_time_window(self):
        with self.assertRaisesRegex(
            ProviderCoreError,
            "must include explicit startTime or endTime",
        ):
            parse_option_delivery_page(
                observation(response(), include_time_window=False)
            )

    def test_parser_binds_rows_to_requested_time_window(self):
        delivery_time = response()["result"]["list"][0]["deliveryTime"]
        cases = (
            (
                {
                    "category": "option",
                    "symbol": "BTC-29DEC22-16000-P",
                    "startTime": str(delivery_time + 1),
                },
                "violates requested time range",
            ),
            (
                {
                    "category": "option",
                    "symbol": "BTC-29DEC22-16000-P",
                    "endTime": str(delivery_time - 1),
                },
                "violates requested time range",
            ),
            (
                {
                    "category": "option",
                    "symbol": "BTC-29DEC22-16000-P",
                    "startTime": str(delivery_time - 1),
                    "endTime": str(delivery_time + 1),
                },
                None,
            ),
        )
        for query, message in cases:
            with self.subTest(query=query):
                if message is None:
                    parsed = parse_option_delivery_page(
                        observation(response(), query=query)
                    )
                    self.assertEqual(
                        parsed.records[0].delivery_time_ms,
                        delivery_time,
                    )
                else:
                    with self.assertRaisesRegex(ProviderCoreError, message):
                        parse_option_delivery_page(
                            observation(response(), query=query)
                        )

    def test_parser_binds_rows_to_requested_expiry(self):
        with self.assertRaisesRegex(
            ProviderCoreError,
            "violates requested expiry filter",
        ):
            parse_option_delivery_page(
                observation(
                    response(),
                    query={
                        "category": "option",
                        "symbol": "BTC-29DEC22-16000-P",
                        "expDate": "28DEC22",
                    },
                )
            )

        parsed = parse_option_delivery_page(
            observation(
                response(),
                query={
                    "category": "option",
                    "symbol": "BTC-29DEC22-16000-P",
                    "expDate": "29DEC22",
                },
            )
        )
        self.assertEqual(parsed.records[0].symbol, "BTC-29DEC22-16000-P")

    def test_parser_enforces_limit_cursor_and_calendar_expiry(self):
        valid_cursor = "132791%3A0%2C132791%3A0"
        for query in (
            {
                "category": "option",
                "symbol": "BTC-29DEC22-16000-P",
                "limit": "0",
            },
            {
                "category": "option",
                "symbol": "BTC-29DEC22-16000-P",
                "limit": "51",
            },
            {
                "category": "option",
                "symbol": "BTC-29DEC22-16000-P",
                "limit": "01",
            },
            {
                "category": "option",
                "symbol": "BTC-29DEC22-16000-P",
                "cursor": "",
            },
            {
                "category": "option",
                "symbol": "BTC-29DEC22-16000-P",
                "cursor": "%3a",
            },
        ):
            with self.subTest(query=query):
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    "(?:limit must be between 1 and 50|limit must be canonical integer text|query cursor is non-canonical)",
                ):
                    parse_option_delivery_page(observation(response(), query=query))

        for exp_date, message in (
            ("29FEB23", "non-existent calendar date"),
            ("31APR23", "non-existent calendar date"),
        ):
            with self.subTest(exp_date=exp_date):
                with self.assertRaisesRegex(ProviderCoreError, message):
                    parse_option_delivery_page(
                        observation(
                            response(),
                            query={
                                "category": "option",
                                "symbol": "BTC-29DEC22-16000-P",
                                "expDate": exp_date,
                            },
                        )
                    )

        parsed = parse_option_delivery_page(
            observation(
                response(),
                query={
                    "category": "option",
                    "symbol": "BTC-29DEC22-16000-P",
                    "limit": "1",
                    "cursor": valid_cursor,
                    "expDate": "29DEC22",
                },
            )
        )
        self.assertEqual(len(parsed.records), 1)

        payload = response()
        payload["result"]["list"].append(dict(payload["result"]["list"][0]))
        with self.assertRaisesRegex(
            ProviderCoreError,
            "exceeds requested limit",
        ):
            parse_option_delivery_page(
                observation(
                    payload,
                    query={
                        "category": "option",
                        "symbol": "BTC-29DEC22-16000-P",
                        "limit": "1",
                    },
                )
            )

    def test_parser_rejects_unqualified_query_shape(self):
        with self.assertRaisesRegex(
            ProviderCoreError,
            "unsupported query fields",
        ):
            parse_option_delivery_page(
                observation(
                    response(),
                    query={
                        "category": "option",
                        "symbol": "BTC-29DEC22-16000-P",
                        "accountType": "UNIFIED",
                    },
                )
            )

    def test_parser_rejects_non_string_economic_fields(self):
        for field, value in (
            ("position", 0.01),
            ("entryPrice", 150.25),
            ("deliveryPrice", 16541.86),
            ("strike", 16000),
            ("fee", 0),
            ("deliveryRpl", -3.5),
        ):
            with self.subTest(field=field):
                payload = response()
                payload["result"]["list"][0][field] = value
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    "canonical provider decimal text",
                ):
                    parse_option_delivery_page(observation(payload))

    def test_parser_rejects_noncanonical_decimal_text(self):
        for value in ("+1", "01", "1e-8", " 1", "1 ", "NaN", "Infinity"):
            with self.subTest(value=value):
                payload = response()
                payload["result"]["list"][0]["deliveryRpl"] = value
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    "canonical provider decimal text",
                ):
                    parse_option_delivery_page(observation(payload))

    def test_parser_rejects_invalid_side_and_delivery_time(self):
        payload = response()
        payload["result"]["list"][0]["side"] = "BUY"
        with self.assertRaisesRegex(
            ProviderCoreError,
            "side must be Buy or Sell",
        ):
            parse_option_delivery_page(observation(payload))

        for value in (-1, "1672300800860", True):
            with self.subTest(delivery_time=value):
                payload = response()
                payload["result"]["list"][0]["deliveryTime"] = value
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    "deliveryTime must be an exact non-negative integer",
                ):
                    parse_option_delivery_page(observation(payload))

    def test_parser_rejects_oversized_requested_symbol(self):
        payload = response()
        oversized = "A" * 161
        with self.assertRaisesRegex(
            ProviderCoreError,
            "query symbol is non-canonical",
        ):
            parse_option_delivery_page(
                observation(
                    payload,
                    query={
                        "category": "option",
                        "symbol": oversized,
                    },
                )
            )

    def test_parser_rejects_symbol_that_needs_normalization(self):
        for symbol in (
            " BTC-29DEC22-16000-P",
            "BTC-29DEC22-16000-P ",
            "btc-29DEC22-16000-P",
        ):
            with self.subTest(symbol=symbol):
                payload = response()
                payload["result"]["list"][0]["symbol"] = symbol
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    "symbol is non-canonical",
                ):
                    parse_option_delivery_page(observation(payload))

    def test_parser_rejects_noncanonical_response_cursor(self):
        for cursor in ("a&b", "%3a", "%ZZ", "two words"):
            with self.subTest(cursor=cursor):
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    "nextPageCursor is non-canonical",
                ):
                    parse_option_delivery_page(
                        observation(response(cursor=cursor))
                    )


if __name__ == "__main__":
    unittest.main()
