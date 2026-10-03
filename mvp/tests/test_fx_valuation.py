from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.fx_valuation as fx_valuation
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

    def test_inverse_rate_projection_overflow_does_not_veto_exact_final_amount(self):
        quote = eurusd(bid="1e-256", ask="1e-256")
        observed = []
        for precision in (6, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    result = value_amount(
                        "1e-256",
                        source_currency="USD",
                        reporting_currency="EUR",
                        quote=quote,
                        as_of=NOW,
                        max_age=timedelta(minutes=1),
                    )
                    observed.append(result)

        self.assertTrue(all(row.converted_amount == Decimal("1") for row in observed))
        self.assertTrue(all(row.rate_used is None for row in observed))
        self.assertTrue(all(row.rate_numerator == 10**256 for row in observed))
        self.assertTrue(all(row.rate_denominator == 1 for row in observed))
        self.assertTrue(all(row.rounding_policy_id is None for row in observed))
        self.assertTrue(all(row.rounding_quantum is None for row in observed))

    def test_inverse_optional_rate_fallback_cannot_mask_final_amount_overflow(self):
        quote = eurusd(bid="1e-256", ask="1e-256")
        with self.assertRaisesRegex(FxValuationError, "resource envelope"):
            value_amount(
                "1",
                source_currency="USD",
                reporting_currency="EUR",
                quote=quote,
                as_of=NOW,
                max_age=timedelta(minutes=1),
                rounding_policy=FxRoundingPolicy(
                    reporting_currency="EUR",
                    quantum="0.01",
                ),
            )

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


class FxValuationAuthorityBoundaryTests(unittest.TestCase):
    def test_decimal_subclass_is_rejected_before_virtual_dispatch(self):
        touched = []

        class HostileDecimal(Decimal):
            def is_finite(self):
                touched.append("is_finite")
                raise AssertionError("hostile Decimal dispatch")

            def as_tuple(self):
                touched.append("as_tuple")
                raise AssertionError("hostile Decimal dispatch")

        with self.assertRaisesRegex(FxValuationError, "exact decimal"):
            value_amount(
                HostileDecimal("1"),
                source_currency="EUR",
                reporting_currency="USD",
                quote=eurusd(),
                as_of=NOW,
                max_age=timedelta(minutes=1),
            )

        self.assertEqual(touched, [])

    def test_text_subclass_is_rejected_before_strip_or_upper_dispatch(self):
        touched = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                touched.append("strip")
                raise AssertionError("hostile text strip")

            def upper(self, *args, **kwargs):
                touched.append("upper")
                raise AssertionError("hostile text upper")

        with self.assertRaisesRegex(FxValuationError, "base_currency"):
            FxQuote.create(
                base_currency=HostileText("EUR"),
                quote_currency="USD",
                bid="1",
                ask="1",
                available_at=NOW,
                source_id="provider:fx",
                evidence_sha256=DIGEST,
            )

        self.assertEqual(touched, [])

    def test_datetime_subclass_is_rejected_before_time_callbacks(self):
        touched = []

        class HostileDateTime(datetime):
            def utcoffset(self):
                touched.append("utcoffset")
                raise AssertionError("hostile datetime utcoffset")

            def astimezone(self, *args, **kwargs):
                touched.append("astimezone")
                raise AssertionError("hostile datetime astimezone")

        hostile = HostileDateTime(2026, 9, 24, 18, 0, tzinfo=timezone.utc)
        with self.assertRaisesRegex(FxValuationError, "exact datetime"):
            FxQuote.create(
                base_currency="EUR",
                quote_currency="USD",
                bid="1",
                ask="1",
                available_at=hostile,
                source_id="provider:fx",
                evidence_sha256=DIGEST,
            )

        self.assertEqual(touched, [])

    def test_custom_tzinfo_is_rejected_before_tz_callbacks(self):
        touched = []

        class HostileTzInfo(tzinfo):
            def utcoffset(self, dt):
                touched.append("utcoffset")
                raise AssertionError("hostile tzinfo utcoffset")

            def dst(self, dt):
                touched.append("dst")
                raise AssertionError("hostile tzinfo dst")

            def tzname(self, dt):
                touched.append("tzname")
                raise AssertionError("hostile tzinfo tzname")

        hostile = datetime(2026, 9, 24, 18, 0, tzinfo=HostileTzInfo())
        with self.assertRaisesRegex(FxValuationError, "built-in timezone"):
            FxQuote.create(
                base_currency="EUR",
                quote_currency="USD",
                bid="1",
                ask="1",
                available_at=hostile,
                source_id="provider:fx",
                evidence_sha256=DIGEST,
            )

        self.assertEqual(touched, [])

    def test_quote_and_rounding_policy_subclasses_are_rejected_before_reads(self):
        touched = []

        class HostileQuote(FxQuote):
            def __getattribute__(self, name):
                if name != "__class__":
                    touched.append(f"quote:{name}")
                    raise AssertionError("hostile quote attribute dispatch")
                return super().__getattribute__(name)

        class HostilePolicy(FxRoundingPolicy):
            def __getattribute__(self, name):
                if name != "__class__":
                    touched.append(f"policy:{name}")
                    raise AssertionError("hostile policy attribute dispatch")
                return super().__getattribute__(name)

        hostile_quote = object.__new__(HostileQuote)
        hostile_policy = object.__new__(HostilePolicy)

        with self.assertRaisesRegex(FxValuationError, "exact FxQuote"):
            value_amount(
                "1",
                source_currency="EUR",
                reporting_currency="USD",
                quote=hostile_quote,
                as_of=NOW,
                max_age=timedelta(minutes=1),
            )
        with self.assertRaisesRegex(FxValuationError, "exact FxRoundingPolicy"):
            value_amount(
                "1",
                source_currency="USD",
                reporting_currency="EUR",
                quote=eurusd(bid="1.1", ask="1.1"),
                as_of=NOW,
                max_age=timedelta(minutes=1),
                rounding_policy=hostile_policy,
            )

        self.assertEqual(touched, [])

    def test_empty_portfolio_cannot_skip_shared_valuation_authority(self):
        valid = value_cash_balances(
            {},
            reporting_currency="USD",
            quotes={},
            as_of=NOW,
            max_age=timedelta(minutes=1),
        )
        self.assertEqual(valid.status, "CERTAIN")
        self.assertEqual(valid.total, Decimal("0"))
        self.assertTrue(valid.allocatable)

        with self.assertRaisesRegex(FxValuationError, "exact datetime"):
            value_cash_balances(
                {},
                reporting_currency="USD",
                quotes={},
                as_of=object(),
                max_age=timedelta(minutes=1),
            )
        with self.assertRaisesRegex(FxValuationError, "non-negative exact timedelta"):
            value_cash_balances(
                {},
                reporting_currency="USD",
                quotes={},
                as_of=NOW,
                max_age=-timedelta(microseconds=1),
            )
        with self.assertRaisesRegex(FxValuationError, "haircut must be in"):
            value_cash_balances(
                {},
                reporting_currency="USD",
                quotes={},
                as_of=NOW,
                max_age=timedelta(minutes=1),
                haircut="1",
            )
        with self.assertRaisesRegex(FxValuationError, "reporting currency mismatch"):
            value_cash_balances(
                {},
                reporting_currency="USD",
                quotes={},
                as_of=NOW,
                max_age=timedelta(minutes=1),
                rounding_policy=FxRoundingPolicy(
                    reporting_currency="EUR",
                    quantum="0.01",
                ),
            )

    def test_empty_portfolio_rejects_polymorphic_context_without_callbacks(self):
        touched = []

        class HostileDateTime(datetime):
            def utcoffset(self):
                touched.append("utcoffset")
                raise AssertionError("hostile datetime callback")

        class HostilePolicy(FxRoundingPolicy):
            def __getattribute__(self, name):
                if name != "__class__":
                    touched.append(name)
                    raise AssertionError("hostile policy callback")
                return super().__getattribute__(name)

        with self.assertRaisesRegex(FxValuationError, "exact datetime"):
            value_cash_balances(
                {},
                reporting_currency="USD",
                quotes={},
                as_of=HostileDateTime(
                    2026, 9, 24, 18, 0, tzinfo=timezone.utc
                ),
                max_age=timedelta(minutes=1),
            )
        with self.assertRaisesRegex(FxValuationError, "exact FxRoundingPolicy"):
            value_cash_balances(
                {},
                reporting_currency="USD",
                quotes={},
                as_of=NOW,
                max_age=timedelta(minutes=1),
                rounding_policy=object.__new__(HostilePolicy),
            )

        self.assertEqual(touched, [])

    def test_portfolio_valuation_holds_balance_and_quote_bindings_once(self):
        balances = {"EUR": "100"}
        quotes = {"EUR": eurusd(bid="1.1000", ask="1.1002")}
        original_currency = fx_valuation._currency
        mutated = False

        def mutate_callers_after_snapshot(value, name):
            nonlocal mutated
            if name == "reporting_currency" and not mutated:
                mutated = True
                balances["EUR"] = "999"
                quotes["EUR"] = eurusd(bid="9", ask="9")
            return original_currency(value, name)

        with patch(
            "mvp.autotrade_mvp.fx_valuation._currency",
            side_effect=mutate_callers_after_snapshot,
        ):
            result = value_cash_balances(
                balances,
                reporting_currency="USD",
                quotes=quotes,
                as_of=NOW,
                max_age=timedelta(minutes=1),
            )

        self.assertTrue(mutated)
        self.assertEqual(balances["EUR"], "999")
        self.assertEqual(quotes["EUR"].bid, Decimal("9"))
        self.assertEqual(result.status, "CERTAIN")
        self.assertEqual(result.components[0].source_amount, Decimal("100"))
        self.assertEqual(result.components[0].rate_used, Decimal("1.1000"))
        self.assertEqual(result.total, Decimal("110.0000"))

    def test_mapping_subclasses_are_rejected_before_mapping_callbacks(self):
        touched = []

        class HostileDict(dict):
            def __iter__(self):
                touched.append("iter")
                raise AssertionError("hostile dict iteration")

            def get(self, *args, **kwargs):
                touched.append("get")
                raise AssertionError("hostile dict get")

        for balances, quotes in (
            (HostileDict({"USD": "1"}), {}),
            ({"USD": "1"}, HostileDict({})),
        ):
            touched.clear()
            with self.subTest(
                balances_type=type(balances).__name__,
                quotes_type=type(quotes).__name__,
            ):
                with self.assertRaisesRegex(FxValuationError, "exact dict"):
                    value_cash_balances(
                        balances,
                        reporting_currency="USD",
                        quotes=quotes,
                        as_of=NOW,
                        max_age=timedelta(minutes=1),
                    )
                self.assertEqual(touched, [])

    def test_nonexact_currency_key_is_rejected_before_sort_or_text_dispatch(self):
        touched = []

        class HostileCurrency(str):
            def __lt__(self, other):
                touched.append("lt")
                raise AssertionError("hostile key comparison")

            def strip(self, *args, **kwargs):
                touched.append("strip")
                raise AssertionError("hostile key strip")

        hostile_key = HostileCurrency("EUR")
        balances = {hostile_key: "1", "USD": "1"}
        touched.clear()

        with self.assertRaisesRegex(FxValuationError, "currency keys"):
            value_cash_balances(
                balances,
                reporting_currency="USD",
                quotes={},
                as_of=NOW,
                max_age=timedelta(minutes=1),
            )

        self.assertEqual(touched, [])


    def test_exact_quote_instance_cannot_bypass_nested_scalar_admission(self):
        touched = []

        class HostileDecimal(Decimal):
            def is_finite(self):
                touched.append("is_finite")
                raise AssertionError("hostile quote Decimal dispatch")

            def as_tuple(self):
                touched.append("as_tuple")
                raise AssertionError("hostile quote Decimal dispatch")

        forged = FxQuote(
            base_currency="EUR",
            quote_currency="USD",
            bid=HostileDecimal("1"),
            ask=Decimal("1"),
            available_at=NOW,
            source_id="provider:fx",
            evidence_sha256=DIGEST,
        )

        with self.assertRaisesRegex(FxValuationError, "exact decimal"):
            value_amount(
                "1",
                source_currency="EUR",
                reporting_currency="USD",
                quote=forged,
                as_of=NOW,
                max_age=timedelta(minutes=1),
            )

        self.assertEqual(touched, [])

    def test_exact_quote_instance_cannot_bypass_quote_invariants(self):
        forged = FxQuote(
            base_currency="EUR",
            quote_currency="USD",
            bid=Decimal("2"),
            ask=Decimal("1"),
            available_at=NOW,
            source_id="provider:fx",
            evidence_sha256=DIGEST,
        )

        with self.assertRaisesRegex(FxValuationError, "cannot exceed"):
            value_amount(
                "1",
                source_currency="EUR",
                reporting_currency="USD",
                quote=forged,
                as_of=NOW,
                max_age=timedelta(minutes=1),
            )

    def test_forged_exact_rounding_policy_is_resealed_before_use(self):
        touched = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                touched.append("strip")
                raise AssertionError("hostile policy text dispatch")

        forged = object.__new__(FxRoundingPolicy)
        object.__setattr__(forged, "reporting_currency", HostileText("EUR"))
        object.__setattr__(forged, "quantum", Decimal("0.01"))
        object.__setattr__(forged, "version", "1")

        with self.assertRaisesRegex(FxValuationError, "reporting_currency"):
            value_amount(
                "1",
                source_currency="USD",
                reporting_currency="EUR",
                quote=eurusd(bid="1.1", ask="1.1"),
                as_of=NOW,
                max_age=timedelta(minutes=1),
                rounding_policy=forged,
            )

        self.assertEqual(touched, [])


    def test_quote_factory_cannot_issue_polymorphic_quote(self):
        touched = []

        class HostileQuote(FxQuote):
            def __init__(self, *args, **kwargs):
                touched.append("init")
                raise AssertionError("hostile quote constructor")

        with self.assertRaisesRegex(FxValuationError, "exact FxQuote class"):
            HostileQuote.create(
                base_currency="EUR",
                quote_currency="USD",
                bid="1",
                ask="1",
                available_at=NOW,
                source_id="provider:fx",
                evidence_sha256=DIGEST,
            )

        self.assertEqual(touched, [])


    def test_forged_exact_domain_objects_missing_fields_fail_closed(self):
        empty_quote = object.__new__(FxQuote)
        empty_policy = object.__new__(FxRoundingPolicy)

        with self.assertRaisesRegex(FxValuationError, "quote is missing required field"):
            value_amount(
                "1",
                source_currency="EUR",
                reporting_currency="USD",
                quote=empty_quote,
                as_of=NOW,
                max_age=timedelta(minutes=1),
            )

        with self.assertRaisesRegex(
            FxValuationError, "rounding_policy is missing required field"
        ):
            value_amount(
                "1",
                source_currency="USD",
                reporting_currency="EUR",
                quote=eurusd(bid="1.1", ask="1.1"),
                as_of=NOW,
                max_age=timedelta(minutes=1),
                rounding_policy=empty_policy,
            )


if __name__ == "__main__":
    unittest.main()
