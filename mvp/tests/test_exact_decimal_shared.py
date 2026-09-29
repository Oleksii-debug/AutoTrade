from decimal import Decimal
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


if __name__ == "__main__":
    unittest.main()
