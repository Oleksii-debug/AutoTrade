from decimal import Decimal
from fractions import Fraction
import unittest

import autotrade_numeric.exact_decimal as shared
import mvp.autotrade_mvp.exact_decimal as facade


class ExactDecimalSharedFacadeTests(unittest.TestCase):
    def test_mvp_facade_uses_exact_same_shared_implementation(self):
        self.assertIs(facade.as_fraction, shared.as_fraction)
        self.assertIs(facade.validate_fraction, shared.validate_fraction)
        self.assertIs(facade.exact_add, shared.exact_add)
        self.assertEqual(
            facade.as_fraction(Decimal("1.25")),
            shared.as_fraction(Decimal("1.25")),
        )

    def test_zero_rounding_preserves_explicit_reporting_quantum_scale(self):
        rounded = shared.round_fraction_to_quantum(
            Fraction(0, 1),
            Decimal("0.00000001"),
            mode="HALF_EVEN",
        )

        self.assertEqual(str(rounded), "0E-8")
        self.assertEqual(rounded.as_tuple().exponent, -8)
        self.assertEqual(shared.canonical_decimal_text(rounded), "0")
        self.assertEqual(
            facade.round_fraction_to_quantum(
                Fraction(0, 1),
                Decimal("0.00000001"),
                mode="HALF_EVEN",
            ),
            rounded,
        )


if __name__ == "__main__":
    unittest.main()
