from datetime import datetime, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.accounting import EconomicBook
from mvp.autotrade_mvp.options import (
    DeliverableLeg,
    OptionContract,
    OptionError,
    book_cash_option_settlement,
    book_physical_option_settlement,
    exercise_gate,
    expiration_cash_settlement,
    expiration_pnl_after_premium,
    interim_multi_leg_reservation,
    intrinsic_value_per_unit,
    physical_exercise_obligation,
    require_holder_exercise_open,
    require_physical_resources,
)


def at(hour: int):
    return datetime(2026, 9, 25, hour, tzinfo=timezone.utc)


class OptionLifecycleTests(unittest.TestCase):
    def _cash_call(self):
        return OptionContract(
            instrument="OPT:CALL",
            right="CALL",
            strike=Decimal("100"),
            multiplier=Decimal("100"),
            settlement_currency="USD",
            settlement_method="CASH",
            exercise_style="EUROPEAN",
            exercise_cutoff=at(19),
            expiry=at(20),
            exercise_opens_at=at(18),
        )

    def _physical(self, right="CALL"):
        return OptionContract(
            instrument=f"OPT:{right}",
            right=right,
            strike=Decimal("50"),
            multiplier=Decimal("150"),
            settlement_currency="USD",
            settlement_method="PHYSICAL",
            exercise_style="AMERICAN",
            exercise_cutoff=at(19),
            expiry=at(20),
            deliverable=(
                DeliverableLeg("SHARES:ADJUSTED", Decimal("150")),
                DeliverableLeg("CASHLIKE:MERGER_RIGHT", Decimal("2.5")),
            ),
        )

    def test_call_and_put_intrinsic_values_are_exact(self):
        call = self._cash_call()
        self.assertEqual(intrinsic_value_per_unit(call, "112.5"), Decimal("12.5"))
        self.assertEqual(intrinsic_value_per_unit(call, "90"), Decimal("0"))

        put = OptionContract(
            instrument="OPT:PUT",
            right="PUT",
            strike=Decimal("100"),
            multiplier=Decimal("100"),
            settlement_currency="USD",
            settlement_method="CASH",
            exercise_style="EUROPEAN",
            exercise_cutoff=at(19),
            expiry=at(20),
        )
        self.assertEqual(intrinsic_value_per_unit(put, "80"), Decimal("20"))

    def test_cash_expiry_and_premium_pnl_use_signed_contracts(self):
        call = self._cash_call()
        self.assertEqual(
            expiration_cash_settlement(call, signed_contracts=2, underlying_price="110"),
            Decimal("2000"),
        )
        self.assertEqual(
            expiration_pnl_after_premium(
                call,
                signed_contracts=2,
                premium_per_unit="3",
                underlying_price="110",
            ),
            Decimal("1400"),
        )
        self.assertEqual(
            expiration_pnl_after_premium(
                call,
                signed_contracts=-2,
                premium_per_unit="3",
                underlying_price="110",
            ),
            Decimal("-1400"),
        )

    def test_adjusted_deliverable_never_assumes_one_hundred_shares(self):
        call = self._physical("CALL")
        obligation = physical_exercise_obligation(call, signed_contracts=2)
        self.assertEqual(
            obligation.asset_quantities,
            (
                ("SHARES:ADJUSTED", Decimal("300")),
                ("CASHLIKE:MERGER_RIGHT", Decimal("5.0")),
            ),
        )
        self.assertEqual(obligation.settlement_cash, Decimal("-15000"))

    def test_put_and_short_assignment_reverse_physical_direction(self):
        put = self._physical("PUT")
        long_put = physical_exercise_obligation(put, signed_contracts=1)
        self.assertEqual(long_put.asset_quantities[0][1], Decimal("-150"))
        self.assertEqual(long_put.settlement_cash, Decimal("7500"))

        short_put = physical_exercise_obligation(put, signed_contracts=-1)
        self.assertEqual(short_put.asset_quantities[0][1], Decimal("150"))
        self.assertEqual(short_put.settlement_cash, Decimal("-7500"))

    def test_physical_resource_check_fails_closed(self):
        put = self._physical("PUT")
        obligation = physical_exercise_obligation(put, signed_contracts=1)
        with self.assertRaises(OptionError):
            require_physical_resources(
                obligation,
                asset_balances={"SHARES:ADJUSTED": "100"},
                cash_balance="0",
            )
        require_physical_resources(
            obligation,
            asset_balances={
                "SHARES:ADJUSTED": "150",
                "CASHLIKE:MERGER_RIGHT": "2.5",
            },
            cash_balance="0",
        )

    def test_non_atomic_multileg_reserves_all_interim_leg_loss(self):
        self.assertEqual(
            interim_multi_leg_reservation(
                ["100", "60", "25"],
                atomic_package_guaranteed=False,
            ),
            Decimal("185"),
        )
        with self.assertRaises(OptionError):
            interim_multi_leg_reservation(
                ["100", "60"],
                atomic_package_guaranteed=False,
                package_worst_case_loss="20",
            )
        self.assertEqual(
            interim_multi_leg_reservation(
                ["100", "60"],
                atomic_package_guaranteed=True,
                package_worst_case_loss="20",
            ),
            Decimal("20"),
        )

    def test_european_exercise_requires_explicit_window_evidence(self):
        unknown = OptionContract(
            instrument="OPT:EUROPEAN:UNKNOWN",
            right="CALL",
            strike=Decimal("100"),
            multiplier=Decimal("100"),
            settlement_currency="USD",
            settlement_method="CASH",
            exercise_style="EUROPEAN",
            exercise_cutoff=at(19),
            expiry=at(20),
        )
        self.assertEqual(exercise_gate(unknown, at(18)), "EXERCISE_SCHEDULE_UNKNOWN")
        with self.assertRaises(OptionError):
            require_holder_exercise_open(unknown, at(18))

        known = self._cash_call()
        self.assertEqual(exercise_gate(known, at(17)), "EXERCISE_NOT_YET_OPEN")
        self.assertEqual(exercise_gate(known, at(18)), "OPEN")

    def test_invalid_exercise_style_is_rejected(self):
        with self.assertRaises(OptionError):
            OptionContract(
                instrument="OPT:BAD",
                right="CALL",
                strike=Decimal("100"),
                multiplier=Decimal("100"),
                settlement_currency="USD",
                settlement_method="CASH",
                exercise_style="UNKNOWN",
                exercise_cutoff=at(19),
                expiry=at(20),
            )

    def test_exercise_cutoff_and_expiry_are_hard_gates(self):
        contract = self._cash_call()
        self.assertEqual(exercise_gate(contract, at(18)), "OPEN")
        self.assertEqual(exercise_gate(contract, at(19)), "EXERCISE_WINDOW_CLOSED")
        self.assertEqual(exercise_gate(contract, at(20)), "EXPIRED")
        with self.assertRaises(OptionError):
            require_holder_exercise_open(contract, at(19))

    def test_cash_and_physical_settlements_balance_by_unit(self):
        cash_tx = book_cash_option_settlement(
            transaction_id="option-cash-1",
            cause_event_id="expiry-1",
            settlement_currency="USD",
            amount="2000",
        )
        book = EconomicBook([cash_tx])
        self.assertEqual(book.cash("USD"), Decimal("2000"))

        obligation = physical_exercise_obligation(self._physical("CALL"), signed_contracts=1)
        physical_tx = book_physical_option_settlement(
            transaction_id="option-physical-1",
            cause_event_id="exercise-1",
            obligation=obligation,
        )
        physical_book = EconomicBook([physical_tx])
        self.assertEqual(
            physical_book.position("SHARES:ADJUSTED"),
            Decimal("150"),
        )
        self.assertEqual(physical_book.cash("USD"), Decimal("-7500"))

    def test_float_inputs_are_rejected(self):
        with self.assertRaises(OptionError):
            intrinsic_value_per_unit(self._cash_call(), 101.0)


if __name__ == "__main__":
    unittest.main()
