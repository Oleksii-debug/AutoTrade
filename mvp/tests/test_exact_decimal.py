from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from fractions import Fraction
import unittest

from mvp.autotrade_mvp.exact_decimal import (
    ExactDecimalError,
    MAX_INTEGER_DIGITS,
    MAX_SCALE,
    MAX_SIGNIFICANT_DIGITS,
    as_fraction,
    bounded_fraction,
    canonical_decimal_text,
    exact_abs,
    exact_add,
    exact_multiply,
    exact_subtract,
    exact_sum,
    is_exact_decimal_multiple,
    round_fraction_to_quantum,
    terminating_decimal,
)


class HostileDecimal(Decimal):
    """Decimal subclass whose virtual methods must never become authority."""

    def is_finite(self):
        raise AssertionError("hostile Decimal.is_finite() was virtual-dispatched")

    def as_tuple(self):
        raise AssertionError("hostile Decimal.as_tuple() was virtual-dispatched")

    def __format__(self, format_spec):
        raise AssertionError("hostile Decimal.__format__() was virtual-dispatched")

    def __eq__(self, other):
        raise AssertionError("hostile Decimal.__eq__() was virtual-dispatched")


class HostileFraction(Fraction):
    """Fraction subclass whose rational properties must never be consulted."""

    numerator_reads = 0
    denominator_reads = 0

    @property
    def numerator(self):
        type(self).numerator_reads += 1
        raise AssertionError("hostile Fraction.numerator was virtual-dispatched")

    @property
    def denominator(self):
        type(self).denominator_reads += 1
        raise AssertionError("hostile Fraction.denominator was virtual-dispatched")


class HostileRoundingMode(str):
    """str subclass whose equality must never select rounding semantics."""

    equality_checks = 0

    def __eq__(self, other):
        type(self).equality_checks += 1
        raise AssertionError("hostile rounding-mode equality was virtual-dispatched")


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

    def test_exact_decimal_multiple_is_context_independent(self):
        aligned = Decimal("1.234567890123456789")
        off_grid = Decimal("1.2345678901234567891")
        quantum = Decimal("1e-18")
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        self.assertTrue(
                            is_exact_decimal_multiple(aligned, quantum)
                        )
                        self.assertFalse(
                            is_exact_decimal_multiple(off_grid, quantum)
                        )
        with self.assertRaisesRegex(ExactDecimalError, "quantum must be positive"):
            is_exact_decimal_multiple(Decimal("1"), Decimal("0"))

    def test_polymorphic_decimal_is_rejected_before_virtual_dispatch(self):
        hostile = HostileDecimal("1.25")
        operations = (
            lambda: as_fraction(hostile),
            lambda: canonical_decimal_text(hostile),
            lambda: exact_abs(hostile),
            lambda: exact_add(hostile, Decimal("1")),
            lambda: exact_subtract(Decimal("1"), hostile),
            lambda: exact_multiply(Decimal("2"), hostile),
            lambda: exact_sum((Decimal("1"), hostile)),
            lambda: exact_sum((Decimal("1"),), start=hostile),
            lambda: is_exact_decimal_multiple(hostile, Decimal("0.25")),
            lambda: is_exact_decimal_multiple(Decimal("1"), hostile),
            lambda: round_fraction_to_quantum(
                Fraction(1, 3), hostile, mode="FLOOR"
            ),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(
                    ExactDecimalError, "value must be a finite Decimal"
                ):
                    operation()

    def test_polymorphic_fraction_is_rejected_before_property_dispatch(self):
        HostileFraction.numerator_reads = 0
        HostileFraction.denominator_reads = 0
        hostile = HostileFraction(1, 3)
        operations = (
            lambda: bounded_fraction(hostile),
            lambda: terminating_decimal(hostile),
            lambda: round_fraction_to_quantum(
                hostile, Decimal("0.01"), mode="FLOOR"
            ),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(TypeError, "value must be Fraction"):
                    operation()
        self.assertEqual(HostileFraction.numerator_reads, 0)
        self.assertEqual(HostileFraction.denominator_reads, 0)

    def test_polymorphic_rounding_mode_is_rejected_before_equality_dispatch(self):
        HostileRoundingMode.equality_checks = 0
        hostile = HostileRoundingMode("FLOOR")
        with self.assertRaisesRegex(ExactDecimalError, "unsupported rounding mode"):
            round_fraction_to_quantum(
                Fraction(1, 3), Decimal("0.01"), mode=hostile
            )
        self.assertEqual(HostileRoundingMode.equality_checks, 0)

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

    def test_bounded_fraction_rejects_authority_beyond_rational_envelope(self):
        with self.assertRaisesRegex(ExactDecimalError, "numerator exceeds resource envelope"):
            bounded_fraction(Fraction(10**1024, 1))
        with self.assertRaisesRegex(ExactDecimalError, "denominator exceeds resource envelope"):
            bounded_fraction(Fraction(1, 10**1024))


if __name__ == "__main__":
    unittest.main()
