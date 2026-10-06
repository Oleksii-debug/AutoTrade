from datetime import datetime, timedelta, timezone
import json
import unittest

from mvp.autotrade_mvp.bybit_v5 import (
    BYBIT_OPTION_DELIVERY_PARSER_IDENTITY,
    BybitOptionDeliveryPage,
    parse_option_delivery_page,
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


NOW = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
ENDPOINT = "/v5/asset/delivery-record"
_ARTIFACT_IDS = {
    "DOCUMENTED": "91111111-1111-4111-8111-111111111111",
    "API": "92222222-2222-4222-8222-222222222222",
    "ACCOUNT": "93333333-3333-4333-8333-333333333333",
    "INSTRUMENT": "94444444-4444-4444-8444-444444444444",
}


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
            instrument_version="BTC-29DEC22-16000-P@1",
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
):
    binding = prepare_authenticated_read_query(
        capability=capability(),
        surface=surface,
        endpoint=endpoint,
        query=(
            {"category": "option", "symbol": "BTC-29DEC22-16000-P"}
            if query is None
            else query
        ),
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
        parsed = parse_option_delivery_page(observation(response()))
        self.assertIsInstance(parsed, BybitOptionDeliveryPage)
        self.assertEqual(len(parsed.records), 1)
        self.assertFalse(hasattr(parsed.records[0], "event_kind"))

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
                {"query": {"category": "linear", "symbol": "BTC-29DEC22-16000-P"}},
                "requires ACCOUNT.READ category=option",
            ),
            (
                {"query": {"category": "option"}},
                "requires an exact symbol filter",
            ),
            (
                {
                    "query": {
                        "category": "option",
                        "symbol": "BTC-29DEC22-16000-C",
                    }
                },
                "query symbol does not match instrument_version",
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

    def test_parser_binds_rows_to_requested_symbol(self):
        payload = response()
        payload["result"]["list"][0]["symbol"] = "ETH-29DEC22-1600-P"
        with self.assertRaisesRegex(
            ProviderCoreError,
            "violates requested symbol filter",
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

    def test_parser_rejects_instrument_version_aliasing_requested_symbol(self):
        cases = (
            "BTC-29DEC22-16000-P",
            "BTC-29DEC22-16000-P@",
            "btc-29DEC22-16000-P@1",
            "@1",
        )
        for instrument_version in cases:
            with self.subTest(instrument_version=instrument_version):
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
                        instrument_version=instrument_version,
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
                capability_snapshot = derive_capability_snapshot(
                    snapshot_id="99999999-9999-4999-8999-999999999999",
                    claims=claims,
                    observed_at=NOW - timedelta(minutes=1),
                    evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
                )
                prepared = prepare_authenticated_read_query(
                    capability=capability_snapshot,
                    surface=Surface.ACTIVITIES,
                    endpoint=ENDPOINT,
                    query={
                        "category": "option",
                        "symbol": "BTC-29DEC22-16000-P",
                    },
                    at=NOW,
                    permission_scope="ACCOUNT.READ",
                )
                source = observe_authenticated_json_response(
                    query_binding=prepared,
                    http_status=200,
                    response_bytes=json.dumps(
                        response(),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8"),
                    observed_at=NOW + timedelta(seconds=1),
                )
                with self.assertRaisesRegex(
                    ProviderCoreError,
                    "(?:instrument_version is not canonical|query symbol does not match instrument_version)",
                ):
                    parse_option_delivery_page(source)

    def test_parser_binds_rows_to_requested_time_window(self):
        delivery_time = response()["result"]["list"][0]["deliveryTime"]
        cases = (
            (
                {
                    "category": "option",
                    "startTime": str(delivery_time + 1),
                },
                "violates requested time range",
            ),
            (
                {
                    "category": "option",
                    "endTime": str(delivery_time - 1),
                },
                "violates requested time range",
            ),
            (
                {
                    "category": "option",
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
                    query={"category": "option", "expDate": "28DEC22"},
                )
            )

        parsed = parse_option_delivery_page(
            observation(
                response(),
                query={"category": "option", "expDate": "29DEC22"},
            )
        )
        self.assertEqual(parsed.records[0].symbol, "BTC-29DEC22-16000-P")

    def test_parser_rejects_unqualified_query_shape(self):
        with self.assertRaisesRegex(
            ProviderCoreError,
            "unsupported query fields",
        ):
            parse_option_delivery_page(
                observation(
                    response(),
                    query={"category": "option", "accountType": "UNIFIED"},
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
