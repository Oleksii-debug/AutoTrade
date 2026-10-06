from datetime import datetime, timezone
from decimal import Decimal
import json
import unittest

from mvp.autotrade_mvp.bybit_margin_market import (
    BYBIT_MARGIN_MARKET_PARSER_CONTRACT_DIGEST,
    BYBIT_MARGIN_MARKET_PARSER_IDENTITY,
    BYBIT_MARGIN_RISK_LIMIT_ENDPOINT,
    BYBIT_MARGIN_TICKER_ENDPOINT,
    BybitMarginMarketError,
    parse_bybit_margin_risk_limits,
    parse_bybit_margin_ticker,
)
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)
from mvp.tests.test_bybit_v5 import read_capability


NOW = datetime(2026, 10, 6, 11, 0, tzinfo=timezone.utc)


def _binding(
    *,
    endpoint,
    category="linear",
    symbol="BTCUSDT",
    permission_scope="MARKET.READ",
):
    capability = read_capability(
        account_id="paper-margin-market",
        environment="PAPER",
        provider_environment="TESTNET",
        instrument_version=f"{symbol}@v1",
        permission_scopes=(permission_scope,),
        at=NOW,
    )
    return prepare_authenticated_read_query(
        capability=capability,
        surface=Surface.PUBLIC_DATA,
        endpoint=endpoint,
        query={"category": category, "symbol": symbol},
        at=NOW,
        permission_scope=permission_scope,
    )


def _observe(*, binding, raw):
    return observe_authenticated_json_response(
        query_binding=binding,
        http_status=200,
        response_bytes=raw,
        observed_at=NOW,
    )


