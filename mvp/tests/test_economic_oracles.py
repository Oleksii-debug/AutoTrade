from decimal import Decimal
import unittest

from mvp.autotrade_mvp.economics import (
    apply_split,
    cash_round_trip,
    corrected_fill_cash_difference,
    investment_pnl_excluding_external_flows,
    inverse_futures_pnl,
    linear_funding_cashflow,
    linear_futures_mark_pnl,
)


class EconomicOracleTests(unittest.TestCase):
    def test_cash_round_trip_reference_vector(self):
        result = cash_round_trip(
            start_cash="1000",
            buy_quantity="2",
            buy_price="100",
            buy_fee="1",
            sell_quantity="1",
            sell_price="110",
            sell_fee="0.50",
            mark_price="105",
        )
        self.assertEqual(result.cash, Decimal("908.50"))
        self.assertEqual(result.position, Decimal("1"))
        self.assertEqual(result.gross_realized_pnl, Decimal("10"))
        self.assertEqual(result.gross_unrealized_pnl, Decimal("5"))
        self.assertEqual(result.fees, Decimal("1.50"))
        self.assertEqual(result.equity, Decimal("1013.50"))
        self.assertEqual(result.net_pnl, Decimal("13.50"))

    def test_linear_future_reference_vector(self):
        self.assertEqual(
            linear_futures_mark_pnl("2", "10", "100", "103"),
            Decimal("60"),
        )

    def test_inverse_future_reference_vector(self):
        result = inverse_futures_pnl("100", "1", "10000", "11000")
        self.assertEqual(
            result.quantize(Decimal("0.00000000001")),
            Decimal("0.00090909091"),
        )

    def test_funding_reference_vector(self):
        self.assertEqual(
            linear_funding_cashflow("1000", "0.0001", side="LONG"),
            Decimal("-0.1000"),
        )
        self.assertEqual(
            linear_funding_cashflow("1000", "0.0001", side="SHORT"),
            Decimal("0.1000"),
        )

    def test_split_reference_vector_preserves_basis(self):
        result = apply_split("10", "100", numerator="2")
        self.assertEqual(result.quantity, Decimal("20"))
        self.assertEqual(result.unit_basis, Decimal("50"))
        self.assertEqual(result.total_basis, Decimal("1000"))

    def test_deposit_is_not_strategy_profit(self):
        self.assertEqual(
            investment_pnl_excluding_external_flows("1000", "1500", "500"),
            Decimal("0"),
        )

    def test_bust_correction_reference_vector(self):
        self.assertEqual(
            corrected_fill_cash_difference("2", "100", "101", side="BUY"),
            Decimal("-2"),
        )

    def test_invalid_values_fail_closed(self):
        with self.assertRaises(ValueError):
            cash_round_trip(
                start_cash="1000",
                buy_quantity="1",
                buy_price="100",
                buy_fee="0",
                sell_quantity="2",
                sell_price="101",
                sell_fee="0",
                mark_price="101",
            )
        with self.assertRaises(ValueError):
            inverse_futures_pnl("1", "1", "0", "100")


if __name__ == "__main__":
    unittest.main()
