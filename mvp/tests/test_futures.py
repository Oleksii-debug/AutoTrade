from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
import unittest

from mvp.autotrade_mvp.accounting import EconomicBook
from mvp.autotrade_mvp.futures import (
    FuturesContract,
    InverseVariationMarginState,
    FuturesError,
    VariationMarginState,
    apply_variation_margin,
    book_variation_margin,
    apply_inverse_variation_margin,
    inverse_futures_pnl_exact,
    lifecycle_gate,
    linear_futures_pnl,
    require_open_for_new_exposure,
    settle_fraction,
    unrealized_inverse_after_variation,
    unrealized_after_variation,
)


def utc(day: int, hour: int = 0):
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


class FuturesLifecycleTests(unittest.TestCase):
    def _linear_contract(self, settlement_method="CASH"):
        return FuturesContract(
            instrument="FUT:TEST:202609",
            payoff="LINEAR",
            multiplier=Decimal("10"),
            quote_currency="USD",
            settlement_currency="USD",
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(29, 12),
            expiry=utc(30, 21),
            settlement_method=settlement_method,
        )

    def test_linear_fixture_matches_canonical_example(self):
        pnl = linear_futures_pnl(
            signed_contracts=Decimal("2"),
            multiplier=Decimal("10"),
            entry_price=Decimal("100"),
            exit_price=Decimal("103"),
        )
        self.assertEqual(pnl, Decimal("60"))
        self.assertEqual(
            linear_futures_pnl(
                signed_contracts=Decimal("-2"),
                multiplier=Decimal("10"),
                entry_price=Decimal("100"),
                exit_price=Decimal("103"),
            ),
            Decimal("-60"),
        )

    def test_inverse_fixture_keeps_exact_rational_until_settlement(self):
        pnl = inverse_futures_pnl_exact(
            signed_contracts=100,
            contract_quote_value=1,
            entry_price=10000,
            exit_price=11000,
        )
        self.assertEqual(pnl, Fraction(1, 1100))
        self.assertEqual(
            settle_fraction(pnl, quantum=Decimal("0.00000001")),
            Decimal("0.00090909"),
        )

    def test_variation_margin_is_not_counted_again_as_unrealized(self):
        state = VariationMarginState(
            contract=self._linear_contract(),
            signed_contracts=Decimal("2"),
            last_settlement_price=Decimal("100"),
        )
        settled, cash_flow = apply_variation_margin(state, Decimal("103"))
        self.assertEqual(cash_flow, Decimal("60"))
        self.assertEqual(settled.cumulative_variation_margin, Decimal("60"))
        self.assertEqual(unrealized_after_variation(settled, Decimal("104")), Decimal("20"))
        self.assertEqual(
            settled.cumulative_variation_margin
            + unrealized_after_variation(settled, Decimal("104")),
            Decimal("80"),
        )

    def test_inverse_variation_margin_remains_exact_until_settlement(self):
        contract = FuturesContract(
            instrument="BTC-USD-INVERSE",
            payoff="INVERSE",
            multiplier=Decimal("1"),
            quote_currency="USD",
            settlement_currency="BTC",
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(30, 20),
            expiry=utc(30, 21),
            settlement_method="CASH",
        )
        state = InverseVariationMarginState(
            contract=contract,
            signed_contracts=Decimal("100"),
            last_settlement_price=Decimal("10000"),
        )

        after_first, first = apply_inverse_variation_margin(state, "11000")
        self.assertEqual(first, Fraction(1, 1100))
        self.assertEqual(after_first.cumulative_variation_margin, Fraction(1, 1100))

        after_second, second = apply_inverse_variation_margin(after_first, "12000")
        direct = inverse_futures_pnl_exact(
            signed_contracts=100,
            contract_quote_value=1,
            entry_price=10000,
            exit_price=12000,
        )
        self.assertEqual(after_second.cumulative_variation_margin, direct)
        self.assertEqual(
            after_second.cumulative_variation_margin,
            first + second,
        )
        self.assertEqual(
            settle_fraction(
                after_second.cumulative_variation_margin,
                quantum=Decimal("0.00000001"),
            ),
            settle_fraction(direct, quantum=Decimal("0.00000001")),
        )

    def test_inverse_unrealized_marks_only_since_last_settlement(self):
        contract = FuturesContract(
            instrument="BTC-USD-INVERSE",
            payoff="INVERSE",
            multiplier=Decimal("1"),
            quote_currency="USD",
            settlement_currency="BTC",
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(30, 20),
            expiry=utc(30, 21),
            settlement_method="CASH",
        )
        state = InverseVariationMarginState(
            contract=contract,
            signed_contracts=Decimal("100"),
            last_settlement_price=Decimal("10000"),
        )
        settled, _ = apply_inverse_variation_margin(state, "11000")
        unrealized = unrealized_inverse_after_variation(settled, "12000")
        self.assertEqual(
            unrealized,
            inverse_futures_pnl_exact(
                signed_contracts=100,
                contract_quote_value=1,
                entry_price=11000,
                exit_price=12000,
            ),
        )

    def test_inverse_state_rejects_linear_contract_and_decimalized_accumulator(self):
        with self.assertRaisesRegex(FuturesError, "requires INVERSE"):
            InverseVariationMarginState(
                contract=self._linear_contract(),
                signed_contracts=Decimal("1"),
                last_settlement_price=Decimal("100"),
            )

        inverse = FuturesContract(
            instrument="BTC-USD-INVERSE",
            payoff="INVERSE",
            multiplier=Decimal("1"),
            quote_currency="USD",
            settlement_currency="BTC",
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(30, 20),
            expiry=utc(30, 21),
            settlement_method="CASH",
        )
        with self.assertRaisesRegex(FuturesError, "exact Fraction"):
            InverseVariationMarginState(
                contract=inverse,
                signed_contracts=Decimal("1"),
                last_settlement_price=Decimal("10000"),
                cumulative_variation_margin=Decimal("0"),
            )

    def test_variation_margin_books_balanced_cash_and_pnl(self):
        transaction = book_variation_margin(
            transaction_id="vm-1",
            cause_event_id="settlement-1",
            settlement_currency="USD",
            amount=Decimal("60"),
        )
        book = EconomicBook([transaction])
        self.assertEqual(book.cash("USD"), Decimal("60"))
        self.assertEqual(
            book.balance("FUTURES_VARIATION_PNL:USD", "USD"),
            Decimal("-60"),
        )

    def test_physical_delivery_is_fail_closed_without_explicit_authority(self):
        contract = self._linear_contract(settlement_method="PHYSICAL")
        self.assertEqual(lifecycle_gate(contract, utc(29, 11)), "OPEN")
        self.assertEqual(lifecycle_gate(contract, utc(29, 12)), "DELIVERY_BLOCKED")
        with self.assertRaises(FuturesError):
            require_open_for_new_exposure(contract, utc(29, 12))
        self.assertEqual(
            lifecycle_gate(contract, utc(29, 12), physical_delivery_authorized=True),
            "OPEN",
        )

    def test_last_trade_and_expiry_are_hard_gates(self):
        contract = self._linear_contract()
        self.assertEqual(lifecycle_gate(contract, utc(30, 20)), "TRADING_ENDED")
        self.assertEqual(lifecycle_gate(contract, utc(30, 21)), "EXPIRED")
        with self.assertRaises(FuturesError):
            require_open_for_new_exposure(contract, utc(30, 20))

    def test_float_inputs_are_rejected(self):
        with self.assertRaises(FuturesError):
            linear_futures_pnl(
                signed_contracts=1,
                multiplier=10,
                entry_price=100.0,
                exit_price=101,
            )

    def test_invalid_contract_time_order_is_rejected(self):
        with self.assertRaises(FuturesError):
            FuturesContract(
                instrument="bad",
                payoff="LINEAR",
                multiplier=Decimal("1"),
                quote_currency="USD",
                settlement_currency="USD",
                last_trade_at=utc(30, 22),
                delivery_cutoff=utc(29),
                expiry=utc(30, 21),
                settlement_method="CASH",
            )


if __name__ == "__main__":
    unittest.main()
