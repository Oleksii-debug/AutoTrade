from datetime import datetime, timezone
from decimal import Decimal, Inexact, Rounded, ROUND_CEILING, localcontext
import unittest

from mvp.autotrade_mvp.bybit_margin_convention import (
    BYBIT_MARGIN_TIER_CONVENTION_DIGEST,
    BYBIT_MARGIN_TIER_CONVENTION_IDENTITY,
    BybitMarginConventionError,
    derive_bybit_margin_tier_convention,
)
from mvp.autotrade_mvp.bybit_margin_market import (
    BYBIT_MARGIN_RISK_LIMIT_ENDPOINT,
    parse_bybit_margin_risk_limits,
)
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)
from mvp.tests.test_bybit_v5 import read_capability


NOW = datetime(2026, 10, 6, 11, 30, tzinfo=timezone.utc)


def _row(
    risk_id,
    limit,
    maintenance,
    initial,
    lowest,
    max_leverage,
    deduction,
):
    return (
        "{"
        f'"id":{risk_id},"symbol":"BTCUSDT",'
        f'"riskLimitValue":"{limit}",'
        f'"maintenanceMargin":"{maintenance}",'
        f'"initialMargin":"{initial}",'
        f'"isLowestRisk":{lowest},'
        f'"maxLeverage":"{max_leverage}",'
        f'"mmDeduction":"{deduction}"'
        "}"
    )


def _page(
    *,
    rows=None,
    ret_msg="OK",
):
    if rows is None:
        rows = [
            _row(1, "100000", "0.5", "1", 1, "100", ""),
            _row(2, "200000", "1.0", "2", 0, "50", "500"),
            _row(3, "300000", "1.5", "3", 0, "33.33", "1500"),
        ]
    capability = read_capability(
        account_id="paper-margin-convention",
        environment="PAPER",
        provider_environment="TESTNET",
        instrument_version="BTCUSDT@v1",
        permission_scopes=("MARKET.READ",),
        at=NOW,
    )
    query = prepare_authenticated_read_query(
        capability=capability,
        surface=Surface.PUBLIC_DATA,
        endpoint=BYBIT_MARGIN_RISK_LIMIT_ENDPOINT,
        query={"category": "linear", "symbol": "BTCUSDT"},
        at=NOW,
        permission_scope="MARKET.READ",
    )
    raw = (
        "{"
        f'"retCode":0,"retMsg":"{ret_msg}","result":{{'
        '"category":"linear","list":['
        + ",".join(rows)
        + '],"nextPageCursor":""},'
        '"retExtInfo":{},"time":1791282600123}'
    ).encode("utf-8")
    observation = observe_authenticated_json_response(
        query_binding=query,
        http_status=200,
        response_bytes=raw,
        observed_at=NOW,
    )
    return parse_bybit_margin_risk_limits(observation)


