from decimal import Decimal
import unittest

from mvp.autotrade_mvp.allocation import (
    AllocationCandidate,
    AllocationPolicy,
    ObjectiveCandidate,
    StressScenarioEvidence,
    allocate_objective_targets,
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
            "max_turnover_notional": "1000",
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
        current_quantity="0",
        turnover_cost_rate=None,
        holding_cost_rate=None,
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
            current_quantity=current_quantity,
            turnover_cost_rate=turnover_cost_rate,
            holding_cost_rate=holding_cost_rate,
        )

    def test_funded_request_is_accepted_without_scaling(self):
        result = allocate_targets([self.candidate()], self.policy())
        self.assertEqual(result.status, "ALLOCATED")
        self.assertEqual(result.scale, Decimal("1"))
        self.assertEqual(result.targets[0].quantity, Decimal("50"))
        self.assertEqual(result.cash_required, Decimal("500"))

    def test_turnover_limit_scales_from_reconciled_current_position(self):
        result = allocate_targets(
            [
                self.candidate(
                    desired="500",
                    price="10",
                    current_quantity="20",
                    turnover_cost_rate="0",
                    holding_cost_rate="0",
                )
            ],
            self.policy(
                cash_available="2000",
                max_turnover_notional="100",
            ),
        )
        self.assertEqual(result.status, "ALLOCATED")
        self.assertLess(result.scale, Decimal("1"))
        self.assertEqual(result.targets[0].quantity, Decimal("30"))
        self.assertEqual(result.targets[0].notional, Decimal("300"))
        self.assertEqual(result.targets[0].turnover_notional, Decimal("100"))
        self.assertEqual(result.turnover_notional, Decimal("100"))

    def test_zero_turnover_budget_preserves_current_position_fail_closed(self):
        result = allocate_targets(
            [
                self.candidate(
                    desired="500",
                    price="10",
                    current_quantity="20",
                    turnover_cost_rate="0",
                    holding_cost_rate="0",
                )
            ],
            self.policy(
                cash_available="2000",
                max_turnover_notional="0",
            ),
        )
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(result.targets[0].quantity, Decimal("20"))
        self.assertEqual(result.targets[0].notional, Decimal("200"))
        self.assertEqual(result.turnover_notional, Decimal("0"))

    def test_cost_split_prices_turnover_and_holding_exposure_separately(self):
        result = allocate_targets(
            [
                self.candidate(
                    desired="300",
                    price="10",
                    current_quantity="20",
                    cost="0.03",
                    turnover_cost_rate="0.01",
                    holding_cost_rate="0.02",
                )
            ],
            self.policy(cash_available="2000"),
        )
        self.assertEqual(result.targets[0].turnover_notional, Decimal("100"))
        self.assertEqual(result.targets[0].estimated_cost, Decimal("7.00"))
        self.assertEqual(result.estimated_cost, Decimal("7.00"))

    def test_nonzero_current_position_requires_explicit_cost_split(self):
        with self.assertRaisesRegex(
            ValueError,
            "requires explicit turnover/holding cost split",
        ):
            self.candidate(current_quantity="1")

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
        policy = self.policy(
            require_adverse_stress_evidence=True,
            require_fresh_stress_evidence=False,
        )
        result = allocate_targets([self.candidate()], policy)
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertIn("no stress scenarios were supplied", result.reason)

    def test_required_stress_must_be_adverse_for_requested_direction(self):
        policy = self.policy(
            require_adverse_stress_evidence=True,
            require_fresh_stress_evidence=False,
        )
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

    def test_fresh_stress_evidence_admits_exposure_at_decision_time(self):
        policy = self.policy(require_adverse_stress_evidence=True)
        evidence = StressScenarioEvidence.create(
            name="gap_down",
            shocks={"AAA": "-0.20"},
            observed_at="2026-09-24T18:00:00Z",
            valid_until="2026-09-24T19:00:00Z",
            source_ref="risk-snapshot:abc123",
        )
        result = allocate_targets(
            [self.candidate()],
            policy,
            stress_evidence=(evidence,),
            decision_time="2026-09-24T18:30:00Z",
        )
        self.assertEqual(result.status, "ALLOCATED")
        self.assertGreater(result.worst_stress_loss, Decimal("0"))

    def test_raw_stress_numbers_do_not_satisfy_fresh_evidence_gate(self):
        policy = self.policy(require_adverse_stress_evidence=True)
        result = allocate_targets(
            [self.candidate()],
            policy,
            stress_scenarios={"gap_down": {"AAA": "-0.20"}},
            decision_time="2026-09-24T18:30:00Z",
        )
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertIn("fresh stress evidence", result.reason)

    def test_expired_or_future_stress_evidence_fails_closed(self):
        policy = self.policy(require_adverse_stress_evidence=True)
        expired = StressScenarioEvidence.create(
            name="expired",
            shocks={"AAA": "-0.20"},
            observed_at="2026-09-24T17:00:00Z",
            valid_until="2026-09-24T18:00:00Z",
            source_ref="risk-snapshot:expired",
        )
        expired_result = allocate_targets(
            [self.candidate()],
            policy,
            stress_evidence=(expired,),
            decision_time="2026-09-24T18:30:00Z",
        )
        self.assertEqual(expired_result.status, "NO_INCREASE_FALLBACK")
        self.assertIn("expired", expired_result.reason)

        future = StressScenarioEvidence.create(
            name="future",
            shocks={"AAA": "-0.20"},
            observed_at="2026-09-24T19:00:00Z",
            valid_until="2026-09-24T20:00:00Z",
            source_ref="risk-snapshot:future",
        )
        future_result = allocate_targets(
            [self.candidate()],
            policy,
            stress_evidence=(future,),
            decision_time="2026-09-24T18:30:00Z",
        )
        self.assertEqual(future_result.status, "NO_INCREASE_FALLBACK")
        self.assertIn("not observable", future_result.reason)

    def test_fresh_stress_evidence_requires_explicit_decision_time(self):
        policy = self.policy(require_adverse_stress_evidence=True)
        evidence = StressScenarioEvidence.create(
            name="gap_down",
            shocks={"AAA": "-0.20"},
            observed_at="2026-09-24T18:00:00Z",
            valid_until="2026-09-24T19:00:00Z",
            source_ref="risk-snapshot:abc123",
        )
        result = allocate_targets(
            [self.candidate()],
            policy,
            stress_evidence=(evidence,),
        )
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertIn("decision_time", result.reason)

    def test_stress_evidence_rejects_binary_float_and_invalid_validity(self):
        with self.assertRaises(TypeError):
            StressScenarioEvidence.create(
                name="float",
                shocks={"AAA": -0.20},
                observed_at="2026-09-24T18:00:00Z",
                valid_until="2026-09-24T19:00:00Z",
                source_ref="risk-snapshot:float",
            )
        with self.assertRaisesRegex(ValueError, "must not precede"):
            StressScenarioEvidence.create(
                name="time",
                shocks={"AAA": "-0.20"},
                observed_at="2026-09-24T19:00:00Z",
                valid_until="2026-09-24T18:00:00Z",
                source_ref="risk-snapshot:time",
            )

    def test_objective_selection_preserves_fresh_stress_provenance(self):
        policy = self.policy(
            cash_available="2000",
            max_gross_notional="2000",
            max_net_notional="2000",
            max_symbol_notional="2000",
            require_adverse_stress_evidence=True,
        )
        evidence = StressScenarioEvidence.create(
            name="joint_down",
            shocks={"AAA": "-0.10", "BBB": "-0.10"},
            observed_at="2026-09-24T18:00:00Z",
            valid_until="2026-09-24T19:00:00Z",
            source_ref="risk-snapshot:joint",
        )
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="AAA",
                    desired_notional="500",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.10",
                ),
                ObjectiveCandidate.create(
                    symbol="BBB",
                    desired_notional="500",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.09",
                ),
            ],
            policy,
            stress_evidence=(evidence,),
            decision_time="2026-09-24T18:30:00Z",
        )
        self.assertEqual(result.allocation.status, "ALLOCATED")
        self.assertGreater(result.expected_net_utility, Decimal("0"))

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


    def test_minimum_cash_reserve_is_never_allocated(self):
        result = allocate_targets(
            [self.candidate(desired="1000", price="10", lot="1")],
            self.policy(
                cash_available="1000",
                max_gross_notional="2000",
                max_net_notional="2000",
                max_symbol_notional="2000",
                minimum_cash_reserve="250",
            ),
        )
        self.assertEqual(result.status, "ALLOCATED")
        self.assertLessEqual(result.cash_required, Decimal("750"))
        self.assertGreaterEqual(
            Decimal("1000") - result.cash_required,
            Decimal("250"),
        )

    def test_impossible_cash_reserve_fails_closed(self):
        result = allocate_targets(
            [self.candidate(desired="100", price="10", lot="1")],
            self.policy(
                cash_available="100",
                minimum_cash_reserve="101",
            ),
        )
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(result.gross_notional, Decimal("0"))
        self.assertIn("reserve exceeds", result.reason)

    def test_objective_selection_prefers_higher_expected_net_utility(self):
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="HIGH",
                    desired_notional="1000",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.10",
                ),
                ObjectiveCandidate.create(
                    symbol="LOW",
                    desired_notional="1000",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.02",
                ),
            ],
            self.policy(
                cash_available="1000",
                max_gross_notional="1000",
                max_net_notional="1000",
                max_symbol_notional="1000",
            ),
        )
        self.assertEqual(result.allocation.status, "ALLOCATED")
        self.assertEqual(result.selected_symbols, ("HIGH",))
        self.assertEqual(result.expected_net_utility, Decimal("100"))
        self.assertEqual(result.allocation.targets[0].symbol, "HIGH")

    def test_objective_subtracts_actual_estimated_cost_once(self):
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="EXPENSIVE",
                    desired_notional="1000",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.01",
                    fee_floor="20",
                )
            ],
            self.policy(
                cash_available="2000",
                max_gross_notional="2000",
                max_net_notional="2000",
                max_symbol_notional="2000",
                max_total_cost="100",
            ),
        )
        self.assertEqual(result.allocation.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(result.expected_net_utility, Decimal("0"))
        self.assertEqual(result.selected_symbols, ())

    def test_objective_risk_penalty_can_make_candidate_ineligible(self):
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="RISKY",
                    desired_notional="500",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.03",
                    risk_penalty_rate="0.04",
                )
            ],
            self.policy(),
        )
        self.assertEqual(result.allocation.status, "NO_INCREASE_FALLBACK")
        self.assertIn("risk penalty", result.reason)

    def test_objective_search_budget_fails_closed_instead_of_truncating(self):
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="AAA",
                    desired_notional="100",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.02",
                ),
                ObjectiveCandidate.create(
                    symbol="BBB",
                    desired_notional="100",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.01",
                ),
            ],
            self.policy(),
            max_candidate_sets=1,
        )
        self.assertEqual(result.allocation.status, "NO_INCREASE_FALLBACK")
        self.assertIn("budget exceeded", result.reason)

    def test_objective_inputs_reject_binary_float(self):
        with self.assertRaises(TypeError):
            ObjectiveCandidate.create(
                symbol="AAA",
                desired_notional="100",
                price="10",
                lot_size="1",
                expected_return_rate=0.01,
            )

    def test_objective_selection_preserves_stress_constraints(self):
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="AAA",
                    desired_notional="1000",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.10",
                ),
                ObjectiveCandidate.create(
                    symbol="BBB",
                    desired_notional="1000",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.09",
                ),
            ],
            self.policy(
                cash_available="5000",
                max_gross_notional="5000",
                max_net_notional="5000",
                max_symbol_notional="5000",
                max_stress_loss="50",
                require_adverse_stress_evidence=True,
                require_fresh_stress_evidence=False,
            ),
            stress_scenarios={
                "joint_down": {
                    "AAA": "-0.10",
                    "BBB": "-0.10",
                }
            },
        )
        self.assertEqual(result.allocation.status, "ALLOCATED")
        self.assertLessEqual(result.allocation.worst_stress_loss, Decimal("50"))
        self.assertGreater(result.expected_net_utility, Decimal("0"))

    def test_objective_candidate_respects_liquidity_capacity(self):
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="AAA",
                    desired_notional="1000",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.10",
                    max_executable_notional="120",
                )
            ],
            self.policy(),
        )
        self.assertEqual(result.allocation.status, "ALLOCATED")
        self.assertEqual(result.allocation.targets[0].notional, Decimal("120"))


    def test_objective_selection_can_skip_expensive_higher_ranked_candidate(self):
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="EXPENSIVE_HIGH",
                    desired_notional="1000",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.10",
                    fee_floor="200",
                ),
                ObjectiveCandidate.create(
                    symbol="CHEAP_LOWER",
                    desired_notional="1000",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.05",
                ),
            ],
            self.policy(
                cash_available="3000",
                max_gross_notional="3000",
                max_net_notional="3000",
                max_symbol_notional="3000",
                max_total_cost="300",
            ),
        )
        self.assertEqual(result.allocation.status, "ALLOCATED")
        self.assertEqual(result.selected_symbols, ("CHEAP_LOWER",))
        self.assertEqual(result.expected_net_utility, Decimal("50"))
        self.assertEqual(
            tuple(target.symbol for target in result.allocation.targets),
            ("CHEAP_LOWER",),
        )

    def test_objective_budget_covers_complete_subset_space(self):
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="AAA",
                    desired_notional="100",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.02",
                ),
                ObjectiveCandidate.create(
                    symbol="BBB",
                    desired_notional="100",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.01",
                ),
            ],
            self.policy(),
            max_candidate_sets=2,
        )
        self.assertEqual(result.allocation.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(result.selected_symbols, ())
        self.assertIn("complete subset evaluation", result.reason)

    def test_selected_symbols_exclude_targets_that_round_to_zero(self):
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="AAA_ZERO",
                    desired_notional="0.5",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.20",
                ),
                ObjectiveCandidate.create(
                    symbol="ZZZ_ACTIVE",
                    desired_notional="100",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.05",
                ),
            ],
            self.policy(
                cash_available="1000",
                max_gross_notional="1000",
                max_net_notional="1000",
                max_symbol_notional="1000",
                max_total_cost="100",
            ),
        )
        self.assertEqual(result.allocation.status, "ALLOCATED")
        self.assertEqual(result.selected_symbols, ("ZZZ_ACTIVE",))
        nonzero = tuple(
            target.symbol
            for target in result.allocation.targets
            if target.notional != 0
        )
        self.assertEqual(nonzero, ("ZZZ_ACTIVE",))

    def test_objective_equal_utility_prefers_lower_cost_subset(self):
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="COSTLY",
                    desired_notional="1000",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.01",
                    fee_floor="10",
                ),
                ObjectiveCandidate.create(
                    symbol="CHEAP",
                    desired_notional="1000",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.05",
                ),
            ],
            self.policy(
                cash_available="3000",
                max_gross_notional="3000",
                max_net_notional="3000",
                max_symbol_notional="3000",
                max_total_cost="100",
            ),
        )
        self.assertEqual(result.allocation.status, "ALLOCATED")
        self.assertEqual(result.selected_symbols, ("CHEAP",))
        self.assertEqual(result.expected_net_utility, Decimal("50"))
        self.assertEqual(result.objective_version, "deterministic-net-utility-v2")

if __name__ == "__main__":
    unittest.main()
