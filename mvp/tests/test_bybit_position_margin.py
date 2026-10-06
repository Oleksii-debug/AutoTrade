from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from types import SimpleNamespace
import unittest

import mvp.autotrade_mvp.provider_route_reads as provider_route_reads_module
from mvp.autotrade_mvp.bybit_position_margin import (
    BYBIT_POSITION_MARGIN_PARSER_CONTRACT_DIGEST,
    BYBIT_POSITION_MARGIN_PARSER_IDENTITY,
    BybitPositionMarginError,
    parse_bybit_position_margin_page,
)
from mvp.autotrade_mvp.persistence import canonical_json
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)
from mvp.autotrade_mvp.provider_route_reads import (
    ProviderRouteReadError,
    qualified_read_parser_semantic_claim,
    qualified_read_route_semantic_claim,
)
from mvp.tests.test_bybit_v5 import read_capability


NOW = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)


def _response(
    row,
    *,
    category="linear",
    query_symbol="BTCUSDT",
    cursor="",
    extra_rows=(),
):
    capability = read_capability(
        account_id="paper-position",
        environment="PAPER",
        provider_environment="TESTNET",
        instrument_version=f"{query_symbol}@v1",
        permission_scopes=("POSITION.READ",),
        at=NOW,
    )
    query = prepare_authenticated_read_query(
        capability=capability,
        surface=Surface.AUTHENTICATED_READ,
        endpoint="/v5/position/list",
        query={"category": category, "symbol": query_symbol},
        at=NOW,
        permission_scope="POSITION.READ",
    )
    raw = json.dumps(
        {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "category": category,
                "list": [row, *extra_rows],
                "nextPageCursor": cursor,
            },
            "retExtInfo": {},
            "time": 1791277200000,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return observe_authenticated_json_response(
        query_binding=query,
        http_status=200,
        response_bytes=raw,
        observed_at=NOW,
    )


def _row(**overrides):
    value = {
        "positionIdx": 0,
        "riskId": 1,
        "riskLimitValue": "2000000",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "size": "0.25",
        "markPrice": "65000.125",
        "liqPrice": "51000.5",
        "positionIM": "1000.25",
        "positionMM": "500.125",
        "leverage": "10",
        "positionStatus": "Normal",
        "updatedTime": "1791277199123",
        "seq": 42,
        "isReduceOnly": False,
    }
    value.update(overrides)
    return value


def _qualification_with_route_semantics(semantics):
    raw = canonical_json(dict(sorted(semantics.items())))
    digest = "sha256:" + sha256(raw.encode("utf-8")).hexdigest()
    qualification_id = "provider-qualification:sha256:" + "d" * 64
    return SimpleNamespace(
        route_semantics_json=raw,
        identity=SimpleNamespace(
            route_semantics_digest=digest,
            content_digest=qualification_id,
        ),
        qualification_id=qualification_id,
    )


class BybitPositionMarginParserTests(unittest.TestCase):
    def test_exact_single_symbol_position_page_preserves_provider_facts(self):
        observation = _response(_row())
        page = parse_bybit_position_margin_page(observation)
        self.assertEqual(page.category, "linear")
        self.assertEqual(page.provider_id, "BYBIT")
        self.assertEqual(page.account_id, "paper-position")
        self.assertEqual(page.instrument_version, "BTCUSDT@v1")
        self.assertEqual(page.permission_scope, "POSITION.READ")
        self.assertEqual(page.parser_identity, BYBIT_POSITION_MARGIN_PARSER_IDENTITY)
        self.assertEqual(page.parser_contract_digest, BYBIT_POSITION_MARGIN_PARSER_CONTRACT_DIGEST)
        self.assertEqual(len(page.positions), 1)
        fact = page.positions[0]
        self.assertEqual(fact.position_idx, 0)
        self.assertEqual(fact.risk_id, 1)
        self.assertEqual(fact.risk_limit_value, Decimal("2000000"))
        self.assertEqual(fact.size, Decimal("0.25"))
        self.assertEqual(fact.mark_price, Decimal("65000.125"))
        self.assertEqual(fact.liquidation_price, Decimal("51000.5"))
        self.assertEqual(fact.position_initial_margin, Decimal("1000.25"))
        self.assertEqual(fact.position_maintenance_margin, Decimal("500.125"))
        self.assertEqual(fact.updated_time_ms, 1791277199123)
        self.assertEqual(fact.sequence, 42)
        self.assertFalse(fact.is_reduce_only)
        self.assertEqual(page.evidence_ref, observation.evidence_ref)

    def test_portfolio_margin_invalid_risk_rules_remain_non_authoritative_facts(self):
        page = parse_bybit_position_margin_page(
            _response(
                _row(
                    riskId=0,
                    riskLimitValue="0",
                    liqPrice="",
                    positionIM="",
                    positionMM="",
                    leverage="",
                )
            )
        )
        fact = page.positions[0]
        self.assertEqual(fact.risk_id, 0)
        self.assertEqual(fact.risk_limit_value, Decimal("0"))
        self.assertIsNone(fact.liquidation_price)
        self.assertIsNone(fact.position_initial_margin)
        self.assertIsNone(fact.position_maintenance_margin)
        self.assertIsNone(fact.leverage)

    def test_nonzero_risk_id_requires_positive_risk_limit_value(self):
        with self.assertRaisesRegex(
            BybitPositionMarginError,
            "positive riskId requires positive riskLimitValue",
        ):
            parse_bybit_position_margin_page(_response(_row(riskLimitValue="0")))

    def test_risk_limit_value_is_required_decimal_text_for_all_tiers(self):
        for risk_id in (0, 1):
            with self.subTest(risk_id=risk_id):
                with self.assertRaisesRegex(
                    BybitPositionMarginError,
                    "riskLimitValue must not be empty",
                ):
                    parse_bybit_position_margin_page(
                        _response(_row(riskId=risk_id, riskLimitValue=""))
                    )

    def test_portfolio_margin_requires_documented_zero_risk_limit_value(self):
        with self.assertRaisesRegex(
            BybitPositionMarginError,
            "riskId=0 requires zero riskLimitValue",
        ):
            parse_bybit_position_margin_page(
                _response(_row(riskId=0, riskLimitValue="1"))
            )

    def test_hedge_position_idx_requires_documented_side(self):
        for position_idx, side in ((1, "Sell"), (2, "Buy")):
            with self.subTest(position_idx=position_idx, side=side):
                with self.assertRaisesRegex(
                    BybitPositionMarginError,
                    f"hedge positionIdx={position_idx} requires",
                ):
                    parse_bybit_position_margin_page(
                        _response(_row(positionIdx=position_idx, side=side))
                    )

    def test_position_page_cannot_mix_one_way_and_hedge_modes(self):
        with self.assertRaisesRegex(
            BybitPositionMarginError,
            "cannot mix one-way and hedge-mode",
        ):
            parse_bybit_position_margin_page(
                _response(
                    _row(positionIdx=0, side="Buy"),
                    extra_rows=(_row(positionIdx=1, side="Buy"),),
                )
            )

    def test_valid_hedge_legs_preserve_documented_position_identity(self):
        page = parse_bybit_position_margin_page(
            _response(
                _row(positionIdx=1, side="Buy"),
                extra_rows=(_row(positionIdx=2, side="Sell"),),
            )
        )
        self.assertEqual(
            tuple((fact.position_idx, fact.side) for fact in page.positions),
            ((1, "Buy"), (2, "Sell")),
        )

    def test_position_row_must_match_exact_query_symbol(self):
        with self.assertRaisesRegex(
            BybitPositionMarginError,
            "row symbol must match exact query symbol",
        ):
            parse_bybit_position_margin_page(_response(_row(symbol="ETHUSDT")))

    def test_single_symbol_parser_rejects_incomplete_pagination(self):
        with self.assertRaisesRegex(BybitPositionMarginError, "complete page"):
            parse_bybit_position_margin_page(_response(_row(), cursor="opaque-next"))

    def test_parser_rejects_observation_subclass_before_virtual_callback(self):
        canonical = _response(_row())
        callbacks = []

        class HostileObservation(type(canonical)):
            def __getattribute__(self, name):
                callbacks.append(name)
                raise AssertionError("hostile provider-response callback executed")

        forged = object.__new__(HostileObservation)
        with self.assertRaisesRegex(TypeError, "exact ProviderResponseObservation"):
            parse_bybit_position_margin_page(forged)
        self.assertEqual(callbacks, [])

    def test_position_endpoint_requires_source_owned_parser_q_claim(self):
        rule_key, rule_digest = qualified_read_route_semantic_claim(
            provider_id="BYBIT",
            endpoint="/v5/position/list",
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="POSITION.READ",
        )
        parser_key, parser_digest = qualified_read_parser_semantic_claim(
            provider_id="BYBIT",
            endpoint="/v5/position/list",
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="POSITION.READ",
        )
        self.assertEqual(parser_digest, BYBIT_POSITION_MARGIN_PARSER_CONTRACT_DIGEST)
        self.assertNotEqual(parser_key, rule_key)

        missing = _qualification_with_route_semantics(
            {"PARSER_IDENTITY": "BYBIT_ORDER_V5_JSON_V1", rule_key: rule_digest}
        )
        with self.assertRaisesRegex(
            ProviderRouteReadError,
            "does not cover exact authenticated-read parser contract",
        ):
            provider_route_reads_module._qualified_read_rule(
                qualification=missing,
                provider_id="BYBIT",
                endpoint="/v5/position/list",
                surface=Surface.AUTHENTICATED_READ,
                permission_scope="POSITION.READ",
            )

        exact = _qualification_with_route_semantics(
            {
                "PARSER_IDENTITY": "BYBIT_ORDER_V5_JSON_V1",
                parser_key: parser_digest,
                rule_key: rule_digest,
            }
        )
        (
            _semantics_digest,
            returned_rule_digest,
            _qualified_rule_digest,
            entitlement,
            statuses,
            returned_parser_identity,
        ) = provider_route_reads_module._qualified_read_rule(
            qualification=exact,
            provider_id="BYBIT",
            endpoint="/v5/position/list",
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="POSITION.READ",
        )
        self.assertEqual(returned_rule_digest, rule_digest)
        self.assertEqual(entitlement, "POSITIONS")
        self.assertEqual(statuses, (200,))
        self.assertEqual(returned_parser_identity, BYBIT_POSITION_MARGIN_PARSER_IDENTITY)


if __name__ == "__main__":
    unittest.main()
