from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from fractions import Fraction
import unittest

from mvp.autotrade_mvp.economics import (
    apply_split,
    cash_round_trip,
    corrected_fill_cash_difference,
    inverse_futures_pnl,
    inverse_futures_pnl_exact,
    investment_pnl_excluding_external_flows,
    linear_funding_cashflow,
    linear_futures_mark_pnl,
    project_split_decimal,
)
from mvp.autotrade_mvp.exact_decimal import ExactDecimalError


class EconomicOracleExactnessTests(unittest.TestCase):
    def test_cash_round_trip_rejects_binary_float_money(self):
        with self.assertRaises(TypeError):
            cash_round_trip(
                start_cash=1000.0,
                buy_quantity="1",
                buy_price="100",
                buy_fee="1",
                sell_quantity="1",
                sell_price="101",
                sell_fee="1",
                mark_price="101",
            )

    def test_derivative_oracles_reject_binary_float_inputs(self):
        with self.assertRaises(TypeError):
            linear_futures_mark_pnl("1", "1", 100.0, "101")
        with self.assertRaises(TypeError):
            inverse_futures_pnl("1", "100", "100", 101.0)
        with self.assertRaises(TypeError):
            linear_funding_cashflow("1000", 0.001)

    def test_corporate_and_flow_oracles_reject_binary_float_inputs(self):
        with self.assertRaises(TypeError):
            apply_split("10", "100", numerator=2.0)
        with self.assertRaises(TypeError):
            investment_pnl_excluding_external_flows("1000", "1010", 1.0)
        with self.assertRaises(TypeError):
            corrected_fill_cash_difference("1", "100", 100.5)

    def test_split_oracle_preserves_basis_for_long_and_short_quantities(self):
        long_result = apply_split("10", "100", numerator="2", denominator="1")
        short_result = apply_split("-10", "100", numerator="2", denominator="1")

        self.assertEqual(long_result.quantity, Fraction(20, 1))
        self.assertEqual(short_result.quantity, Fraction(-20, 1))
        self.assertEqual(long_result.total_basis, Fraction(1000, 1))
        self.assertEqual(short_result.total_basis, Fraction(1000, 1))
        self.assertEqual(long_result.unit_basis, Fraction(50, 1))
        self.assertEqual(short_result.unit_basis, Fraction(50, 1))

        long_decimal = project_split_decimal(long_result)
        short_decimal = project_split_decimal(short_result)
        self.assertEqual(long_decimal.quantity, Decimal("20"))
        self.assertEqual(short_decimal.quantity, Decimal("-20"))
        self.assertEqual(long_decimal.unit_basis, Decimal("50"))
        self.assertEqual(short_decimal.unit_basis, Decimal("50"))

    def test_nonterminating_split_remains_exact_for_long_and_short(self):
        observed = []
        for precision in (3, 6, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    long_result = apply_split(
                        "1",
                        "300",
                        numerator="1",
                        denominator="3",
                    )
                    short_result = apply_split(
                        "-1",
                        "300",
                        numerator="1",
                        denominator="3",
                    )
                    observed.append((long_result, short_result))

        self.assertTrue(all(item == observed[0] for item in observed))
        long_result, short_result = observed[0]
        self.assertEqual(long_result.quantity, Fraction(1, 3))
        self.assertEqual(short_result.quantity, Fraction(-1, 3))
        self.assertEqual(long_result.unit_basis, Fraction(900, 1))
        self.assertEqual(short_result.unit_basis, Fraction(900, 1))
        self.assertEqual(long_result.total_basis, Fraction(300, 1))
        self.assertEqual(short_result.total_basis, Fraction(300, 1))
        self.assertEqual(
            abs(long_result.quantity) * long_result.unit_basis,
            long_result.total_basis,
        )
        self.assertEqual(
            abs(short_result.quantity) * short_result.unit_basis,
            short_result.total_basis,
        )
        with self.assertRaisesRegex(ExactDecimalError, "non-terminating"):
            project_split_decimal(long_result)
        with self.assertRaisesRegex(ExactDecimalError, "non-terminating"):
            project_split_decimal(short_result)

    def test_exact_decimal_inputs_preserve_expected_reference_result(self):
        result = cash_round_trip(
            start_cash=Decimal("1000"),
            buy_quantity="2",
            buy_price="100",
            buy_fee="0.20",
            sell_quantity="2",
            sell_price="101",
            sell_fee="0.202",
            mark_price="101",
        )
        self.assertEqual(result.position, Decimal("0"))
        self.assertEqual(result.net_pnl, Decimal("1.598"))
        self.assertEqual(result.equity, Decimal("1001.598"))

    def test_reference_arithmetic_is_invariant_under_hostile_context(self):
        observed = []
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    cash = cash_round_trip(
                        start_cash="12345678901234567890.123456789",
                        buy_quantity="2.000000001",
                        buy_price="1000000000.000000001",
                        buy_fee="0.000000001",
                        sell_quantity="1.000000001",
                        sell_price="1000000001.000000001",
                        sell_fee="0.000000002",
                        mark_price="1000000002.000000001",
                    )
                    linear = linear_futures_mark_pnl(
                        "123456789.000000001",
                        "0.000000001",
                        "1000000000.000000001",
                        "1000000001.000000001",
                    )
                    inverse = inverse_futures_pnl(
                        "100",
                        "1",
                        "10000",
                        "11000",
                    )
                    observed.append((cash, linear, inverse))
        self.assertTrue(all(item == observed[0] for item in observed))

    def test_inverse_reference_exposes_exact_rational_authority(self):
        self.assertEqual(
            inverse_futures_pnl_exact("100", "1", "10000", "11000"),
            Fraction(1, 1100),
        )
        observed = []
        for precision in (3, 6, 28, 80):
            with localcontext() as context:
                context.prec = precision
                observed.append(inverse_futures_pnl("100", "1", "10000", "11000"))
        self.assertTrue(all(value == observed[0] for value in observed))
        self.assertEqual(
            observed[0].quantize(Decimal("0.00000000001")),
            Decimal("0.00090909091"),
        )


if __name__ == "__main__":
    unittest.main()
