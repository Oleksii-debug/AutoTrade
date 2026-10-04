from decimal import Decimal
from fractions import Fraction
import unittest

import autotrade_numeric.exact_decimal as shared
import mvp.autotrade_mvp.exact_decimal as facade
from mvp.autotrade_mvp._generated_common_scalars import CONTRACT_VERSION as mvp_version
from autotrade_numeric._generated_common_scalars import CONTRACT_VERSION as shared_version


class SharedExactNumericTests(unittest.TestCase):
    def test_only_one_implementation_drives_both_callers(self):
        for name in (
            "parse_canonical_decimal_text", "as_fraction", "bounded_fraction",
            "exact_add", "exact_subtract", "round_fraction_to_quantum",
            "canonical_decimal_text",
        ):
            self.assertIs(getattr(facade, name), getattr(shared, name))
        self.assertEqual(shared_version, mvp_version)
        self.assertEqual(shared.MAX_SIGNIFICANT_DIGITS, 256)
        self.assertEqual(shared.as_fraction(Decimal("0.125")), Fraction(1, 8))

    def test_shared_runtime_retains_merged_v5_type_and_wire_fences(self):
        class HostileDecimal(Decimal):
            def as_tuple(self):
                raise AssertionError("subclass should never dispatch")
        class HostileText(str):
            def startswith(self, prefix):
                raise AssertionError("subclass should never dispatch")
        with self.assertRaises(shared.ExactDecimalError):
            facade.as_fraction(HostileDecimal("1.25"))
        with self.assertRaises(shared.ExactDecimalError):
            facade.parse_canonical_decimal_text(HostileText("1.25"))
        with self.assertRaises(shared.ExactDecimalError):
            shared.parse_canonical_decimal_text("1.0")
        self.assertEqual(facade.parse_canonical_decimal_text("0.1"), Decimal("0.1"))

    def test_zero_rounding_is_one_canonical_shared_source(self):
        result = shared.round_fraction_to_quantum(
            Fraction(0, 1), Decimal("0.00000001"), mode="HALF_EVEN"
        )
        self.assertEqual(shared.canonical_decimal_text(result), "0")
        self.assertEqual(facade.canonical_decimal_text(result), "0")


if __name__ == "__main__":
    unittest.main()
