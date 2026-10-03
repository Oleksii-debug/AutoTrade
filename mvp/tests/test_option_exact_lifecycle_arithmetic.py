"""Option lifecycle arithmetic is independent of process Decimal context."""
from dataclasses import replace
from decimal import Decimal, Inexact, Rounded, ROUND_CEILING, ROUND_FLOOR, localcontext
import unittest

from mvp.tests import test_options as fixtures
from mvp.autotrade_mvp.instruments import DeliverableLeg as InstrumentDeliverableLeg
from mvp.tests.test_option_lifecycle_registry_authority import option_version
from mvp.autotrade_mvp.options import (
    DeliverableLeg, OptionError, expiration_cash_settlement,
    expiration_pnl_after_premium, physical_exercise_obligation,
    interim_multi_leg_reservation, intrinsic_value_per_unit,
    require_physical_resources,
)
from mvp.autotrade_mvp.option_lifecycle import (
    OptionLifecycleError, _standard_physical_exercise_cash, _decimal,
)


class OptionExactLifecycleArithmeticTests(unittest.TestCase):
    def contexts(self):
        for precision in (1, 6):
            for rounding in (ROUND_CEILING, ROUND_FLOOR):
                yield precision, rounding

    def test_cash_settlement_and_premium_pnl_preserve_high_significance(self):
        contract = replace(fixtures.OptionLifecycleTests()._cash_call(),
                           strike=Decimal("100.000000001"), multiplier=Decimal("37.5"))
        for precision, rounding in self.contexts():
            with self.subTest(precision=precision, rounding=rounding), localcontext() as context:
                context.prec = precision; context.rounding = rounding
                context.traps[Inexact] = True; context.traps[Rounded] = True
                self.assertEqual(intrinsic_value_per_unit(contract, "100.000000003"), Decimal("0.000000002"))
                self.assertEqual(expiration_cash_settlement(contract, signed_contracts="-2", underlying_price="100.000000003"), Decimal("-0.000000150"))
                self.assertEqual(expiration_pnl_after_premium(contract, signed_contracts="2", premium_per_unit="0.000000001", underlying_price="100.000000003"), Decimal("0.000000075"))

    def test_adjusted_physical_deliverables_and_assignment_keep_exact_signs(self):
        contract = replace(fixtures.OptionLifecycleTests()._physical(),
                           deliverable=(DeliverableLeg("SHARES", Decimal("123456789.125")),
                                        DeliverableLeg("MERGER_RIGHT", Decimal("2.5"))),
                           exercise_cash_per_contract=Decimal("987654321.375"))
        for precision, rounding in self.contexts():
            with self.subTest(precision=precision, rounding=rounding), localcontext() as context:
                context.prec=precision; context.rounding=rounding
                context.traps[Inexact]=True; context.traps[Rounded]=True
                obligation=physical_exercise_obligation(contract, signed_contracts="-0.5")
                self.assertEqual(obligation.asset_quantities, (("SHARES", Decimal("-61728394.5625")), ("MERGER_RIGHT", Decimal("-1.25"))))
                self.assertEqual(obligation.settlement_cash, Decimal("493827160.6875"))
                require_physical_resources(obligation, asset_balances={"SHARES":"61728394.5625", "MERGER_RIGHT":"1.25"}, cash_balance="0")
                with self.assertRaisesRegex(OptionError, "insufficient evidenced balance"):
                    require_physical_resources(obligation, asset_balances={"SHARES":"61728394.5624", "MERGER_RIGHT":"1.25"}, cash_balance="0")

    def test_multi_leg_interim_loss_keeps_tiny_component_beside_large_loss(self):
        expected=Decimal("1000000000000000000000000000000.000000000000000000000000000001")
        for precision, rounding in self.contexts():
            with self.subTest(precision=precision, rounding=rounding), localcontext() as context:
                context.prec=precision; context.rounding=rounding
                context.traps[Inexact]=True; context.traps[Rounded]=True
                self.assertEqual(interim_multi_leg_reservation(["1e30", "1e-30"], atomic_package_guaranteed=False), expected)

    def test_registry_standard_exercise_cash_keeps_nondefault_multiplier(self):
        version=replace(option_version(quantity_step="1"),
                        strike=Decimal("123456789.125"), contract_multiplier=Decimal("37.5"),
                        deliverable=(InstrumentDeliverableLeg("ABC", Decimal("37.5")),))
        with localcontext() as context:
            context.prec=1; context.traps[Inexact]=True; context.traps[Rounded]=True
            self.assertEqual(_standard_physical_exercise_cash(version), Decimal("4629629592.1875"))

    def test_resource_overflow_and_hostile_decimal_fail_before_publication(self):
        contract=replace(fixtures.OptionLifecycleTests()._cash_call(), multiplier=Decimal("9"*256))
        with self.assertRaises(OptionError):
            expiration_cash_settlement(contract, signed_contracts="2", underlying_price="200")
        class Hostile(Decimal):
            def is_finite(self): raise AssertionError("virtual finite")
            def as_tuple(self): raise AssertionError("virtual tuple")
        with self.assertRaises((TypeError, OptionError)):
            intrinsic_value_per_unit(contract, Hostile("200"))
        with self.assertRaises((TypeError, OptionLifecycleError)):
            _decimal(Hostile("1"), "contracts")
        with self.assertRaises(OptionLifecycleError):
            _decimal("1e999999999", "contracts")


if __name__ == "__main__":
    unittest.main()