def _ticker_response(
    *,
    category="linear",
    symbol="BTCUSDT",
    mark="65000.125",
    index="64990.5",
    permission_scope="MARKET.READ",
    rows=None,
):
    binding = _binding(
        endpoint=BYBIT_MARGIN_TICKER_ENDPOINT,
        category=category,
        symbol=symbol,
        permission_scope=permission_scope,
    )
    if rows is None:
        rows = [{"symbol": symbol, "markPrice": mark, "indexPrice": index}]
    raw = json.dumps(
        {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"category": category, "list": rows},
            "retExtInfo": {},
            "time": 1791280800123,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _observe(binding=binding, raw=raw)


def _risk_raw(
    rows_json,
    *,
    category="linear",
    cursor="",
):
    return (
        "{"
        '"retCode":0,"retMsg":"OK","result":{'
        f'"category":"{category}","list":[{rows_json}],'
        f'"nextPageCursor":"{cursor}"'
        '},"retExtInfo":{},"time":1791280800456}'
    ).encode("utf-8")


def _risk_row_json(
    *,
    risk_id=1,
    symbol="BTCUSDT",
    limit="2000000",
    maintenance="0.5",
    initial="1",
    lowest=1,
    max_leverage="100.00",
    deduction="",
):
    return (
        "{"
        f'"id":{risk_id},'
        f'"symbol":"{symbol}",'
        f'"riskLimitValue":"{limit}",'
        f'"maintenanceMargin":{maintenance},'
        f'"initialMargin":{initial},'
        f'"isLowestRisk":{lowest},'
        f'"maxLeverage":"{max_leverage}",'
        f'"mmDeduction":"{deduction}"'
        "}"
    )


def _risk_response(
    rows_json=None,
    *,
    category="linear",
    symbol="BTCUSDT",
    cursor="",
    permission_scope="MARKET.READ",
):
    if rows_json is None:
        rows_json = _risk_row_json()
    binding = _binding(
        endpoint=BYBIT_MARGIN_RISK_LIMIT_ENDPOINT,
        category=category,
        symbol=symbol,
        permission_scope=permission_scope,
    )
    return _observe(
        binding=binding,
        raw=_risk_raw(rows_json, category=category, cursor=cursor),
    )


class BybitMarginMarketParserTests(unittest.TestCase):
    def test_public_data_query_is_neutral_exact_byte_surface(self):
        binding = _binding(endpoint=BYBIT_MARGIN_TICKER_ENDPOINT)
        self.assertIs(binding.surface, Surface.PUBLIC_DATA)
        self.assertEqual(binding.permission_scope, "MARKET.READ")
        self.assertEqual(
            dict(binding.query),
            {"category": "linear", "symbol": "BTCUSDT"},
        )

    def test_ticker_preserves_mark_index_and_response_identity(self):
        observation = _ticker_response()
        fact = parse_bybit_margin_ticker(observation)
        self.assertEqual(fact.provider_id, "BYBIT")
        self.assertEqual(fact.account_id, "paper-margin-market")
        self.assertEqual(fact.environment, "PAPER")
        self.assertEqual(fact.instrument_version, "BTCUSDT@v1")
        self.assertEqual(fact.category, "linear")
        self.assertEqual(fact.symbol, "BTCUSDT")
        self.assertEqual(fact.mark_price, Decimal("65000.125"))
        self.assertEqual(fact.index_price, Decimal("64990.5"))
        self.assertEqual(fact.provider_time_ms, 1791280800123)
        self.assertEqual(fact.query_digest, observation.query_binding.query_digest)
        self.assertEqual(fact.evidence_ref, observation.evidence_ref)
        self.assertEqual(fact.response_sha256, observation.response_sha256)
        self.assertEqual(fact.parser_identity, BYBIT_MARGIN_MARKET_PARSER_IDENTITY)
        self.assertEqual(
            fact.parser_contract_digest,
            BYBIT_MARGIN_MARKET_PARSER_CONTRACT_DIGEST,
        )

    def test_ticker_requires_exact_single_symbol_row(self):
        observation = _ticker_response(
            rows=[
                {"symbol": "BTCUSDT", "markPrice": "1", "indexPrice": "1"},
                {"symbol": "BTCUSDT", "markPrice": "1", "indexPrice": "1"},
            ]
        )
        with self.assertRaisesRegex(
            BybitMarginMarketError,
            "exactly one row",
        ):
            parse_bybit_margin_ticker(observation)

    def test_ticker_rejects_symbol_substitution(self):
        with self.assertRaisesRegex(
            BybitMarginMarketError,
            "symbol must match exact query symbol",
        ):
            parse_bybit_margin_ticker(
                _ticker_response(
                    rows=[
                        {
                            "symbol": "ETHUSDT",
                            "markPrice": "65000",
                            "indexPrice": "64990",
                        }
                    ]
                )
            )

    def test_ticker_prices_must_be_provider_decimal_text(self):
        with self.assertRaisesRegex(
            BybitMarginMarketError,
            "markPrice must be provider decimal text",
        ):
            parse_bybit_margin_ticker(
                _ticker_response(
                    rows=[
                        {
                            "symbol": "BTCUSDT",
                            "markPrice": 65000,
                            "indexPrice": "64990",
                        }
                    ]
                )
            )

    def test_public_margin_fact_requires_market_read_permission(self):
        observation = _ticker_response(permission_scope="ORDER.READ")
        with self.assertRaisesRegex(
            BybitMarginMarketError,
            "MARKET.READ",
        ):
            parse_bybit_margin_ticker(observation)

    def test_risk_limit_preserves_provider_rates_without_unit_conversion(self):
        observation = _risk_response(
            ",".join(
                [
                    _risk_row_json(),
                    _risk_row_json(
                        risk_id=2,
                        limit="4000000",
                        maintenance="1.25",
                        initial="2",
                        lowest=0,
                        max_leverage="50",
                        deduction="10000.5",
                    ),
                ]
            )
        )
        page = parse_bybit_margin_risk_limits(observation)
        self.assertEqual(page.provider_id, "BYBIT")
        self.assertEqual(page.category, "linear")
        self.assertEqual(page.symbol, "BTCUSDT")
        self.assertEqual(page.provider_time_ms, 1791280800456)
        self.assertEqual(page.query_digest, observation.query_binding.query_digest)
        self.assertEqual(page.evidence_ref, observation.evidence_ref)
        self.assertEqual(len(page.tiers), 2)
        first, second = page.tiers
        self.assertEqual(first.risk_id, 1)
        self.assertEqual(first.risk_limit_value, Decimal("2000000"))
        self.assertEqual(first.maintenance_margin, Decimal("0.5"))
        self.assertEqual(first.initial_margin, Decimal("1"))
        self.assertTrue(first.is_lowest_risk)
        self.assertEqual(first.max_leverage, Decimal("100.00"))
        self.assertIsNone(first.maintenance_margin_deduction)
        self.assertEqual(second.maintenance_margin, Decimal("1.25"))
        self.assertEqual(second.initial_margin, Decimal("2"))
        self.assertEqual(
            second.maintenance_margin_deduction,
            Decimal("10000.5"),
        )

    def test_risk_limit_requires_complete_single_symbol_page(self):
        with self.assertRaisesRegex(
            BybitMarginMarketError,
            "complete page",
        ):
            parse_bybit_margin_risk_limits(
                _risk_response(cursor="next-provider-cursor")
            )

    def test_risk_limit_rejects_duplicate_risk_identity(self):
        rows = ",".join(
            [
                _risk_row_json(),
                _risk_row_json(
                    risk_id=1,
                    limit="4000000",
                    lowest=0,
                ),
            ]
        )
        with self.assertRaisesRegex(
            BybitMarginMarketError,
            "duplicate risk id",
        ):
            parse_bybit_margin_risk_limits(_risk_response(rows))

    def test_risk_limit_rejects_duplicate_position_limit(self):
        rows = ",".join(
            [
                _risk_row_json(),
                _risk_row_json(
                    risk_id=2,
                    limit="2000000",
                    lowest=0,
                ),
            ]
        )
        with self.assertRaisesRegex(
            BybitMarginMarketError,
            "duplicate position limit",
        ):
            parse_bybit_margin_risk_limits(_risk_response(rows))

    def test_risk_limit_requires_exactly_one_lowest_risk_tier(self):
        for lowest_values in ((0, 0), (1, 1)):
            with self.subTest(lowest_values=lowest_values):
                rows = ",".join(
                    [
                        _risk_row_json(lowest=lowest_values[0]),
                        _risk_row_json(
                            risk_id=2,
                            limit="4000000",
                            lowest=lowest_values[1],
                        ),
                    ]
                )
                with self.assertRaisesRegex(
                    BybitMarginMarketError,
                    "exactly one lowest-risk tier",
                ):
                    parse_bybit_margin_risk_limits(_risk_response(rows))

    def test_risk_limit_rejects_cross_symbol_row(self):
        with self.assertRaisesRegex(
            BybitMarginMarketError,
            "symbol must match exact query symbol",
        ):
            parse_bybit_margin_risk_limits(
                _risk_response(_risk_row_json(symbol="ETHUSDT"))
            )

    def test_risk_limit_rate_accepts_documented_decimal_text_representation(self):
        row = _risk_row_json(maintenance='"0.5"', initial='"1"')
        page = parse_bybit_margin_risk_limits(_risk_response(row))
        self.assertEqual(page.tiers[0].maintenance_margin, Decimal("0.5"))
        self.assertEqual(page.tiers[0].initial_margin, Decimal("1"))

    def test_risk_limit_rate_rejects_non_numeric_json_scalar(self):
        row = _risk_row_json().replace(
            '"maintenanceMargin":0.5',
            '"maintenanceMargin":true',
        )
        with self.assertRaisesRegex(
            BybitMarginMarketError,
            "decimal text or JSON number",
        ):
            parse_bybit_margin_risk_limits(_risk_response(row))

    def test_risk_limit_category_is_query_bound(self):
        binding = _binding(
            endpoint=BYBIT_MARGIN_RISK_LIMIT_ENDPOINT,
            category="linear",
        )
        raw = _risk_raw(_risk_row_json(), category="inverse")
        observation = _observe(binding=binding, raw=raw)
        with self.assertRaisesRegex(
            BybitMarginMarketError,
            "category must match",
        ):
            parse_bybit_margin_risk_limits(observation)

    def test_wrong_public_endpoint_cannot_be_relabelled_as_ticker(self):
        observation = _risk_response()
        with self.assertRaisesRegex(
            Exception,
            "endpoint mismatch",
        ):
            parse_bybit_margin_ticker(observation)


if __name__ == "__main__":
    unittest.main()
