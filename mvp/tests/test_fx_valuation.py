from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import unittest

from mvp.autotrade_mvp.fx_valuation import (
    FxQuote,
    FxRoundingPolicy,
    FxValuationError,
    value_amount,
    value_cash_balances,
)


NOW = datetime(2026, 9, 24, 18, 0, tzinfo=timezone.utc)
DIGEST = "sha256:" + "a" * 64


def eurusd(*, available_at=NOW, bid="1.1000", ask="1.1002"):
    return FxQuote.create(
        base_currency="EUR",
        quote_currency="USD",
        bid=bid,
        ask=ask,
        available_at=available_at,
        source_id="provider:fx",
        evidence_sha256=DIGEST,
    )


class FxValuationTests(unittest.TestCase):
    def test_positive_asset_uses_bid_and_liability_uses_ask(self):
        asset = value_amount(
            "100",
            source_currency="EUR",
            reporting_currency="USD",
            quote=eurusd(),
            as_of=NOW,
            max_age=timedelta(minutes=1),
        )
        liability = value_amount(
            "-100",
            source_currency="EUR",
            reporting_currency="USD",
            quote=eurusd(),
            as_of=NOW,
            max_age=timedelta(minutes=1),
        )
        self.assertEqual(asset.converted_amount, Decimal("110.0000"))
        self.assertEqual(asset.side, "BID")
        self.assertEqual(liability.converted_amount, Decimal("-110.0200"))
        self.assertEqual(liability.side, "ASK_FOR_LIABILITY")

    def test_inverse_quote_uses_conservative_side_and_exact_rational_identity(self):
        asset = value_amount(
            "110.02",
            source_currency="USD",
            reporting_currency="EUR",
            quote=eurusd(),
            as_of=NOW,
            max_age=timedelta(minutes=1),
        )
        liability = value_amount(
            "-110",
            source_currency="USD",
            reporting_currency="EUR",
            quote=eurusd(),
            as_of=NOW,
            max_age=timedelta(minutes=1),
        )
        self.assertEqual(asset.converted_amount, Decimal("100"))
        self.assertEqual(asset.side, "INVERSE_ASK")
        self.assertEqual((asset.rate_numerator, asset.rate_denominator), (5000, 5501))
        self.assertIsNone(asset.rate_used)
        self.assertEqual(liability.converted_amount, Decimal("-100"))
        self.assertEqual(liability.side, "INVERSE_BID_FOR_LIABILITY")
        self.assertEqual((liability.rate_numerator, liability.rate_denominator), (10, 11))
        self.assertIsNone(liability.rate_used)

    def test_terminating_inverse_rate_keeps_exact_decimal_compatibility_identity(self):
        quote = eurusd(bid="2", ask="2")
        result = value_amount(
            "10",
            source_currency="USD",
            reporting_currency="EUR",
            quote=quote,
            as_of=NOW,
            max_age=timedelta(minutes=1),
        )
        self.assertEqual(result.converted_amount, Decimal("5"))
        self.assertEqual(result.rate_used, Decimal("0.5"))
        self.assertEqual((result.rate_numerator, result.rate_denominator), (1, 2))

    def test_inverse_rate_projection_cannot_veto_bounded_final_conversion(self):
        quote_value = str(2**257)
        result = value_amount(
            quote_value,
            source_currency="USD",
            reporting_currency="EUR",
            quote=eurusd(bid=quote_value, ask=quote_value),
            as_of=NOW,
            max_age=timedelta(minutes=1),
        )
        self.assertEqual(result.converted_amount, Decimal("1"))
        self.assertIsNone(result.rate_used)
        self.assertEqual(result.rate_numerator, 1)
        self.assertEqual(result.rate_denominator, 2**257)

    def test_nonterminating_inverse_requires_explicit_reporting_quantum(self):
        no_policy = value_amount(
            "1",
            source_currency="USD",
            reporting_currency="EUR",
            quote=eurusd(bid="1.1", ask="1.1"),
            as_of=NOW,
            max_age=timedelta(minutes=1),
        )
        self.assertEqual(no_policy.status, "ROUNDING_POLICY_REQUIRED")
        self.assertFalse(no_policy.allocatable)
        self.assertIsNone(no_policy.converted_amount)
        self.assertEqual((no_policy.rate_numerator, no_policy.rate_denominator), (10, 11))

        policy = FxRoundingPolicy(reporting_currency="EUR", quantum=Decimal("0.01"))
        asset = value_amount(
            "1",
            source_currency="USD",
            reporting_currency="EUR",
            quote=eurusd(bid="1.1", ask="1.1"),
            as_of=NOW,
            max_age=timedelta(minutes=1),
            rounding_policy=policy,
        )
        liability = value_amount(
            "-1",
            source_currency="USD",
            reporting_currency="EUR",
            quote=eurusd(bid="1.1", ask="1.1"),
            as_of=NOW,
            max_age=timedelta(minutes=1),
            rounding_policy=policy,
        )
        self.assertEqual(asset.converted_amount, Decimal("0.90"))
        self.assertEqual(liability.converted_amount, Decimal("-0.91"))
        self.assertEqual(asset.rounding_policy_id, policy.policy_id)
        self.assertEqual(asset.rounding_quantum, Decimal("0.01"))
        self.assertLessEqual(asset.converted_amount, Decimal(10) / Decimal(11))
        self.assertLessEqual(liability.converted_amount, Decimal(-10) / Decimal(11))

    def test_fx_results_are_independent_of_hostile_decimal_context(self):
        policy = FxRoundingPolicy(reporting_currency="EUR", quantum=Decimal("0.000001"))
        observed = []
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    direct = value_amount(
                        "12345678901234567890.123456789",
                        source_currency="EUR",
                        reporting_currency="USD",
                        quote=eurusd(bid="1.000000001", ask="1.000000002"),
                        as_of=NOW,
                        max_age=timedelta(minutes=1),
                        haircut="0.000000001",
                    )
                    inverse = value_amount(
                        "1",
                        source_currency="USD",
                        reporting_currency="EUR",
                        quote=eurusd(bid="1.1", ask="1.1"),
                        as_of=NOW,
                        max_age=timedelta(minutes=1),
                        rounding_policy=policy,
                    )
                    observed.append(
                        (
                            direct.converted_amount,
                            inverse.converted_amount,
                            inverse.rounding_policy_id,
                        )
                    )
        self.assertTrue(all(item == observed[0] for item in observed))
        self.assertEqual(observed[0][1], Decimal("0.909090"))

    def test_haircut_reduces_assets_and_increases_liabilities(self):
        asset = value_amount(
            "100",
            source_currency="EUR",
            reporting_currency="USD",
            quote=eurusd(bid="1", ask="1"),
            as_of=NOW,
            max_age=timedelta(minutes=1),
            haircut="0.10",
        )
        liability = value_amount(
            "-100",
            source_currency="EUR",
            reporting_currency="USD",
            quote=eurusd(bid="1", ask="1"),
            as_of=NOW,
            max_age=timedelta(minutes=1),
            haircut="0.10",
        )
        self.assertEqual(asset.converted_amount, Decimal("90"))
        self.assertEqual(liability.converted_amount, Decimal("-110"))

    def test_stale_future_and_missing_quotes_are_non_allocatable(self):
        stale = value_amount(
            "100",
            source_currency="EUR",
            reporting_currency="USD",
            quote=eurusd(available_at=NOW - timedelta(minutes=2)),
            as_of=NOW,
            max_age=timedelta(minutes=1),
        )
        future = value_amount(
            "100",
            source_currency="EUR",
            reporting_currency="USD",
            quote=eurusd(available_at=NOW + timedelta(seconds=1)),
            as_of=NOW,
            max_age=timedelta(minutes=1),
        )
        missing = value_amount(
            "100",
            source_currency="EUR",
            reporting_currency="USD",
            quote=None,
            as_of=NOW,
            max_age=timedelta(minutes=1),
        )
        self.assertEqual(stale.status, "STALE")
        self.assertEqual(future.status, "FUTURE_EVIDENCE")
        self.assertEqual(missing.status, "MISSING")
        self.assertFalse(stale.allocatable)
        self.assertFalse(future.allocatable)
        self.assertFalse(missing.allocatable)
        self.assertIsNone(stale.converted_amount)

    def test_portfolio_total_is_withheld_if_any_nonzero_currency_is_uncertain(self):
        result = value_cash_balances(
            {"USD": "1000", "EUR": "100"},
            reporting_currency="USD",
            quotes={},
            as_of=NOW,
            max_age=timedelta(minutes=1),
        )
        self.assertEqual(result.status, "UNCERTAIN")
        self.assertIsNone(result.total)
        self.assertFalse(result.allocatable)
        self.assertIn("EUR: MISSING", result.reasons[0])

    def test_portfolio_sums_only_after_each_currency_is_converted(self):
        result = value_cash_balances(
            {"USD": "1000", "EUR": "100", "CHF": "0"},
            reporting_currency="USD",
            quotes={"EUR": eurusd(bid="1.1", ask="1.2")},
            as_of=NOW,
            max_age=timedelta(minutes=1),
        )
        self.assertEqual(result.status, "CERTAIN")
        self.assertEqual(result.total, Decimal("1110"))
        self.assertTrue(result.allocatable)

    def test_portfolio_sum_is_exact_under_low_precision_context(self):
        with localcontext() as context:
            context.prec = 6
            result = value_cash_balances(
                {
                    "USD": "12345678901234567890.1",
                    "EUR": "0.9",
                },
                reporting_currency="USD",
                quotes={"EUR": eurusd(bid="1", ask="1")},
                as_of=NOW,
                max_age=timedelta(minutes=1),
            )
        self.assertEqual(result.total, Decimal("12345678901234567891"))

    def test_resource_envelope_failure_is_reported_as_fx_error(self):
        boundary = "9" * 256
        with self.assertRaisesRegex(FxValuationError, "resource envelope"):
            value_amount(
                boundary,
                source_currency="EUR",
                reporting_currency="USD",
                quote=eurusd(bid="10", ask="10"),
                as_of=NOW,
                max_age=timedelta(minutes=1),
            )

    def test_rounding_policy_cannot_reclassify_terminating_resource_overflow(self):
        boundary = "9" * 256
        with self.assertRaisesRegex(FxValuationError, "resource envelope"):
            value_amount(
                boundary,
                source_currency="EUR",
                reporting_currency="USD",
                quote=eurusd(bid="10", ask="10"),
                as_of=NOW,
                max_age=timedelta(minutes=1),
                rounding_policy=FxRoundingPolicy(
                    reporting_currency="USD",
                    quantum="0.01",
                ),
            )

    def test_extreme_zero_exponent_is_canonicalized_before_fx_identity(self):
        result = value_amount(
            Decimal("0E-1000000"),
            source_currency="EUR",
            reporting_currency="USD",
            quote=None,
            as_of=NOW,
            max_age=timedelta(0),
        )
        self.assertEqual(result.converted_amount, Decimal("0"))
        self.assertEqual(result.source_amount, Decimal("0"))
        self.assertEqual(result.status, "CERTAIN")

    def test_duplicate_normalized_currency_codes_cannot_double_count_capital(self):
        with self.assertRaisesRegex(
            FxValuationError,
            "duplicate normalized currency codes",
        ):
            value_cash_balances(
                {"USD": "1000", "usd": "1000"},
                reporting_currency="USD",
                quotes={},
                as_of=NOW,
                max_age=timedelta(minutes=1),
            )

    def test_same_currency_and_zero_foreign_balance_need_no_rate(self):
        local = value_amount(
            "25",
            source_currency="USD",
            reporting_currency="USD",
            quote=None,
            as_of=NOW,
            max_age=timedelta(0),
        )
        zero = value_amount(
            "0",
            source_currency="EUR",
            reporting_currency="USD",
            quote=None,
            as_of=NOW,
            max_age=timedelta(0),
        )
        self.assertEqual(local.converted_amount, Decimal("25"))
        self.assertEqual(zero.converted_amount, Decimal("0"))
        self.assertTrue(local.allocatable)
        self.assertTrue(zero.allocatable)

    def test_rounding_policy_currency_must_match_reporting_currency(self):
        with self.assertRaisesRegex(FxValuationError, "reporting currency mismatch"):
            value_amount(
                "1",
                source_currency="USD",
                reporting_currency="EUR",
                quote=eurusd(bid="1.1", ask="1.1"),
                as_of=NOW,
                max_age=timedelta(minutes=1),
                rounding_policy=FxRoundingPolicy(
                    reporting_currency="USD",
                    quantum="0.01",
                ),
            )

    def test_float_bad_spread_and_wrong_pair_fail_closed(self):
        with self.assertRaisesRegex(FxValuationError, "exact decimal"):
            eurusd(bid=1.1)
        with self.assertRaisesRegex(FxValuationError, "cannot exceed"):
            eurusd(bid="1.2", ask="1.1")
        with self.assertRaisesRegex(FxValuationError, "does not connect"):
            value_amount(
                "1",
                source_currency="CHF",
                reporting_currency="USD",
                quote=eurusd(),
                as_of=NOW,
                max_age=timedelta(minutes=1),
            )


if __name__ == "__main__":
    unittest.main()
