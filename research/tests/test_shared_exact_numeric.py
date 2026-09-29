from decimal import Decimal
from fractions import Fraction
import unittest

from autotrade_numeric import (
    MAX_INTEGER_DIGITS,
    MAX_RATIONAL_DIGITS,
    MAX_SCALE,
    MAX_SIGNIFICANT_DIGITS,
    as_fraction,
    validate_fraction,
)


class SharedExactNumericDependencyTests(unittest.TestCase):
    def test_isolated_research_install_resolves_shared_numeric_authority(self):
        self.assertEqual(MAX_SIGNIFICANT_DIGITS, 256)
        self.assertEqual(MAX_SCALE, 256)
        self.assertEqual(MAX_INTEGER_DIGITS, 256)
        self.assertEqual(MAX_RATIONAL_DIGITS, 1024)
        self.assertEqual(as_fraction(Decimal("0.125")), Fraction(1, 8))
        self.assertEqual(validate_fraction(Fraction(1, 3)), Fraction(1, 3))


if __name__ == "__main__":
    unittest.main()
