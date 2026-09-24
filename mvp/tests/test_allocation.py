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
            "require_adverse_stress_evidence": False,
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
        min_notional="0",
        fee_floor="0",
        max_executable_notional=None,
    ):
        return AllocationCandidate.create(
            symbol=symbol,
            desired_notional=desired,
            price=price,
            lot_size=lot,
            cost_rate=cost,
            capital_requirement_rate=capital_requirement,
            min_notional=min_notional,
            fee_floor=fee_floor,
            max_executable_notional=max_executable_notional,
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

    def test_direct_candidate_construction_cannot_create_free_capital(self):
        with self.assertRaisesRegex(
            ValueError,
            "capital_requirement_rate must be positive",
        ):
            AllocationCandidate(
                symbol="SHORT",
                desired_notional=Decimal("-1000"),
                price=Decimal("10"),
                lot_size=Decimal("1"),
                capital_requirement_rate=Decimal("0"),
            )

    def test_direct_policy_construction_cannot_bypass_financial_bounds(self):
        with self.assertRaisesRegex(ValueError, "cash_available must be non-negative"):
            AllocationPolicy(
                cash_available=Decimal("-1"),
                max_gross_notional=Decimal("1000"),
                max_net_notional=Decimal("1000"),
                max_symbol_notional=Decimal("1000"),
                max_total_cost=Decimal("100"),
                max_stress_loss=Decimal("100"),
            )
        with self.assertRaisesRegex(ValueError, "max_iterations must be a positive integer"):
            AllocationPolicy(
                cash_available=Decimal("1000"),
                max_gross_notional=Decimal("1000"),
                max_net_notional=Decimal("1000"),
                max_symbol_notional=Decimal("1000"),
                max_total_cost=Decimal("100"),
                max_stress_loss=Decimal("100"),
                max_iterations=0,
            )

    def test_capital_requirement_rejects_binary_float_input(self):
        with self.assertRaises(TypeError):
            self.candidate(capital_requirement=0.2)

    def test_capital_requirement_must_be_strictly_positive(self):
        for invalid in ("0", "-0.01"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "capital_requirement_rate must be positive"):
                    self.candidate(capital_requirement=invalid)

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

    def test_zero_desired_targets_are_explicit_cash_fallback_not_allocated(self):
        result = allocate_targets(
            [self.candidate("ZERO", desired="0", price="10", lot="1")],
            self.policy(),
        )
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(result.scale, Decimal("0"))
        self.assertEqual(result.gross_notional, Decimal("0"))
        self.assertEqual(result.targets[0].quantity, Decimal("0"))

    def test_minimum_notional_never_proposes_an_unexecutable_small_trade(self):
        result = allocate_targets(
            [self.candidate(desired="40", price="10", lot="1", min_notional="50")],
            self.policy(
                cash_available="1000",
                max_gross_notional="1000",
                max_net_notional="1000",
                max_symbol_notional="1000",
            ),
        )
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(result.targets[0].quantity, Decimal("0"))
        self.assertEqual(result.targets[0].notional, Decimal("0"))

    def test_fee_floor_is_charged_once_for_each_nonzero_target(self):
        result = allocate_targets(
            [self.candidate(desired="100", price="10", lot="1", cost="0.001", fee_floor="5")],
            self.policy(
                cash_available="1000",
                max_gross_notional="1000",
                max_net_notional="1000",
                max_total_cost="10",
            ),
        )
        self.assertEqual(result.status, "ALLOCATED")
        self.assertEqual(result.estimated_cost, Decimal("5"))
        self.assertEqual(result.cash_required, Decimal("105"))

    def test_fee_floor_can_force_cash_fallback_when_no_trade_is_affordable(self):
        result = allocate_targets(
            [self.candidate(desired="1000", price="10", lot="1", fee_floor="25")],
            self.policy(
                cash_available="2000",
                max_gross_notional="2000",
                max_net_notional="2000",
                max_total_cost="20",
            ),
        )
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(result.estimated_cost, Decimal("0"))

    def test_minimum_trade_inputs_reject_binary_float(self):
        with self.assertRaises(TypeError):
            self.candidate(min_notional=10.0)
        with self.assertRaises(TypeError):
            self.candidate(fee_floor=1.0)

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

    def test_default_policy_requires_adverse_stress_evidence(self):
        policy = self.policy(require_adverse_stress_evidence=True)
        result = allocate_targets([self.candidate()], policy)
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertIn("no stress scenarios were supplied", result.reason)

    def test_required_stress_must_be_adverse_for_requested_direction(self):
        policy = self.policy(require_adverse_stress_evidence=True)
        short = self.candidate("SHORT", desired="-500")
        wrong_direction = allocate_targets(
            [short],
            policy,
            stress_scenarios={"down_only": {"SHORT": "-0.20"}},
        )
        self.assertEqual(wrong_direction.status, "NO_INCREASE_FALLBACK")
        self.assertIn("SHORT", wrong_direction.reason)

        covered = allocate_targets(
            [short],
            policy,
            stress_scenarios={"short_squeeze": {"SHORT": "0.20"}},
        )
        self.assertEqual(covered.status, "ALLOCATED")
        self.assertGreater(covered.worst_stress_loss, Decimal("0"))

    def test_liquidity_capacity_caps_requested_target_without_increasing_risk(self):
        result = allocate_targets(
            [
                self.candidate(
                    desired="500",
                    price="10",
                    lot="1",
                    max_executable_notional="120",
                )
            ],
            self.policy(),
        )
        self.assertEqual(result.status, "ALLOCATED")
        self.assertEqual(result.targets[0].quantity, Decimal("12"))
        self.assertEqual(result.targets[0].notional, Decimal("120"))

    def test_liquidity_capacity_below_minimum_notional_falls_back_to_cash(self):
        result = allocate_targets(
            [
                self.candidate(
                    desired="500",
                    price="10",
                    lot="1",
                    min_notional="100",
                    max_executable_notional="90",
                )
            ],
            self.policy(),
        )
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(result.targets[0].notional, Decimal("0"))

    def test_liquidity_capacity_rejects_binary_float(self):
        with self.assertRaises(TypeError):
            self.candidate(max_executable_notional=100.0)

    def test_stress_requirement_flag_must_be_boolean(self):
        with self.assertRaises(TypeError):
            self.policy(require_adverse_stress_evidence="yes")

    def test_duplicate_symbols_are_rejected(self):
        with self.assertRaises(ValueError):
            allocate_targets(
                [self.candidate("AAA"), self.candidate("AAA")],
                self.policy(),
            )


if __name__ == "__main__":
    unittest.main()
