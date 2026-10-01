from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
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

    def test_empty_portfolio_fallback_has_known_zero_stress_loss(self):
        result = allocate_targets([], self.policy())
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(result.gross_notional, Decimal("0"))
        self.assertEqual(result.worst_stress_loss, Decimal("0"))

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

    def test_zero_scale_preserves_reconciled_odd_lot_below_new_order_minimum(self):
        result = allocate_targets(
            [
                self.candidate(
                    desired="100",
                    price="10",
                    lot="1",
                    min_notional="10",
                    current_quantity="0.5",
                    turnover_cost_rate="0",
                    holding_cost_rate="0",
                )
            ],
            self.policy(
                cash_available="1000",
                max_gross_notional="5",
                max_net_notional="5",
                max_symbol_notional="5",
                max_turnover_notional="0",
            ),
        )
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(result.targets[0].quantity, Decimal("0.5"))
        self.assertEqual(result.targets[0].notional, Decimal("5"))
        self.assertEqual(result.turnover_notional, Decimal("0"))

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

    def test_no_increase_fallback_reports_preserved_capital_and_holding_cost(self):
        result = allocate_targets(
            [
                self.candidate(
                    desired="500",
                    price="10",
                    current_quantity="20",
                    cost="0.02",
                    capital_requirement="0.5",
                    turnover_cost_rate="0",
                    holding_cost_rate="0.02",
                )
            ],
            self.policy(
                cash_available="2000",
                max_turnover_notional="0",
            ),
            stress_scenarios={"down": {"AAA": "-0.25"}},
        )
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(result.targets[0].quantity, Decimal("20"))
        self.assertEqual(result.targets[0].notional, Decimal("200"))
        self.assertEqual(result.targets[0].turnover_notional, Decimal("0"))
        self.assertEqual(result.targets[0].estimated_cost, Decimal("4.00"))
        self.assertEqual(result.estimated_cost, Decimal("4.00"))
        self.assertEqual(result.cash_required, Decimal("104.00"))
        self.assertEqual(result.worst_stress_loss, Decimal("50.00"))

    def test_incomplete_stress_coverage_preserves_current_portfolio_without_keyerror(self):
        result = allocate_targets(
            [
                self.candidate(
                    "AAA",
                    desired="500",
                    price="10",
                    current_quantity="20",
                    turnover_cost_rate="0",
                    holding_cost_rate="0",
                ),
                self.candidate(
                    "BBB",
                    desired="500",
                    price="10",
                    current_quantity="10",
                    turnover_cost_rate="0",
                    holding_cost_rate="0",
                ),
            ],
            self.policy(
                cash_available="5000",
                max_gross_notional="5000",
                max_net_notional="5000",
                max_symbol_notional="5000",
            ),
            stress_scenarios={"partial": {"AAA": "-0.20"}},
        )

        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertIn("stress evidence is missing", result.reason)
        targets = {target.symbol: target for target in result.targets}
        self.assertEqual(targets["AAA"].quantity, Decimal("20"))
        self.assertEqual(targets["BBB"].quantity, Decimal("10"))
        self.assertEqual(result.turnover_notional, Decimal("0"))
        self.assertIsNone(result.worst_stress_loss)

    def test_missing_stress_evidence_never_reports_zero_for_preserved_exposure(self):
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
        self.assertEqual(result.targets[0].notional, Decimal("200"))
        self.assertIsNone(result.worst_stress_loss)

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
        result = allocate_targets(
            [
                self.candidate("AAA", desired="500", price="10"),
                self.candidate("BBB", desired="500", price="10"),
            ],
            self.policy(cash_available="2000", max_gross_notional="2000"),
            stress_scenarios={"partial": {"AAA": "-0.20"}},
        )
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(result.gross_notional, Decimal("0"))
        self.assertEqual(result.turnover_notional, Decimal("0"))
        self.assertEqual(result.worst_stress_loss, Decimal("0"))
        self.assertIn("missing explicit shocks", result.reason)

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

    def test_liquidity_cap_rounding_cannot_overshoot_sell_turnover(self):
        result = allocate_targets(
            [
                self.candidate(
                    desired="0",
                    price="10",
                    lot="1",
                    current_quantity="10",
                    max_executable_notional="15",
                    turnover_cost_rate="0",
                    holding_cost_rate="0",
                )
            ],
            self.policy(
                cash_available="1000",
                max_gross_notional="1000",
                max_net_notional="1000",
                max_symbol_notional="1000",
            ),
        )
        self.assertEqual(result.status, "ALLOCATED")
        self.assertEqual(result.targets[0].quantity, Decimal("9"))
        self.assertEqual(result.targets[0].notional, Decimal("90"))
        self.assertEqual(result.targets[0].turnover_notional, Decimal("10"))
        self.assertLessEqual(
            result.targets[0].turnover_notional,
            Decimal("15"),
        )

    def test_bounded_search_probes_zero_crossing_before_discarding_feasible_interval(self):
        result = allocate_targets(
            [
                self.candidate(
                    desired="-100",
                    price="10",
                    lot="1",
                    current_quantity="100",
                    turnover_cost_rate="0",
                    holding_cost_rate="0",
                )
            ],
            self.policy(
                cash_available="5000",
                max_gross_notional="50",
                max_net_notional="50",
                max_symbol_notional="50",
                max_turnover_notional="2000",
            ),
        )

        self.assertEqual(result.status, "ALLOCATED")
        self.assertGreater(result.turnover_notional, Decimal("0"))
        self.assertLessEqual(result.gross_notional, Decimal("50"))
        self.assertLessEqual(abs(result.targets[0].notional), Decimal("50"))
        self.assertLess(result.targets[0].quantity, Decimal("0"))

    def test_bounded_search_probes_portfolio_net_zero_interior(self):
        result = allocate_targets(
            [
                self.candidate(
                    symbol="AAA",
                    desired="-1000",
                    price="10",
                    current_quantity="100",
                    turnover_cost_rate="0",
                    holding_cost_rate="0",
                ),
                self.candidate(
                    symbol="BBB",
                    desired="-900",
                    price="10",
                    current_quantity="-20",
                    turnover_cost_rate="0",
                    holding_cost_rate="0",
                ),
            ],
            self.policy(
                cash_available="5000",
                max_gross_notional="5000",
                max_net_notional="50",
                max_symbol_notional="5000",
                max_turnover_notional="5000",
            ),
        )

        # Continuous portfolio net is 800 - 2700*scale, whose zero is 8/27.
        # Scale 0, 0.25, 0.5 and 1 are all net-infeasible after lot rounding,
        # but the interior around 8/27 is feasible (roughly +410 / -400).
        self.assertEqual(result.status, "ALLOCATED")
        self.assertGreater(result.scale, Decimal("0.25"))
        self.assertLess(result.scale, Decimal("0.5"))
        self.assertLessEqual(result.net_notional, Decimal("50"))
        self.assertGreater(result.turnover_notional, Decimal("0"))
        by_symbol = {target.symbol: target for target in result.targets}
        self.assertGreater(by_symbol["AAA"].notional, Decimal("0"))
        self.assertLess(by_symbol["BBB"].notional, Decimal("0"))

    def test_order_lot_rounding_preserves_odd_lot_current_position(self):
        result = allocate_targets(
            [
                self.candidate(
                    desired="25",
                    price="10",
                    lot="1",
                    current_quantity="0.5",
                    turnover_cost_rate="0",
                    holding_cost_rate="0",
                )
            ],
            self.policy(cash_available="1000"),
        )
        self.assertEqual(result.status, "ALLOCATED")
        self.assertEqual(result.targets[0].quantity, Decimal("2.5"))
        self.assertEqual(result.targets[0].notional, Decimal("25"))
        self.assertEqual(result.targets[0].turnover_notional, Decimal("20"))

    def test_subminimum_order_preserves_existing_position_instead_of_liquidating(self):
        result = allocate_targets(
            [
                self.candidate(
                    desired="90",
                    price="10",
                    lot="1",
                    min_notional="20",
                    current_quantity="10",
                    turnover_cost_rate="0",
                    holding_cost_rate="0",
                )
            ],
            self.policy(cash_available="1000"),
        )
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(result.targets[0].quantity, Decimal("10"))
        self.assertEqual(result.targets[0].notional, Decimal("100"))
        self.assertEqual(result.targets[0].turnover_notional, Decimal("0"))

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

    def test_objective_fee_floor_must_beat_qualified_no_trade_baseline(self):
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="HELD",
                    desired_notional="101",
                    price="1",
                    lot_size="1",
                    expected_return_rate="0.10",
                    fee_floor="1",
                    current_quantity="100",
                    turnover_cost_rate="0",
                    holding_cost_rate="0",
                )
            ],
            self.policy(),
        )
        self.assertEqual(result.allocation.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(result.allocation.targets[0].notional, Decimal("100"))
        self.assertEqual(result.allocation.turnover_notional, Decimal("0"))
        self.assertEqual(result.expected_net_utility, Decimal("10.00"))
        self.assertEqual(result.selected_symbols, ())
        self.assertIn("no-trade portfolio baseline", result.reason)

    def test_objective_equal_utility_prefers_no_trade(self):
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="HELD",
                    desired_notional="101",
                    price="1",
                    lot_size="1",
                    expected_return_rate="0.10",
                    fee_floor="0.10",
                    current_quantity="100",
                    turnover_cost_rate="0",
                    holding_cost_rate="0",
                )
            ],
            self.policy(),
        )
        self.assertEqual(result.allocation.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(result.allocation.targets[0].notional, Decimal("100"))
        self.assertEqual(result.expected_net_utility, Decimal("10.00"))
        self.assertIn("strictly improved", result.reason)

    def test_objective_genuine_utility_improvement_executes(self):
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="HELD",
                    desired_notional="101",
                    price="1",
                    lot_size="1",
                    expected_return_rate="0.10",
                    fee_floor="0.05",
                    current_quantity="100",
                    turnover_cost_rate="0",
                    holding_cost_rate="0",
                )
            ],
            self.policy(),
        )
        self.assertEqual(result.allocation.status, "ALLOCATED")
        self.assertEqual(result.allocation.targets[0].notional, Decimal("101"))
        self.assertEqual(result.allocation.turnover_notional, Decimal("1"))
        self.assertEqual(result.expected_net_utility, Decimal("10.05"))
        self.assertEqual(result.selected_symbols, ("HELD",))
        self.assertEqual(result.objective_version, "deterministic-net-utility-v5")

    def test_no_trade_baseline_uses_holding_stress_capital_and_cash_economics(self):
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="HELD",
                    desired_notional="101",
                    price="1",
                    lot_size="1",
                    expected_return_rate="0.10",
                    fee_floor="10",
                    capital_requirement_rate="0.5",
                    current_quantity="100",
                    turnover_cost_rate="0",
                    holding_cost_rate="0.02",
                )
            ],
            self.policy(
                cash_available="60",
                max_gross_notional="1000",
                max_net_notional="1000",
                max_symbol_notional="1000",
                max_total_cost="100",
                max_stress_loss="100",
                stress_loss_penalty_rate="0.10",
            ),
            stress_scenarios={"down": {"HELD": "-0.20"}},
        )
        self.assertEqual(result.allocation.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(result.allocation.targets[0].notional, Decimal("100"))
        self.assertEqual(result.allocation.estimated_cost, Decimal("2.00"))
        self.assertEqual(result.allocation.worst_stress_loss, Decimal("20.00"))
        self.assertEqual(result.allocation.cash_required, Decimal("52.00"))
        self.assertEqual(result.expected_net_utility, Decimal("6.0000"))
        self.assertIn("no-trade portfolio baseline", result.reason)

    def test_hard_infeasible_current_portfolio_can_still_de_risk(self):
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="OVER_LIMIT",
                    desired_notional="50",
                    price="1",
                    lot_size="1",
                    expected_return_rate="0.10",
                    current_quantity="100",
                    turnover_cost_rate="0",
                    holding_cost_rate="0",
                )
            ],
            self.policy(
                cash_available="1000",
                max_gross_notional="80",
                max_net_notional="80",
                max_symbol_notional="80",
                max_turnover_notional="100",
            ),
        )
        self.assertEqual(result.allocation.status, "ALLOCATED")
        self.assertEqual(result.allocation.targets[0].notional, Decimal("50"))
        self.assertEqual(result.allocation.turnover_notional, Decimal("50"))
        self.assertEqual(result.expected_net_utility, Decimal("5.00"))
        self.assertEqual(result.selected_symbols, ("OVER_LIMIT",))

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

    def test_objective_subset_cannot_hide_existing_unselected_portfolio_risk(self):
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="HELD",
                    desired_notional="0",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0",
                    current_quantity="80",
                    turnover_cost_rate="0",
                    holding_cost_rate="0",
                ),
                ObjectiveCandidate.create(
                    symbol="NEW",
                    desired_notional="1000",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.10",
                ),
            ],
            self.policy(
                cash_available="1000",
                max_gross_notional="1000",
                max_net_notional="1000",
                max_symbol_notional="1000",
                max_turnover_notional="1000",
                require_adverse_stress_evidence=False,
            ),
            stress_scenarios={
                "joint_down": {
                    "HELD": "-0.10",
                    "NEW": "-0.10",
                }
            },
        )
        self.assertEqual(result.allocation.status, "ALLOCATED")
        by_symbol = {
            target.symbol: target
            for target in result.allocation.targets
        }
        self.assertEqual(by_symbol["HELD"].quantity, Decimal("80"))
        self.assertEqual(by_symbol["HELD"].notional, Decimal("800"))
        self.assertLessEqual(by_symbol["NEW"].notional, Decimal("200"))
        self.assertLessEqual(result.allocation.gross_notional, Decimal("1000"))
        self.assertLessEqual(result.allocation.cash_required, Decimal("1000"))
        self.assertGreaterEqual(
            result.allocation.worst_stress_loss,
            Decimal("80"),
        )
        self.assertEqual(result.selected_symbols, ("NEW",))

    def test_objective_fallback_preserves_current_stress_economics(self):
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="RISKY",
                    desired_notional="500",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.03",
                    risk_penalty_rate="0.04",
                    current_quantity="20",
                    cost_rate="0.02",
                    capital_requirement_rate="0.5",
                    turnover_cost_rate="0",
                    holding_cost_rate="0.02",
                )
            ],
            self.policy(
                cash_available="2000",
                max_gross_notional="2000",
                max_net_notional="2000",
                max_symbol_notional="2000",
            ),
            stress_scenarios={"down": {"RISKY": "-0.25"}},
        )
        self.assertEqual(result.allocation.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(result.allocation.targets[0].notional, Decimal("200"))
        self.assertEqual(result.allocation.targets[0].estimated_cost, Decimal("4.00"))
        self.assertEqual(result.allocation.cash_required, Decimal("104.00"))
        self.assertEqual(result.allocation.worst_stress_loss, Decimal("50.00"))
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

    def test_objective_stress_penalty_prefers_lower_tail_loss_subset(self):
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="LOW_STRESS",
                    desired_notional="1000",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.10",
                ),
                ObjectiveCandidate.create(
                    symbol="HIGH_STRESS",
                    desired_notional="1000",
                    price="10",
                    lot_size="1",
                    expected_return_rate="0.10",
                ),
            ],
            self.policy(
                cash_available="3000",
                max_gross_notional="3000",
                max_net_notional="3000",
                max_symbol_notional="3000",
                max_stress_loss="1000",
                stress_loss_penalty_rate="1",
                require_adverse_stress_evidence=False,
            ),
            stress_scenarios={
                "joint_down": {
                    "LOW_STRESS": "-0.01",
                    "HIGH_STRESS": "-0.20",
                }
            },
        )
        self.assertEqual(result.allocation.status, "ALLOCATED")
        self.assertEqual(result.selected_symbols, ("LOW_STRESS",))
        self.assertEqual(result.allocation.worst_stress_loss, Decimal("10"))
        self.assertEqual(result.expected_net_utility, Decimal("90"))
        self.assertEqual(result.objective_version, "deterministic-net-utility-v5")

    def test_stress_loss_penalty_rejects_binary_float(self):
        with self.assertRaises(TypeError):
            self.policy(stress_loss_penalty_rate=0.5)

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
            ("EXPENSIVE_HIGH", "CHEAP_LOWER"),
        )
        targets = {
            target.symbol: target
            for target in result.allocation.targets
        }
        self.assertEqual(targets["EXPENSIVE_HIGH"].notional, Decimal("0"))
        self.assertEqual(targets["EXPENSIVE_HIGH"].turnover_notional, Decimal("0"))
        self.assertEqual(targets["CHEAP_LOWER"].notional, Decimal("1000"))

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
        self.assertEqual(result.objective_version, "deterministic-net-utility-v5")


    def test_split_holding_cost_cannot_satisfy_execution_fee_floor(self):
        candidate = self.candidate(
            desired="1010",
            price="10",
            lot="1",
            cost="0.021",
            capital_requirement="0.001",
            fee_floor="5",
            current_quantity="100",
            turnover_cost_rate="0.001",
            holding_cost_rate="0.02",
        )
        accepted = allocate_targets(
            [candidate],
            self.policy(
                cash_available="1000",
                max_gross_notional="2000",
                max_net_notional="2000",
                max_symbol_notional="2000",
                max_total_cost="26",
            ),
        )
        self.assertEqual(accepted.status, "ALLOCATED")
        self.assertEqual(accepted.targets[0].turnover_notional, Decimal("10"))
        self.assertEqual(accepted.targets[0].estimated_cost, Decimal("25.20"))

        blocked = allocate_targets(
            [candidate],
            self.policy(
                cash_available="1000",
                max_gross_notional="2000",
                max_net_notional="2000",
                max_symbol_notional="2000",
                max_total_cost="24",
            ),
        )
        self.assertEqual(blocked.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(blocked.targets[0].estimated_cost, Decimal("20.00"))

    def test_objective_partial_reversal_never_credits_opposite_retained_exposure(self):
        for current, desired in (("100", "-100"), ("-100", "100")):
            result = allocate_objective_targets(
                [
                    ObjectiveCandidate.create(
                        symbol="REV",
                        desired_notional=desired,
                        price="1",
                        lot_size="1",
                        expected_return_rate="0.10",
                        current_quantity=current,
                        max_executable_notional="20",
                        turnover_cost_rate="0",
                        holding_cost_rate="0",
                    )
                ],
                self.policy(
                    cash_available="1000",
                    max_gross_notional="1000",
                    max_net_notional="1000",
                    max_symbol_notional="1000",
                    max_total_cost="100",
                ),
            )
            self.assertEqual(result.allocation.status, "NO_INCREASE_FALLBACK")
            self.assertEqual(result.selected_symbols, ())
            self.assertEqual(result.expected_net_utility, Decimal("-10.00"))
            self.assertEqual(
                result.allocation.targets[0].notional,
                Decimal(current),
            )

    def test_objective_reversal_zero_crossing_uses_resulting_direction(self):
        policy = self.policy(
            cash_available="1000",
            max_gross_notional="1000",
            max_net_notional="1000",
            max_symbol_notional="1000",
            max_total_cost="100",
        )
        def reversal(cap):
            return allocate_objective_targets(
                [
                    ObjectiveCandidate.create(
                        symbol="REV",
                        desired_notional="-100",
                        price="1",
                        lot_size="1",
                        expected_return_rate="0.10",
                        current_quantity="100",
                        max_executable_notional=cap,
                        turnover_cost_rate="0",
                        holding_cost_rate="0",
                    )
                ],
                policy,
            )

        flat = reversal("100")
        self.assertEqual(flat.allocation.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(flat.allocation.targets[0].notional, Decimal("100"))
        self.assertEqual(flat.expected_net_utility, Decimal("-10.00"))
        self.assertEqual(flat.selected_symbols, ())

        crossed = reversal("120")
        self.assertEqual(crossed.allocation.status, "ALLOCATED")
        self.assertEqual(crossed.allocation.targets[0].notional, Decimal("-20"))
        self.assertEqual(crossed.expected_net_utility, Decimal("2.00"))
        self.assertEqual(crossed.selected_symbols, ("REV",))

    def test_objective_utility_depends_on_portfolio_economics_not_subset_labels(self):
        result = allocate_objective_targets(
            [
                ObjectiveCandidate.create(
                    symbol="AAA",
                    desired_notional="10",
                    price="1",
                    lot_size="1",
                    expected_return_rate="0.10",
                ),
                ObjectiveCandidate.create(
                    symbol="BBB",
                    desired_notional="10",
                    price="1",
                    lot_size="1",
                    expected_return_rate="0.05",
                    current_quantity="10",
                    turnover_cost_rate="0",
                    holding_cost_rate="0",
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
        self.assertEqual(result.selected_symbols, ("AAA",))
        self.assertEqual(result.expected_net_utility, Decimal("1.50"))
        targets = {item.symbol: item.notional for item in result.allocation.targets}
        self.assertEqual(targets, {"AAA": Decimal("10"), "BBB": Decimal("10")})


    def test_financial_authority_is_independent_of_ambient_decimal_context(self):
        candidate = ObjectiveCandidate.create(
            symbol="EXACT",
            desired_notional="100000000000000000000.02",
            price="1",
            lot_size="0.01",
            expected_return_rate="0.000000000000000001",
            current_quantity="100000000000000000000.01",
            turnover_cost_rate="0",
            holding_cost_rate="0",
            capital_requirement_rate="0.000000000000000001",
        )
        policy = self.policy(
            cash_available="1000",
            max_gross_notional="200000000000000000000",
            max_net_notional="200000000000000000000",
            max_symbol_notional="200000000000000000000",
            max_total_cost="1000",
            max_turnover_notional="1",
        )

        def snapshot():
            result = allocate_objective_targets([candidate], policy)
            target = result.allocation.targets[0]
            return (
                result.allocation.status,
                target.quantity,
                target.notional,
                target.turnover_notional,
                result.allocation.gross_notional,
                result.allocation.net_notional,
                result.allocation.estimated_cost,
                result.allocation.cash_required,
                result.selected_symbols,
                result.expected_net_utility,
            )

        baseline = snapshot()
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    self.assertEqual(snapshot(), baseline)

        self.assertEqual(baseline[0], "ALLOCATED")
        self.assertEqual(baseline[2], Decimal("100000000000000000000.02"))
        self.assertEqual(baseline[3], Decimal("0.01"))

    def test_exact_arithmetic_resource_overflow_uses_allocation_error_contract(self):
        candidate = self.candidate(
            desired="1E+255",
            price="100",
            current_quantity="1E+255",
            turnover_cost_rate="0",
            holding_cost_rate="0",
        )
        policy = self.policy(
            cash_available="1E+255",
            max_gross_notional="1E+255",
            max_net_notional="1E+255",
            max_symbol_notional="1E+255",
            max_total_cost="1E+255",
        )
        with self.assertRaisesRegex(
            ValueError,
            "allocation exact arithmetic exceeds resource envelope",
        ):
            allocate_targets([candidate], policy)

    def test_numeric_ingress_rejects_resource_overflow_and_polymorphic_values(self):
        class HostileMoney(Decimal):
            def is_finite(self):
                raise AssertionError("caller numeric callback executed")

        for value in ("1e999999999", "9" * 257, Decimal("1e-257")):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                self.candidate(desired=value)
        with self.assertRaises(TypeError):
            self.candidate(desired=HostileMoney("1"))

    def test_split_cost_contract_is_independent_of_ambient_decimal_context(self):
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    candidate = self.candidate(
                        cost="0.0200000001",
                        turnover_cost_rate="0.0000000001",
                        holding_cost_rate="0.02",
                    )
                    self.assertEqual(
                        candidate.cost_rate,
                        Decimal("0.0200000001"),
                    )

    def test_objective_subset_ranking_is_independent_of_ambient_decimal_context(self):
        retained = ObjectiveCandidate.create(
            symbol="HELD",
            desired_notional="100000000000000000000.01",
            price="1",
            lot_size="0.01",
            expected_return_rate="0",
            current_quantity="100000000000000000000.01",
            turnover_cost_rate="0",
            holding_cost_rate="0",
            capital_requirement_rate="0.000000000000000001",
        )
        low = ObjectiveCandidate.create(
            symbol="AAA",
            desired_notional="0.01",
            price="1",
            lot_size="0.01",
            expected_return_rate="0.0000001",
            capital_requirement_rate="0.001",
        )
        high = ObjectiveCandidate.create(
            symbol="ZZZ",
            desired_notional="0.01",
            price="1",
            lot_size="0.01",
            expected_return_rate="0.0000001000000001",
            capital_requirement_rate="0.001",
        )
        policy = self.policy(
            cash_available="1000",
            max_gross_notional="100000000000000000001",
            max_net_notional="100000000000000000001",
            max_symbol_notional="100000000000000000001",
            max_total_cost="1000",
            max_turnover_notional="0.01",
        )

        def snapshot():
            result = allocate_objective_targets(
                [retained, low, high],
                policy,
            )
            return (
                result.allocation.status,
                result.selected_symbols,
                result.expected_net_utility,
                tuple(
                    (target.symbol, target.notional)
                    for target in result.allocation.targets
                ),
                result.allocation.cash_required,
            )

        baseline = snapshot()
        self.assertEqual(baseline[0], "ALLOCATED")
        self.assertEqual(baseline[1], ("ZZZ",))
        self.assertEqual(
            baseline[2],
            Decimal("0.000000001000000001"),
        )
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    self.assertEqual(snapshot(), baseline)

    def test_allocation_policy_digest_binds_execution_search_budget(self):
        from mvp.autotrade_mvp.allocation import _allocation_policy_digest

        first = self.policy(max_execution_states=100)
        same = self.policy(max_execution_states=100)
        changed = self.policy(max_execution_states=101)

        self.assertEqual(
            _allocation_policy_digest(first),
            _allocation_policy_digest(same),
        )
        self.assertNotEqual(
            _allocation_policy_digest(first),
            _allocation_policy_digest(changed),
        )

    def test_objective_search_digest_binds_candidate_set_budget(self):
        from mvp.autotrade_mvp.allocation import _objective_search_config_digest

        self.assertEqual(
            _objective_search_config_digest(64),
            _objective_search_config_digest(64),
        )
        self.assertNotEqual(
            _objective_search_config_digest(64),
            _objective_search_config_digest(65),
        )
        for invalid in (0, -1, True, 2.5):
            with self.assertRaisesRegex(ValueError, "max_candidate_sets"):
                _objective_search_config_digest(invalid)

class DiscreteAllocationSearchTests(unittest.TestCase):
    policy = AllocationTests.policy
    candidate = AllocationTests.candidate
    # Reuse canonical factories; the independent oracle below does not call any
    # allocator search/evaluation helper.
    def loose_policy(self, **changes):
        values = dict(cash_available="10000", max_gross_notional="10000",
                      max_net_notional="10000", max_symbol_notional="10000",
                      max_total_cost="10000", max_stress_loss="10000",
                      max_turnover_notional="10000")
        values.update(changes)
        return self.policy(**values)

    def position(self, symbol, current, desired, **changes):
        return self.candidate(symbol, price="1", current_quantity=str(current),
                              desired=str(desired), turnover_cost_rate="0",
                              holding_cost_rate="0", **changes)

    def capped_pair(self):
        return [self.position("A", 1000, -1000, max_executable_notional="200"),
                self.position("B", 1000, -3000)]

    def test_post_liquidity_cap_net_interval_is_found(self):
        result = allocate_targets(self.capped_pair(), self.loose_policy(max_net_notional="50"))
        self.assertEqual(result.status, "ALLOCATED")
        self.assertEqual(tuple(x.notional for x in result.targets), (Decimal("800"), Decimal("-850")))
        self.assertEqual(result.net_notional, Decimal("50"))
        self.assertEqual(result.scale, Decimal("0.4625"))

    def test_post_liquidity_cap_stress_envelope_is_found(self):
        result = allocate_targets(self.capped_pair(), self.loose_policy(max_stress_loss="5"),
                                  stress_scenarios={"up": {"A": ".1", "B": ".1"},
                                                    "down": {"A": "-.1", "B": "-.1"}})
        self.assertEqual(result.status, "ALLOCATED")
        self.assertEqual(result.worst_stress_loss, Decimal("5"))
        self.assertEqual(result.scale, Decimal("0.4625"))

    def test_discrete_neutral_interval_missed_by_continuous_roots_is_found(self):
        result = allocate_targets([self.position("A", -20, -10, lot="3"),
                                   self.position("B", -20, 20, lot="2")],
                                  self.loose_policy(max_net_notional="0"))
        self.assertEqual(result.status, "ALLOCATED")
        self.assertEqual(result.scale, Decimal(".85"))
        self.assertEqual(tuple(x.quantity for x in result.targets), (Decimal("-14"), Decimal("14")))
        self.assertEqual(result.turnover_notional, Decimal("40"))

    def test_minimum_notional_activation_and_non_lot_cap(self):
        candidate = self.position("A", 0, 30, lot="3", min_notional="8",
                                  max_executable_notional="11")
        result = allocate_targets([candidate], self.loose_policy(max_gross_notional="9"))
        self.assertEqual(result.status, "ALLOCATED")
        self.assertEqual(result.targets[0].quantity, Decimal("9"))
        blocked = allocate_targets([candidate], self.loose_policy(max_gross_notional="8"))
        self.assertEqual(blocked.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(blocked.turnover_notional, Decimal("0"))

    def test_recurring_transition_scale_keeps_exact_lot_and_replays(self):
        from mvp.autotrade_mvp.allocation import _evaluate
        candidate = self.position("A", 0, 3)
        policy = self.loose_policy(max_gross_notional="1")
        result = allocate_targets([candidate], policy)
        self.assertEqual(result.targets[0].quantity, Decimal("1"))
        self.assertEqual(_evaluate([candidate], policy, {}, result.scale).targets, result.targets)
        self.assertGreaterEqual(result.scale, Decimal(".3333333333333333333333333333"))
        self.assertLess(result.scale, Decimal(".334"))

    def test_search_budget_is_explicit_and_does_not_claim_infeasibility(self):
        result = allocate_targets([self.position("A", 2, "1e30")],
                                  self.loose_policy(max_execution_states=20))
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertIn("budget exceeded; feasibility not established", result.reason)
        self.assertEqual(result.targets[0].quantity, Decimal("2"))
        self.assertIsNone(result.worst_stress_loss)

    def test_search_budget_counts_before_materializing_and_accepts_exact_bound(self):
        candidate = self.position("A", 0, 10)
        for budget, expected in [(9, "NO_INCREASE_FALLBACK"), (10, "ALLOCATED")]:
            result = allocate_targets([candidate], self.loose_policy(max_gross_notional="9",
                                                                    max_execution_states=budget))
            self.assertEqual(result.status, expected)
        for invalid in (0, -1, True, 2.5):
            with self.assertRaisesRegex(ValueError, "max_execution_states"):
                self.loose_policy(max_execution_states=invalid)

    def test_objective_does_not_treat_unsearched_subset_as_infeasible(self):
        candidate = ObjectiveCandidate(candidate=self.position("A", 0, 100),
                                              expected_return_rate=".1", risk_penalty_rate="0")
        result = allocate_objective_targets([candidate],
                    self.loose_policy(max_gross_notional="10", max_execution_states=5))
        self.assertEqual(result.allocation.status, "NO_INCREASE_FALLBACK")
        self.assertIn("feasibility not established", result.reason)
        self.assertEqual(result.selected_symbols, ())

    def test_complete_close_is_an_executable_allocation_not_preserved_holding(self):
        result = allocate_targets([self.position("A", 7, 0)], self.loose_policy())
        self.assertEqual(result.status, "ALLOCATED")
        self.assertEqual(result.targets[0].quantity, Decimal("0"))
        self.assertEqual(result.turnover_notional, Decimal("7"))

    def test_stress_does_not_require_invented_shock_for_zero_unchanged_symbol(self):
        result = allocate_targets([self.position("A", 0, 10), self.position("B", 0, 0)],
                                  self.loose_policy(), stress_scenarios={"down": {"A": "-.1"}})
        self.assertEqual(result.status, "ALLOCATED")
        self.assertEqual(result.worst_stress_loss, Decimal("1"))

    def test_reversal_fallback_requires_adverse_current_direction(self):
        for current, desired, favorable in [(100, -100, ".2"), (-100, 100, "-.2")]:
            candidate = self.position("A", current, desired)
            policy = self.loose_policy(max_turnover_notional="0", require_adverse_stress_evidence=True,
                                       require_fresh_stress_evidence=False)
            result = allocate_targets([candidate], policy,
                                      stress_scenarios={"desired_adverse": {"A": favorable}})
            self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
            self.assertEqual(result.targets[0].quantity, Decimal(current))
            self.assertIsNone(result.worst_stress_loss)
            qualified = allocate_targets([candidate], policy,
                stress_scenarios={"up": {"A": ".2"}, "down": {"A": "-.2"}})
            self.assertEqual(qualified.worst_stress_loss, Decimal("20"))

    def test_partially_executed_reversal_does_not_qualify_retained_direction(self):
        candidate = self.position("A", 100, -100, max_executable_notional="10")
        result = allocate_targets([candidate], self.loose_policy(require_adverse_stress_evidence=True,
                                  require_fresh_stress_evidence=False),
                                  stress_scenarios={"up": {"A": ".2"}})
        self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
        self.assertIsNone(result.worst_stress_loss)

    def test_default_fresh_stress_omits_only_zero_unchanged_symbols(self):
        evidence = (StressScenarioEvidence.create(name="down", shocks={"A": "-.2"},
            observed_at="2026-09-27T00:00:00Z", valid_until="2026-09-28T00:00:00Z",
            source_ref="scenario:adverse"),)
        policy = self.loose_policy(require_adverse_stress_evidence=True)
        for current, desired, expected in [(0, 0, "ALLOCATED"), (1, 0, "NO_INCREASE_FALLBACK"),
                                          (0, 1, "NO_INCREASE_FALLBACK")]:
            result = allocate_targets([self.position("A", 0, 10), self.position("B", current, desired)],
                policy, stress_evidence=evidence, decision_time="2026-09-27T12:00:00Z")
            self.assertEqual(result.status, expected)
            if current:
                self.assertIsNone(result.worst_stress_loss)
        result = allocate_objective_targets([
            ObjectiveCandidate(self.position("A", 0, 10), Decimal(".1")),
            ObjectiveCandidate(self.position("B", 0, 0), Decimal("0"))], policy,
            stress_evidence=evidence, decision_time="2026-09-27T12:00:00Z")
        self.assertEqual(result.allocation.status, "ALLOCATED")

    def test_bounded_integer_portfolios_match_independent_exhaustive_oracle(self):
        from fractions import Fraction
        from math import lcm
        from random import Random
        rng = Random(796)
        for case in range(120):
            current = [rng.randint(-10, 10) for _ in range(2)]
            desired = [rng.randint(-15, 15) for _ in range(2)]
            lots = [rng.randint(1, 4) for _ in range(2)]
            minima = [rng.randint(0, 5) for _ in range(2)]
            caps = [rng.randint(1, 20) for _ in range(2)]
            net_limit, gross_limit = rng.randint(0, 10), rng.randint(2, 25)
            turnover_limit = rng.randint(1, 25)
            candidates = [self.position(chr(65+i), current[i], desired[i], lot=str(lots[i]),
                                       min_notional=str(minima[i]), max_executable_notional=str(caps[i]))
                          for i in range(2)]
            # A uniform rational grid at lcm(changes) includes every integer
            # order transition; independent integer arithmetic supplies truth.
            denom = lcm(*(abs(desired[i]-current[i]) or 1 for i in range(2)))
            expected = None
            for step in range(denom, -1, -1):
                targets, turnover = [], 0
                for i in range(2):
                    change = desired[i] - current[i]
                    raw = min(Fraction(abs(change)*step, denom), caps[i])
                    delta = int(raw // lots[i])*lots[i]
                    if delta < minima[i]:
                        delta = 0
                    delta *= 1 if change >= 0 else -1
                    targets.append(current[i]+delta)
                    turnover += abs(delta)
                if (abs(sum(targets)) <= net_limit and sum(map(abs, targets)) <= gross_limit
                        and turnover <= turnover_limit and (turnover > 0 or desired == current)):
                    expected = tuple(map(Decimal, targets))
                    break
            result = allocate_targets(candidates, self.loose_policy(max_net_notional=str(net_limit),
                        max_gross_notional=str(gross_limit), max_turnover_notional=str(turnover_limit)))
            with self.subTest(case=case, current=current, desired=desired):
                if expected is None:
                    self.assertEqual(result.status, "NO_INCREASE_FALLBACK")
                    self.assertEqual(tuple(x.quantity for x in result.targets), tuple(map(Decimal, current)))
                else:
                    self.assertEqual(result.status, "ALLOCATED")
                    self.assertEqual(tuple(x.quantity for x in result.targets), expected)


if __name__ == "__main__":
    unittest.main()
