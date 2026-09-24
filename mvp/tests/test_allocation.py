from decimal import Decimal
import unittest

from mvp.autotrade_mvp.allocation import (
    AllocationCandidate,
    AllocationPolicy,
    allocate_targets,
)


class AllocationTests(unittest.TestCase):
    def policy(self, **overrides):
        values = {
            "cash_available": "1000",
            "max_gross_notional": "1000",
            "max_net_notional": "1000",
            "max_symbol_notional": "1000",
            "max_total_cost": "50",
            "max_stress_loss": "500",
            "max_iterations": 64,
        }
        values.update(overrides)
        return AllocationPolicy.create(**values)

    def candidate(
        self,
        symbol="AAA",
        desired="500",
        price="10",
        lot="1",
        cost="0",
        capital_requirement="1",
    ):
        return AllocationCandidate.create(
            symbol=symbol,
            desired_notional=desired,
            price=price,
            lot_size=lot,
            cost_rate=cost,
            capital_requirement_rate=capital_requirement,
        )

    def test_funded_request_is_accepted_without_scaling(self):
        result = allocate_targets([self.candidate()], self.policy())
        self.assertEqual(result.status, "ALLOCATED")
        self.assertEqual(result.scale, Decimal("1"))
        self.assertEqual(result.targets[0].quantity, Decimal("50"))
        self.assertEqual(result.cash_required, Decimal("500"))

    def test_unfunded_request_is_scaled_without_using_short_proceeds(self):
        result = allocate_targets(
            [
                self.candidate("LONG", desired="1600", price="10"),
                self.candidate("SHORT", desired="-1000", price="10"),
            ],
            self.policy(cash_available="600", max_gross_notional="5000", max_net_notional="5000"),
        )
        self.assertEqual(result.status, "ALLOCATED")
        self.assertLess(result.scale, Decimal("1"))
        long_target = next(item for item in result.targets if item.symbol == "LONG")
        self.assertLessEqual(long_target.notional, Decimal("600"))
        self.assertLessEqual(result.cash_required, Decimal("600"))

    def test_pure_short_cannot_bypass_cash_budget_with_sale_proceeds(self):
        result = allocate_targets(
            [self.candidate("SHORT", desired="-1000", price="10")],
            self.policy(
                cash_available="100",
                max_gross_notional="5000",
                max_net_notional="5000",
                max_symbol_notional="5000",
            ),
        )
        self.assertEqual(result.status, "ALLOCATED")
        self.assertLess(result.scale, Decimal("1"))
        self.assertLessEqual(result.cash_required, Decimal("100"))
        self.assertGreater(abs(result.targets[0].notional), Decimal("0"))

    def test_explicit_margin_rate_allows_only_evidenced_leverage(self):
        result = allocate_targets(
            [
                self.candidate(
                    "MARGIN_SHORT",
                    desired="-1000",
                    price="10",
                    capital_requirement="0.20",
                )
            ],
            self.policy(
                cash_available="200",
                max_gross_notional="5000",
                max_net_notional="5000",
                max_symbol_notional="5000",
            ),
        )
        self.assertEqual(result.status, "ALLOCATED")
        self.assertEqual(result.scale, Decimal("1"))
        self.assertEqual(result.cash_required, Decimal("200"))

    def test_capital_requirement_rejects_binary_float_input(self):
        with self.assertRaises(TypeError):
            self.candidate(capital_requirement=0.2)

    def test_correlation_stress_caps_joint_exposure(self):
        result = allocate_targets(
            [
                self.candidate("AAA", desired="500", price="10"),
                self.candidate("BBB", desired="500", price="10"),
            ],
            self.policy(
                cash_available="2000",
                max_gross_notional="2000",
                max_net_notional="2000",
                max_stress_loss="100",
            ),
            stress_scenarios={"joint_down": {"AAA": "-0.20", "BBB": "-0.20"}},
        )
        self.assertEqual(result.status, "ALLOCATED")
        self.assertLess(result.scale, Decimal("1"))
        self.assertLessEqual(result.worst_stress_loss, Decimal("100"))

    def test_incomplete_stress_scenario_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "missing explicit shocks"):
            allocate_targets(
                [
                    self.candidate("AAA", desired="500", price="10"),
                    self.candidate("BBB", desired="500", price="10"),
                ],
                self.policy(cash_available="2000", max_gross_notional="2000"),
                stress_scenarios={"partial": {"AAA": "-0.20"}},
            )

    def test_minimum_lot_can_force_cash_fallback(self):
        result = allocate_targets(
            [self.candidate(desired="100", price="100", lot="1")],
            self.policy(
                cash_available="50",
                max_gross_notional="1000",
                max_net_notional="1000",
                max_symbol_notional="1000",
            ),
        )
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(result.gross_notional, Decimal("0"))

    def test_hard_symbol_limit_is_respected(self):
        result = allocate_targets(
            [self.candidate(desired="1000", price="10", lot="1")],
            self.policy(
                cash_available="2000",
                max_gross_notional="2000",
                max_net_notional="2000",
                max_symbol_notional="250",
            ),
        )
        self.assertEqual(result.status, "ALLOCATED")
        self.assertLessEqual(abs(result.targets[0].notional), Decimal("250"))

    def test_cost_budget_is_a_hard_constraint(self):
        result = allocate_targets(
            [self.candidate(desired="1000", price="10", lot="1", cost="0.10")],
            self.policy(
                cash_available="2000",
                max_gross_notional="2000",
                max_net_notional="2000",
                max_total_cost="20",
            ),
        )
        self.assertEqual(result.status, "ALLOCATED")
        self.assertLessEqual(result.estimated_cost, Decimal("20"))

    def test_bounded_search_fails_closed_when_one_iteration_cannot_reach_positive_lot(self):
        result = allocate_targets(
            [self.candidate(desired="1000", price="100", lot="1")],
            self.policy(
                cash_available="10",
                max_gross_notional="1000",
                max_net_notional="1000",
                max_symbol_notional="1000",
                max_iterations=1,
            ),
        )
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(result.scale, Decimal("0"))

    def test_duplicate_symbols_are_rejected(self):
        with self.assertRaises(ValueError):
            allocate_targets(
                [self.candidate("AAA"), self.candidate("AAA")],
                self.policy(),
            )


if __name__ == "__main__":
    unittest.main()
