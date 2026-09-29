from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from fractions import Fraction
import unittest

from mvp.autotrade_mvp.exact_decimal import (
    ExactDecimalError,
    MAX_INTEGER_DIGITS,
    MAX_SCALE,
    MAX_SIGNIFICANT_DIGITS,
    as_fraction,
    canonical_decimal_text,
    exact_abs,
    exact_add,
    exact_multiply,
    exact_sum,
    round_fraction_to_quantum,
    terminating_decimal,
)


class ExactDecimalTests(unittest.TestCase):
    def test_finite_arithmetic_is_independent_of_ambient_context(self):
        expected_sum = Decimal("1234567890123456789012345679")
        expected_product = Decimal("1234567890123456789012345678.123456789")
        expected_abs = Decimal("12345678901234567890.123456789")
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        self.assertEqual(
                            exact_abs(Decimal("-12345678901234567890.123456789")),
                            expected_abs,
                        )
                        self.assertEqual(
                            exact_add(
                                Decimal("1234567890123456789012345678.1"),
                                Decimal("0.9"),
                            ),
                            expected_sum,
                        )
                        self.assertEqual(
                            exact_multiply(
                                Decimal("1234567890123456789012345678.123456789"),
                                Decimal("1"),
                            ),
                            expected_product,
                        )
                        self.assertEqual(
                            exact_sum(
                                (
                                    Decimal("1234567890123456789012345678.1"),
                                    Decimal("0.8"),
                                    Decimal("0.1"),
                                )
                            ),
                            expected_sum,
                        )

    def test_fraction_rounding_is_context_independent_and_directional(self):
        value = Fraction(1, 3)
        quantum = Decimal("0.0001")
        for precision in (2, 6, 28, 80):
            with localcontext() as context:
                context.prec = precision
                context.rounding = ROUND_CEILING
                self.assertEqual(
                    round_fraction_to_quantum(value, quantum, mode="FLOOR"),
                    Decimal("0.3333"),
                )
                self.assertEqual(
                    round_fraction_to_quantum(value, quantum, mode="CEILING"),
                    Decimal("0.3334"),
                )
                self.assertEqual(
                    round_fraction_to_quantum(Fraction(-1, 3), quantum, mode="FLOOR"),
                    Decimal("-0.3334"),
                )

    def test_half_even_ties_use_integer_arithmetic(self):
        quantum = Decimal("1")
        self.assertEqual(
            round_fraction_to_quantum(Fraction(5, 2), quantum, mode="HALF_EVEN"),
            Decimal("2"),
        )
        self.assertEqual(
            round_fraction_to_quantum(Fraction(7, 2), quantum, mode="HALF_EVEN"),
            Decimal("4"),
        )
        self.assertEqual(
            round_fraction_to_quantum(Fraction(-5, 2), quantum, mode="HALF_EVEN"),
            Decimal("-2"),
        )

    def test_nonterminating_fraction_never_silently_rounds(self):
        with self.assertRaisesRegex(ExactDecimalError, "non-terminating"):
            terminating_decimal(Fraction(1, 3))

    def test_decimal_fraction_and_text_preserve_exact_significance(self):
        value = Decimal("12345678901234567890.123450000")
        with localcontext() as context:
            context.prec = 6
            self.assertEqual(
                as_fraction(value),
                Fraction(246913578024691357802469, 20000),
            )
            self.assertEqual(
                canonical_decimal_text(value),
                "12345678901234567890.12345",
            )

    def test_resource_envelope_accepts_exact_boundaries(self):
        significant_boundary = Decimal(
            (0, tuple(9 for _ in range(MAX_SIGNIFICANT_DIGITS)), 0)
        )
        integer_boundary = Decimal((0, (1,), MAX_INTEGER_DIGITS - 1))
        scale_boundary = Decimal((0, (1,), -MAX_SCALE))

        self.assertEqual(exact_abs(significant_boundary), significant_boundary)
        self.assertEqual(
            as_fraction(integer_boundary),
            Fraction(10 ** (MAX_INTEGER_DIGITS - 1), 1),
        )
        self.assertEqual(
            as_fraction(scale_boundary),
            Fraction(1, 10**MAX_SCALE),
        )

    def test_resource_envelope_rejects_one_unit_over_and_extreme_values(self):
        excessive_significance = Decimal(
            (0, tuple(9 for _ in range(MAX_SIGNIFICANT_DIGITS + 1)), 0)
        )
        excessive_integer = Decimal((0, (1,), MAX_INTEGER_DIGITS))
        excessive_scale = Decimal((0, (1,), -(MAX_SCALE + 1)))
        bad_values = (
            excessive_significance,
            excessive_integer,
            excessive_scale,
        )
        for precision in (6, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    for value in bad_values:
                        with self.assertRaises(ExactDecimalError):
                            exact_abs(value)
                    with self.assertRaises(ExactDecimalError):
                        round_fraction_to_quantum(
                            Fraction(1, 3),
                            excessive_scale,
                            mode="FLOOR",
                        )

    def test_large_integer_reconstruction_never_depends_on_int_string_limit(self):
        # This coefficient is beyond CPython's default int-to-string safety
        # threshold. The public helper must reject it via integer-only bounds.
        with self.assertRaisesRegex(ExactDecimalError, "resource envelope"):
            terminating_decimal(Fraction(10**5000, 1))

    def test_exact_outputs_crossing_envelope_fail_closed(self):
        boundary = Decimal(
            (0, tuple(9 for _ in range(MAX_SIGNIFICANT_DIGITS)), 0)
        )
        with self.assertRaisesRegex(ExactDecimalError, "significant digits"):
            exact_add(boundary, Decimal("1"))
        with self.assertRaisesRegex(ExactDecimalError, "significant digits"):
            exact_multiply(boundary, Decimal("10"))


if __name__ == "__main__":
    unittest.main()
