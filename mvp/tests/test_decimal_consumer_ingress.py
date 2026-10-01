from decimal import Decimal
import unittest

from mvp.autotrade_mvp import economics
from mvp.autotrade_mvp import futures
from mvp.autotrade_mvp import fx_valuation
from mvp.autotrade_mvp import options
from mvp.autotrade_mvp import perpetuals


class HostileDecimal(Decimal):
    """A Decimal subclass whose virtual semantics must never become authority."""

    def is_finite(self):
        raise AssertionError("Decimal subclass is_finite() was dispatched")

    def as_tuple(self):
        raise AssertionError("Decimal subclass as_tuple() was dispatched")

    def __format__(self, format_spec):
        raise AssertionError("Decimal subclass __format__() was dispatched")

    def __eq__(self, other):
        raise AssertionError("Decimal subclass equality was dispatched")

    def __lt__(self, other):
        raise AssertionError("Decimal subclass ordering was dispatched")

    def __le__(self, other):
        raise AssertionError("Decimal subclass ordering was dispatched")

    def __gt__(self, other):
        raise AssertionError("Decimal subclass ordering was dispatched")

    def __ge__(self, other):
        raise AssertionError("Decimal subclass ordering was dispatched")


class DecimalConsumerIngressTests(unittest.TestCase):
    def test_financial_consumers_reject_decimal_subclasses_before_virtual_dispatch(self):
        hostile = HostileDecimal("1.25")
        cases = (
            (
                "fx valuation",
                lambda: fx_valuation._decimal(hostile, "value"),
                fx_valuation.FxValuationError,
            ),
            (
                "perpetuals",
                lambda: perpetuals._decimal(hostile, "value", positive=True),
                perpetuals.PerpetualError,
            ),
            (
                "economics",
                lambda: economics._decimal(hostile, name="value"),
                TypeError,
            ),
            (
                "futures",
                lambda: futures._decimal(hostile, "value", positive=True),
                futures.FuturesError,
            ),
            (
                "options",
                lambda: options._decimal(hostile, "value", positive=True),
                options.OptionError,
            ),
        )
        for name, operation, expected_error in cases:
            with self.subTest(name=name):
                with self.assertRaises(expected_error):
                    operation()


if __name__ == "__main__":
    unittest.main()
