from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.fx_valuation import (
    FxQuote,
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

    def test_inverse_quote_uses_conservative_side(self):
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
        self.assertEqual(liability.converted_amount, Decimal("-1E+2"))
        self.assertEqual(liability.side, "INVERSE_BID_FOR_LIABILITY")

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
        self.assertEqual(asset.converted_amount, Decimal("90.00"))
        self.assertEqual(liability.converted_amount, Decimal("-110.00"))

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
        self.assertEqual(result.total, Decimal("1110.0"))
        self.assertTrue(result.allocatable)

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
