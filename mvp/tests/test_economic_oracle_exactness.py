from decimal import Decimal
import unittest

from mvp.autotrade_mvp.economics import (
    apply_split,
    cash_round_trip,
    corrected_fill_cash_difference,
    inverse_futures_pnl,
    investment_pnl_excluding_external_flows,
    linear_funding_cashflow,
    linear_futures_mark_pnl,
)


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


if __name__ == "__main__":
    unittest.main()
