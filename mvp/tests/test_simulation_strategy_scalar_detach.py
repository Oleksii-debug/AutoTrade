"""Regression preserving the parent simulation strategy exact-scalar authority."""

from decimal import Decimal
import unittest

from mvp.autotrade_mvp.exact_decimal import ExactDecimalError
from mvp.autotrade_mvp.pipeline import MovingAverageStrategy


class HostileDecimal(Decimal):
    """Decimal subclass that must not escape through the strategy boundary."""

    def is_finite(self):
        raise AssertionError("hostile Decimal.is_finite() was virtual-dispatched")

    def as_tuple(self):
        raise AssertionError("hostile Decimal.as_tuple() was virtual-dispatched")


class SimulationStrategyScalarDetachTests(unittest.TestCase):
    def test_strategy_detaches_consumed_values_and_ignores_unused_prefix(self):
        strategy = MovingAverageStrategy()
        decision = strategy.decide(
            [
                HostileDecimal("999"),
                Decimal("100.0"),
                Decimal("101.00"),
                Decimal("103.000"),
            ],
            Decimal("1.000"),
        )
        self.assertEqual(decision.side, "BUY")
        self.assertIs(type(decision.quantity), Decimal)
        self.assertIs(type(decision.price), Decimal)
        self.assertEqual(decision.quantity, Decimal("1"))
        self.assertEqual(decision.price, Decimal("103"))

        hold = strategy.decide(
            [HostileDecimal("999"), Decimal("100.0")], Decimal("1.000")
        )
        self.assertEqual(hold.side, "HOLD")
        self.assertIs(type(hold.price), Decimal)
        self.assertEqual(hold.price, Decimal("100"))

        with self.assertRaisesRegex(ExactDecimalError, "finite Decimal"):
            strategy.decide(
                [Decimal("100"), HostileDecimal("101"), Decimal("103")],
                Decimal("1"),
            )
        with self.assertRaisesRegex(ExactDecimalError, "maximum integer digits"):
            strategy.decide(
                [Decimal("100"), Decimal("101"), Decimal("1e256")],
                Decimal("1"),
            )


if __name__ == "__main__":
    unittest.main()
