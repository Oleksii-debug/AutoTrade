from decimal import Decimal
import unittest

from autotrade_research.economics.after_cost import (
    CostBreakdown,
    TradeEconomics,
    qualify_after_cost_edge,
)
from autotrade_research.economics.stress import CostScenario, qualify_cost_stress


class AfterCostEconomicTests(unittest.TestCase):
    def trade(self, costs=None):
        return TradeEconomics(
            capital_at_risk=Decimal("1000"),
            expected_gross_pnl=Decimal("30"),
            worst_case_loss=Decimal("100"),
            costs=costs or CostBreakdown(),
            minimum_practical_advantage=Decimal("5"),
        )

    def test_all_cost_classes_are_counted_exactly(self):
        costs = CostBreakdown(
            commission=Decimal("1"),
            spread=Decimal("2"),
            slippage=Decimal("3"),
            financing=Decimal("4"),
            funding=Decimal("5"),
            borrow=Decimal("6"),
            market_data=Decimal("7"),
            model_compute=Decimal("8"),
            infrastructure=Decimal("9"),
            tax_estimate=Decimal("10"),
        )
        self.assertEqual(costs.total, Decimal("55"))

    def test_after_cost_edge_is_research_only(self):
        result = qualify_after_cost_edge(
            self.trade(CostBreakdown(commission=Decimal("2"), spread=Decimal("3"))),
            max_loss_budget="150",
        )
        self.assertEqual(result.disposition, "ADMISSIBLE_FOR_RESEARCH")
        self.assertEqual(result.expected_net_pnl, Decimal("25"))
        self.assertFalse(result.live_authority_granted)

    def test_costs_can_erase_apparent_gross_edge(self):
        result = qualify_after_cost_edge(
            self.trade(CostBreakdown(slippage=Decimal("28"))),
            max_loss_budget="150",
        )
        self.assertEqual(result.disposition, "REJECT")
        self.assertIn("NO_AFTER_COST_PRACTICAL_EDGE", result.reason_codes)

    def test_loss_and_minimum_notional_are_hard_gates(self):
        result = qualify_after_cost_edge(
            self.trade(),
            max_loss_budget="50",
            minimum_order_notional="2000",
            proposed_notional="1000",
        )
        self.assertIn("LOSS_BUDGET_EXCEEDED", result.reason_codes)
        self.assertIn("BELOW_MINIMUM_NOTIONAL", result.reason_codes)

    def test_every_stress_scenario_must_preserve_edge(self):
        result = qualify_cost_stress(
            self.trade(),
            [
                CostScenario("base", CostBreakdown(commission=Decimal("2"))),
                CostScenario("adverse", CostBreakdown(commission=Decimal("4"), slippage=Decimal("10"))),
            ],
            max_loss_budget="150",
        )
        self.assertEqual(result.disposition, "ROBUST_FOR_RESEARCH")
        self.assertEqual(result.worst_expected_net_pnl, Decimal("16"))
        self.assertFalse(result.live_authority_granted)

    def test_one_failed_cost_scenario_rejects_whole_candidate(self):
        result = qualify_cost_stress(
            self.trade(),
            [
                CostScenario("base", CostBreakdown(commission=Decimal("2"))),
                CostScenario("liquidity_stress", CostBreakdown(slippage=Decimal("29"))),
            ],
            max_loss_budget="150",
        )
        self.assertEqual(result.disposition, "REJECT")
        self.assertIn("SCENARIO_REJECTED:liquidity_stress", result.reason_codes)

    def test_float_and_negative_costs_are_rejected(self):
        with self.assertRaises(ValueError):
            CostBreakdown(commission=1.0)
        with self.assertRaises(ValueError):
            CostBreakdown(spread=Decimal("-1"))

    def test_duplicate_or_missing_scenarios_are_rejected(self):
        with self.assertRaises(ValueError):
            qualify_cost_stress(self.trade(), [], max_loss_budget="150")
        with self.assertRaises(ValueError):
            qualify_cost_stress(
                self.trade(),
                [CostScenario("x", CostBreakdown()), CostScenario("x", CostBreakdown())],
                max_loss_budget="150",
            )


if __name__ == "__main__":
    unittest.main()