class BybitMarginConventionTests(unittest.TestCase):
    def test_provider_percent_points_convert_to_fractional_deduct_tiers(self):
        convention = derive_bybit_margin_tier_convention(_page())
        self.assertEqual(
            convention.convention_identity,
            BYBIT_MARGIN_TIER_CONVENTION_IDENTITY,
        )
        self.assertEqual(
            convention.convention_digest,
            BYBIT_MARGIN_TIER_CONVENTION_DIGEST,
        )
        self.assertEqual(convention.source_risk_ids, (1, 2, 3))
        self.assertEqual(
            [tier.maintenance_rate for tier in convention.tiers],
            [Decimal("0.005"), Decimal("0.01"), Decimal("0.015")],
        )
        self.assertEqual(
            [tier.maintenance_adjustment for tier in convention.tiers],
            [Decimal("0"), Decimal("500"), Decimal("1500")],
        )
        self.assertTrue(
            all(tier.adjustment_convention == "DEDUCT" for tier in convention.tiers)
        )
        self.assertEqual(
            convention.tiers[1].maintenance_requirement(Decimal("200000")),
            Decimal("1500"),
        )

    def test_conversion_is_independent_of_hostile_decimal_context(self):
        expected = derive_bybit_margin_tier_convention(_page())
        with localcontext() as context:
            context.prec = 2
            context.rounding = ROUND_CEILING
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            actual = derive_bybit_margin_tier_convention(_page())
        self.assertEqual(actual.tiers, expected.tiers)
        self.assertEqual(actual.revision_id, expected.revision_id)

    def test_provider_deduction_must_match_documented_recurrence(self):
        rows = [
            _row(1, "100000", "0.5", "1", 1, "100", ""),
            _row(2, "200000", "1.0", "2", 0, "50", "499.99"),
        ]
        with self.assertRaisesRegex(
            BybitMarginConventionError,
            "deduction conflicts",
        ):
            derive_bybit_margin_tier_convention(_page(rows=rows))

    def test_non_lowest_tier_cannot_omit_deduction(self):
        rows = [
            _row(1, "100000", "0.5", "1", 1, "100", ""),
            _row(2, "200000", "1.0", "2", 0, "50", ""),
        ]
        with self.assertRaisesRegex(
            BybitMarginConventionError,
            "requires maintenance margin deduction",
        ):
            derive_bybit_margin_tier_convention(_page(rows=rows))

    def test_lowest_tier_deduction_must_be_zero(self):
        rows = [
            _row(1, "100000", "0.5", "1", 1, "100", "1"),
        ]
        with self.assertRaisesRegex(
            BybitMarginConventionError,
            "lowest-risk tier maintenance margin deduction",
        ):
            derive_bybit_margin_tier_convention(_page(rows=rows))

    def test_risk_ids_must_be_contiguous_from_one(self):
        rows = [
            _row(1, "100000", "0.5", "1", 1, "100", ""),
            _row(3, "300000", "1.5", "3", 0, "33.33", "1000"),
        ]
        with self.assertRaisesRegex(
            BybitMarginConventionError,
            "contiguous starting at one",
        ):
            derive_bybit_margin_tier_convention(_page(rows=rows))

    def test_position_limit_must_increase_with_tier(self):
        rows = [
            _row(1, "200000", "0.5", "1", 1, "100", ""),
            _row(2, "100000", "1.0", "2", 0, "50", "1000"),
        ]
        with self.assertRaisesRegex(
            BybitMarginConventionError,
            "position bounds must increase",
        ):
            derive_bybit_margin_tier_convention(_page(rows=rows))

    def test_maintenance_rate_cannot_decrease(self):
        rows = [
            _row(1, "100000", "1.0", "2", 1, "50", ""),
            _row(2, "200000", "0.5", "2.5", 0, "40", "-500"),
        ]
        with self.assertRaisesRegex(
            BybitMarginConventionError,
            "maintenance margin rate must not decrease",
        ):
            derive_bybit_margin_tier_convention(_page(rows=rows))

    def test_initial_rate_must_exceed_maintenance_rate(self):
        rows = [
            _row(1, "100000", "1", "1", 1, "100", ""),
        ]
        with self.assertRaisesRegex(
            BybitMarginConventionError,
            "stay below initial margin rate",
        ):
            derive_bybit_margin_tier_convention(_page(rows=rows))

    def test_maximum_leverage_cannot_increase_with_tier(self):
        rows = [
            _row(1, "100000", "0.5", "1", 1, "50", ""),
            _row(2, "200000", "1.0", "2", 0, "100", "500"),
        ]
        with self.assertRaisesRegex(
            BybitMarginConventionError,
            "maximum leverage must not increase",
        ):
            derive_bybit_margin_tier_convention(_page(rows=rows))

    def test_revision_identity_binds_exact_source_response(self):
        first = derive_bybit_margin_tier_convention(_page(ret_msg="OK"))
        second = derive_bybit_margin_tier_convention(_page(ret_msg="SUCCESS"))
        self.assertEqual(first.tiers, second.tiers)
        self.assertNotEqual(first.response_sha256, second.response_sha256)
        self.assertNotEqual(first.revision_id, second.revision_id)


if __name__ == "__main__":
    unittest.main()
