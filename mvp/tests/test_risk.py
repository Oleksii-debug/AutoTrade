from decimal import Decimal
import unittest

from mvp.autotrade_mvp.risk import (
    RiskContext,
    RiskIntent,
    RiskPolicy,
    evaluate_risk,
    risk_decision_fingerprint,
)


def policy(**overrides):
    values = dict(
        max_abs_position="10",
        max_single_notional="2000",
        max_gross_leverage="2",
        max_net_leverage="2",
        max_daily_loss="500",
        max_drawdown_fraction="0.20",
        max_data_age_seconds="5",
        max_fx_age_seconds="60",
        min_margin_headroom="0.20",
        max_stress_loss="500",
        max_asset_concentration_fraction=None,
        max_venue_concentration_fraction=None,
        max_order_participation_fraction=None,
        max_abs_factor_exposure=None,
        max_spread_fraction=None,
        max_slippage_fraction=None,
        max_clock_age_seconds=None,
        allowed_actions=None,
        require_settlement_evidence=False,
        require_option_exercise_evidence=False,
        min_futures_delivery_headroom_seconds=None,
    )
    values.update(overrides)
    return RiskPolicy.create(**values)


def context(**overrides):
    values = dict(
        state_version=7,
        equity="1000",
        positions={"ABC": "2"},
        marks={"ABC": "100", "XYZ": "50"},
        reserved_position_delta={},
        daily_pnl="-10",
        drawdown_fraction="0.05",
        market_data_age_seconds="1",
        fx_age_seconds={"USD": "10"},
        margin_headroom="0.50",
        capability_allowed=True,
        borrow_available=True,
        stress_scenarios=({"ABC": "-0.10", "XYZ": "-0.20"},),
    )
    values.update(overrides)
    return RiskContext.create(**values)


class IndependentRiskTests(unittest.TestCase):
    def test_boundary_equal_to_limits_is_admitted(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="8", price="100",
                expected_state_version=7,
            ),
            context(),
            policy(max_abs_position="10", max_single_notional="1000"),
        )
        self.assertTrue(decision.admitted)
        self.assertEqual(decision.resulting_position, Decimal("10"))

    def test_stale_state_and_data_fail_closed(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=6,
        )
        decision = evaluate_risk(
            intent,
            context(market_data_age_seconds="6", fx_age_seconds={"USD": "61"}),
            policy(),
        )
        failed = {rule.rule for rule in decision.rules if not rule.passed}
        self.assertTrue({"state_version", "market_freshness", "fx_freshness"} <= failed)
        self.assertFalse(decision.admitted)

    def test_required_fx_evidence_fails_closed_when_missing(self):
        intent = RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity="1",
            price="100",
            expected_state_version=7,
        )
        missing = evaluate_risk(
            intent,
            context(fx_age_seconds={}, fx_required=True),
            policy(),
        )
        rule = next(item for item in missing.rules if item.rule == "fx_freshness")
        self.assertFalse(rule.passed)
        self.assertEqual(rule.observed, "UNKNOWN")
        self.assertFalse(missing.admitted)

        not_required = evaluate_risk(
            intent,
            context(fx_age_seconds={}, fx_required=False),
            policy(),
        )
        self.assertTrue(
            next(item for item in not_required.rules if item.rule == "fx_freshness").passed
        )

    def test_fx_required_must_be_real_boolean(self):
        with self.assertRaises(TypeError):
            context(fx_required="yes")

    def test_reserved_exposure_counts_against_position_and_leverage(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="3", price="100",
                expected_state_version=7,
            ),
            context(reserved_position_delta={"ABC": "6"}),
            policy(max_abs_position="10"),
        )
        failed = {rule.rule for rule in decision.rules if not rule.passed}
        self.assertIn("position_limit", failed)
        self.assertEqual(decision.resulting_position, Decimal("11"))

    def test_short_requires_affirmative_borrow(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="SELL", quantity="3", price="100",
                expected_state_version=7,
            ),
            context(positions={"ABC": "0"}, borrow_available=None),
            policy(),
        )
        self.assertFalse(decision.admitted)
        self.assertIn("short_borrow", {r.rule for r in decision.rules if not r.passed})

    def test_reduce_only_sell_cannot_cross_through_flat(self):
        intent = RiskIntent.create(
            symbol="ABC", side="SELL", quantity="3", price="100",
            expected_state_version=7, reduce_only=True,
        )
        decision = evaluate_risk(intent, context(positions={"ABC": "2"}), policy())
        self.assertFalse(decision.admitted)
        self.assertIn("reduce_only", {r.rule for r in decision.rules if not r.passed})

    def test_genuine_reduce_only_can_decrease_risk_while_account_is_over_limits(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC",
                side="SELL",
                quantity="2",
                price="100",
                expected_state_version=7,
                reduce_only=True,
            ),
            context(
                positions={"ABC": "10"},
                marks={"ABC": "100"},
                daily_pnl="-600",
                drawdown_fraction="0.30",
                margin_headroom="0.10",
                stress_scenarios=({"ABC": "-0.50"},),
            ),
            policy(
                max_abs_position="5",
                max_single_notional="100",
                max_gross_leverage="0.5",
                max_net_leverage="0.5",
                max_daily_loss="100",
                max_drawdown_fraction="0.10",
                min_margin_headroom="0.30",
                max_stress_loss="50",
            ),
        )
        self.assertTrue(decision.admitted)
        self.assertEqual(decision.resulting_position, Decimal("8"))
        self.assertFalse({r.rule for r in decision.rules if not r.passed})

    def test_reduce_only_does_not_get_exception_if_portfolio_net_risk_worsens(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC",
                side="SELL",
                quantity="1",
                price="100",
                expected_state_version=7,
                reduce_only=True,
            ),
            context(
                positions={"ABC": "10", "XYZ": "-20"},
                marks={"ABC": "100", "XYZ": "50"},
                stress_scenarios=({"ABC": "-0.10", "XYZ": "-0.10"},),
            ),
            policy(
                max_abs_position="5",
                max_gross_leverage="1",
                max_net_leverage="0.05",
            ),
        )
        self.assertFalse(decision.admitted)
        failed = {r.rule for r in decision.rules if not r.passed}
        self.assertTrue({"position_limit", "gross_leverage", "net_leverage"} & failed)

    def test_protective_reduction_still_requires_fresh_data_and_state(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC",
                side="SELL",
                quantity="1",
                price="100",
                expected_state_version=6,
                reduce_only=True,
            ),
            context(
                positions={"ABC": "10"},
                marks={"ABC": "100"},
                market_data_age_seconds="10",
            ),
            policy(max_abs_position="5", max_data_age_seconds="5"),
        )
        self.assertFalse(decision.admitted)
        failed = {r.rule for r in decision.rules if not r.passed}
        self.assertTrue({"state_version", "market_freshness"} <= failed)

    def test_unmarked_reduction_cannot_bypass_breached_caps(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC",
                side="SELL",
                quantity="1",
                price="100",
                expected_state_version=7,
                reduce_only=False,
            ),
            context(positions={"ABC": "10"}, marks={"ABC": "100"}),
            policy(max_abs_position="5", max_gross_leverage="0.5"),
        )
        self.assertFalse(decision.admitted)

    def test_high_order_price_cannot_bypass_single_notional_limit(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="6", price="200",
                expected_state_version=7,
            ),
            context(positions={"ABC": "0"}, marks={"ABC": "100"}),
            policy(max_single_notional="1000"),
        )
        self.assertFalse(decision.admitted)
        failed = {rule.rule for rule in decision.rules if not rule.passed}
        self.assertIn("single_notional", failed)

    def test_risk_boolean_inputs_fail_closed(self):
        with self.assertRaises(TypeError):
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7, reduce_only="false",
            )
        with self.assertRaises(TypeError):
            context(capability_allowed="false")
        with self.assertRaises(TypeError):
            context(borrow_available="true")

    def test_drawdown_fraction_above_one_is_rejected(self):
        with self.assertRaises(ValueError):
            context(drawdown_fraction="1.01")

    def test_stress_and_daily_loss_and_drawdown_are_independent_hard_rules(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                positions={"ABC": "8"},
                daily_pnl="-600",
                drawdown_fraction="0.25",
                stress_scenarios=({"ABC": "-0.80"},),
            ),
            policy(max_stress_loss="500"),
        )
        failed = {rule.rule for rule in decision.rules if not rule.passed}
        self.assertTrue({"daily_loss", "drawdown", "stress_loss"} <= failed)

    def test_capability_and_margin_are_not_strategy_overridable(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(capability_allowed=False, margin_headroom="0.10"),
            policy(),
        )
        failed = {rule.rule for rule in decision.rules if not rule.passed}
        self.assertTrue({"capability", "margin_headroom"} <= failed)
        self.assertFalse(decision.admitted)

    def test_missing_mark_fails_before_admission(self):
        with self.assertRaisesRegex(ValueError, "Missing mark"):
            evaluate_risk(
                RiskIntent.create(
                    symbol="ABC", side="BUY", quantity="1", price="100",
                    expected_state_version=7,
                ),
                context(marks={"XYZ": "50"}),
                policy(),
            )

    def test_missing_stress_scenarios_block_nonzero_exposure(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(stress_scenarios=()),
            policy(),
        )
        failed = {rule.rule for rule in decision.rules if not rule.passed}
        self.assertIn("stress_coverage", failed)
        self.assertIn("stress_loss", failed)
        self.assertFalse(decision.admitted)

    def test_incomplete_stress_scenario_cannot_hide_existing_position_risk(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                positions={"ABC": "2", "XYZ": "3"},
                stress_scenarios=({"ABC": "-0.10"},),
            ),
            policy(),
        )
        failed = {rule.rule for rule in decision.rules if not rule.passed}
        self.assertIn("stress_coverage", failed)
        stress_rule = next(rule for rule in decision.rules if rule.rule == "stress_coverage")
        self.assertEqual(stress_rule.observed, "XYZ")
        self.assertFalse(decision.admitted)

    def test_full_liquidation_does_not_require_artificial_stress_scenario(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7, reduce_only=True,
            ),
            context(
                positions={"ABC": "-1"},
                stress_scenarios=(),
            ),
            policy(),
        )
        coverage = next(rule for rule in decision.rules if rule.rule == "stress_coverage")
        self.assertTrue(coverage.passed)
        self.assertTrue(decision.admitted)

    def test_semantically_duplicate_identity_keys_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "unique after normalization"):
            context(
                positions={"ABC": "1", " ABC ": "2"},
                marks={"ABC": "100"},
            )
        with self.assertRaisesRegex(ValueError, "unique after normalization"):
            context(
                marks={"ABC": "100", " ABC ": "101"},
            )
        with self.assertRaisesRegex(ValueError, "unique after normalization"):
            context(
                stress_scenarios=(
                    {"ABC": "-0.1", " ABC ": "-0.2", "XYZ": "-0.2"},
                ),
            )

    def test_risk_mapping_and_scenario_container_types_fail_closed(self):
        with self.assertRaisesRegex(TypeError, "positions must be a mapping"):
            context(positions=[("ABC", "1")])
        with self.assertRaisesRegex(TypeError, "stress_scenarios must be a sequence"):
            context(stress_scenarios="ABC:-0.1")

    def test_float_inputs_are_rejected(self):
        with self.assertRaises(TypeError):
            RiskPolicy.create(
                max_abs_position=10.0,
                max_single_notional="1000",
                max_gross_leverage="2",
                max_net_leverage="2",
                max_daily_loss="100",
                max_drawdown_fraction="0.2",
                max_data_age_seconds="5",
                max_fx_age_seconds="60",
                min_margin_headroom="0.2",
                max_stress_loss="100",
            )


    def test_authority_margin_and_borrow_evidence_cannot_be_omitted(self):
        common = dict(
            state_version=7,
            equity="1000",
            positions={"ABC": "2"},
            marks={"ABC": "100"},
        )
        with self.assertRaises(TypeError):
            RiskContext.create(
                **common,
                capability_allowed=True,
                borrow_available=True,
            )
        with self.assertRaises(TypeError):
            RiskContext.create(
                **common,
                margin_headroom="0.50",
                borrow_available=True,
            )
        with self.assertRaises(TypeError):
            RiskContext.create(
                **common,
                margin_headroom="0.50",
                capability_allowed=True,
            )

    def test_explicit_unknown_borrow_blocks_new_short_but_not_long(self):
        unknown = context(borrow_available=None)
        short = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="SELL", quantity="3", price="100",
                expected_state_version=7,
            ),
            unknown,
            policy(),
        )
        self.assertFalse(short.admitted)
        failed = {rule.rule for rule in short.rules if not rule.passed}
        self.assertIn("short_borrow", failed)

        long = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            unknown,
            policy(),
        )
        self.assertTrue(long.admitted)

    def test_configured_liquidity_participation_fails_closed_without_capacity(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=7,
        )
        configured = policy(max_order_participation_fraction="0.10")

        missing = evaluate_risk(intent, context(liquidity_capacity={}), configured)
        missing_rule = next(
            rule for rule in missing.rules if rule.rule == "liquidity_participation"
        )
        self.assertFalse(missing_rule.passed)
        self.assertEqual(missing_rule.observed, "UNKNOWN")

        oversized = evaluate_risk(
            intent,
            context(liquidity_capacity={"ABC": "5"}),
            configured,
        )
        oversized_rule = next(
            rule for rule in oversized.rules if rule.rule == "liquidity_participation"
        )
        self.assertFalse(oversized_rule.passed)
        self.assertEqual(oversized_rule.observed, "0.2")

    def test_asset_concentration_uses_whole_projected_portfolio(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                positions={"ABC": "2", "XYZ": "10"},
                asset_buckets={"ABC": "TECH", "XYZ": "INDUSTRIAL"},
                stress_scenarios=(
                    {"ABC": "-0.10", "XYZ": "-0.10"},
                ),
            ),
            policy(max_asset_concentration_fraction="0.65"),
        )
        rule = next(rule for rule in decision.rules if rule.rule == "asset_concentration")
        self.assertTrue(rule.passed)
        self.assertEqual(rule.observed, "0.625")

        blocked = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                positions={"ABC": "2", "XYZ": "10"},
                asset_buckets={"ABC": "TECH", "XYZ": "INDUSTRIAL"},
                stress_scenarios=(
                    {"ABC": "-0.10", "XYZ": "-0.10"},
                ),
            ),
            policy(max_asset_concentration_fraction="0.60"),
        )
        # XYZ is 500 of 800 gross = 0.625, so the configured 0.60 cap blocks.
        self.assertFalse(blocked.admitted)
        self.assertIn(
            "asset_concentration",
            {item.rule for item in blocked.rules if not item.passed},
        )

    def test_concentration_requires_complete_bucket_and_venue_identity(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                positions={"ABC": "2", "XYZ": "3"},
                asset_buckets={"ABC": "TECH"},
                venues={"ABC": "VENUE-A"},
                stress_scenarios=(
                    {"ABC": "-0.10", "XYZ": "-0.10"},
                ),
            ),
            policy(
                max_asset_concentration_fraction="1",
                max_venue_concentration_fraction="1",
            ),
        )
        failed = {item.rule: item.observed for item in decision.rules if not item.passed}
        self.assertEqual(failed["asset_concentration"], "MISSING:XYZ")
        self.assertEqual(failed["venue_concentration"], "MISSING:XYZ")

    def test_balanced_concentration_and_liquidity_can_pass(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                positions={"ABC": "2", "XYZ": "4"},
                asset_buckets={"ABC": "TECH", "XYZ": "INDUSTRIAL"},
                venues={"ABC": "VENUE-A", "XYZ": "VENUE-B"},
                liquidity_capacity={"ABC": "10"},
                stress_scenarios=(
                    {"ABC": "-0.10", "XYZ": "-0.10"},
                ),
            ),
            policy(
                max_asset_concentration_fraction="0.60",
                max_venue_concentration_fraction="0.60",
                max_order_participation_fraction="0.20",
            ),
        )
        self.assertTrue(decision.admitted)

    def test_optional_risk_fractions_reject_float_and_values_above_one(self):
        with self.assertRaises(TypeError):
            policy(max_order_participation_fraction=0.1)
        with self.assertRaises(ValueError):
            policy(max_asset_concentration_fraction="1.01")
        with self.assertRaises(ValueError):
            policy(max_venue_concentration_fraction="1.01")

    def test_factor_exposure_aggregates_correlated_positions(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                positions={"ABC": "2", "XYZ": "4"},
                factor_loadings={
                    "ABC": {"EQUITY": "1"},
                    "XYZ": {"EQUITY": "0.8"},
                },
                stress_scenarios=({"ABC": "-0.10", "XYZ": "-0.10"},),
            ),
            policy(max_abs_factor_exposure="450"),
        )
        rule = next(item for item in decision.rules if item.rule == "factor_exposure")
        self.assertFalse(rule.passed)
        self.assertEqual(rule.observed, "460.0")
        self.assertFalse(decision.admitted)

    def test_factor_exposure_recognizes_signed_hedge(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                positions={"ABC": "1", "XYZ": "-4"},
                factor_loadings={
                    "ABC": {"EQUITY": "1"},
                    "XYZ": {"EQUITY": "1"},
                },
                stress_scenarios=({"ABC": "-0.10", "XYZ": "-0.10"},),
            ),
            policy(max_abs_factor_exposure="50"),
        )
        rule = next(item for item in decision.rules if item.rule == "factor_exposure")
        self.assertTrue(rule.passed)
        self.assertEqual(rule.observed, "0")

    def test_factor_exposure_fails_closed_on_missing_loading(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                positions={"ABC": "1", "XYZ": "1"},
                factor_loadings={"ABC": {"EQUITY": "1"}},
                stress_scenarios=({"ABC": "-0.10", "XYZ": "-0.10"},),
            ),
            policy(max_abs_factor_exposure="1000"),
        )
        rule = next(item for item in decision.rules if item.rule == "factor_exposure")
        self.assertFalse(rule.passed)
        self.assertEqual(rule.observed, "MISSING:XYZ")

    def test_factor_loading_rejects_binary_float(self):
        with self.assertRaises(TypeError):
            context(factor_loadings={"ABC": {"EQUITY": 1.0}})

    def test_execution_quality_limits_fail_closed_on_missing_evidence(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(spread_fraction={}, slippage_fraction={}),
            policy(max_spread_fraction="0.01", max_slippage_fraction="0.02"),
        )
        failed = {item.rule: item.observed for item in decision.rules if not item.passed}
        self.assertEqual(failed["spread"], "UNKNOWN")
        self.assertEqual(failed["slippage"], "UNKNOWN")

    def test_execution_quality_limits_block_excess_cost(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                spread_fraction={"ABC": "0.005"},
                slippage_fraction={"ABC": "0.03"},
            ),
            policy(max_spread_fraction="0.01", max_slippage_fraction="0.02"),
        )
        spread = next(item for item in decision.rules if item.rule == "spread")
        slippage = next(item for item in decision.rules if item.rule == "slippage")
        self.assertTrue(spread.passed)
        self.assertFalse(slippage.passed)
        self.assertFalse(decision.admitted)

    def test_execution_quality_evidence_rejects_binary_float(self):
        with self.assertRaises(TypeError):
            context(spread_fraction={"ABC": 0.01})
        with self.assertRaises(TypeError):
            context(slippage_fraction={"ABC": 0.01})

    def test_clock_freshness_fails_closed_when_required_evidence_missing(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(clock_age_seconds=None),
            policy(max_clock_age_seconds="2"),
        )
        rule = next(item for item in decision.rules if item.rule == "clock_freshness")
        self.assertFalse(rule.passed)
        self.assertEqual(rule.observed, "UNKNOWN")

    def test_clock_freshness_boundary_is_exact(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=7,
        )
        exact = evaluate_risk(
            intent,
            context(clock_age_seconds="2"),
            policy(max_clock_age_seconds="2"),
        )
        stale = evaluate_risk(
            intent,
            context(clock_age_seconds="2.0001"),
            policy(max_clock_age_seconds="2"),
        )
        self.assertTrue(next(x for x in exact.rules if x.rule == "clock_freshness").passed)
        self.assertFalse(next(x for x in stale.rules if x.rule == "clock_freshness").passed)

    def test_clock_age_rejects_binary_float(self):
        with self.assertRaises(TypeError):
            context(clock_age_seconds=0.1)

    def test_action_policy_blocks_disallowed_action_class(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7, action="HEDGE",
            ),
            context(),
            policy(allowed_actions=("TRADE", "REDUCE")),
        )
        rule = next(item for item in decision.rules if item.rule == "allowed_action")
        self.assertFalse(rule.passed)
        self.assertEqual(rule.observed, "HEDGE")
        self.assertFalse(decision.admitted)

    def test_reduce_and_flatten_labels_require_reduce_only_semantics(self):
        with self.assertRaisesRegex(ValueError, "requires reduce_only"):
            RiskIntent.create(
                symbol="ABC", side="SELL", quantity="1", price="100",
                expected_state_version=7, action="REDUCE",
            )
        with self.assertRaisesRegex(ValueError, "requires reduce_only"):
            RiskIntent.create(
                symbol="ABC", side="SELL", quantity="1", price="100",
                expected_state_version=7, action="FLATTEN",
            )

    def test_action_label_does_not_override_numeric_risk(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="20", price="100",
                expected_state_version=7, action="HEDGE",
            ),
            context(),
            policy(allowed_actions=("HEDGE",)),
        )
        self.assertFalse(decision.admitted)
        self.assertIn("position_limit", {x.rule for x in decision.rules if not x.passed})

    def test_allowed_action_configuration_rejects_duplicates_and_unknowns(self):
        with self.assertRaises(ValueError):
            policy(allowed_actions=("TRADE", "trade"))
        with self.assertRaises(ValueError):
            policy(allowed_actions=("MAGIC",))

    def test_settlement_policy_fails_closed_without_affirmative_evidence(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=7,
        )
        missing = evaluate_risk(
            intent,
            context(settlement_allowed=None),
            policy(require_settlement_evidence=True),
        )
        blocked = evaluate_risk(
            intent,
            context(settlement_allowed=False),
            policy(require_settlement_evidence=True),
        )
        self.assertEqual(
            next(x for x in missing.rules if x.rule == "settlement").observed,
            "UNKNOWN",
        )
        self.assertFalse(next(x for x in missing.rules if x.rule == "settlement").passed)
        self.assertFalse(next(x for x in blocked.rules if x.rule == "settlement").passed)

    def test_settlement_policy_accepts_only_explicit_true(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(settlement_allowed=True),
            policy(require_settlement_evidence=True),
        )
        self.assertTrue(next(x for x in decision.rules if x.rule == "settlement").passed)

    def test_settlement_inputs_require_real_booleans(self):
        with self.assertRaises(TypeError):
            context(settlement_allowed="true")
        with self.assertRaises(TypeError):
            policy(require_settlement_evidence="true")

    def test_option_exercise_requires_verified_deliverable_and_buying_power(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="5",
                expected_state_version=7,
                action="EXERCISE", instrument_type="OPTION",
            ),
            context(
                option_deliverable_verified=None,
                option_exercise_cash_required="5000",
                option_exercise_cash_available="4999.99",
            ),
            policy(
                max_single_notional="10000",
                require_option_exercise_evidence=True,
            ),
        )
        failed = {item.rule for item in decision.rules if not item.passed}
        self.assertIn("option_deliverable", failed)
        self.assertIn("option_exercise_funding", failed)

    def test_option_exercise_accepts_exact_buying_power_boundary(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="5",
                expected_state_version=7,
                action="EXERCISE", instrument_type="OPTION",
            ),
            context(
                option_deliverable_verified=True,
                option_exercise_cash_required="5000",
                option_exercise_cash_available="5000",
            ),
            policy(
                max_single_notional="10000",
                require_option_exercise_evidence=True,
            ),
        )
        option_rules = {
            item.rule: item.passed
            for item in decision.rules
            if item.rule.startswith("option_")
        }
        self.assertEqual(
            option_rules,
            {"option_deliverable": True, "option_exercise_funding": True},
        )

    def test_exercise_action_cannot_be_labeled_on_non_option(self):
        with self.assertRaisesRegex(ValueError, "requires OPTION"):
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
                action="EXERCISE", instrument_type="EQUITY",
            )

    def test_option_obligation_inputs_reject_binary_float_and_fake_booleans(self):
        with self.assertRaises(TypeError):
            context(option_exercise_cash_required=5000.0)
        with self.assertRaises(TypeError):
            context(option_deliverable_verified="true")
        with self.assertRaises(TypeError):
            policy(require_option_exercise_evidence="true")

    def test_future_new_risk_requires_delivery_headroom_evidence(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=7, instrument_type="FUTURE",
        )
        missing = evaluate_risk(
            intent,
            context(futures_delivery_headroom_seconds={}),
            policy(min_futures_delivery_headroom_seconds="3600"),
        )
        too_close = evaluate_risk(
            intent,
            context(futures_delivery_headroom_seconds={"ABC": "3599.9"}),
            policy(min_futures_delivery_headroom_seconds="3600"),
        )
        self.assertEqual(
            next(x for x in missing.rules if x.rule == "futures_delivery_cutoff").observed,
            "UNKNOWN",
        )
        self.assertFalse(
            next(x for x in too_close.rules if x.rule == "futures_delivery_cutoff").passed
        )

    def test_future_reduce_only_can_flatten_inside_delivery_cutoff(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="SELL", quantity="2", price="100",
                expected_state_version=7, reduce_only=True,
                action="FLATTEN", instrument_type="FUTURE",
            ),
            context(
                positions={"ABC": "2"},
                futures_delivery_headroom_seconds={"ABC": "-10"},
                stress_scenarios=(),
            ),
            policy(
                min_futures_delivery_headroom_seconds="3600",
                allowed_actions=("FLATTEN",),
            ),
        )
        rule = next(x for x in decision.rules if x.rule == "futures_delivery_cutoff")
        self.assertTrue(rule.passed)
        self.assertEqual(rule.observed, "RISK_REDUCTION")

    def test_future_delivery_headroom_rejects_binary_float(self):
        with self.assertRaises(TypeError):
            context(futures_delivery_headroom_seconds={"ABC": 3600.0})
        with self.assertRaises(TypeError):
            policy(min_futures_delivery_headroom_seconds=3600.0)

    def test_expected_shortfall_uses_complete_projected_tail_distribution(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=7,
        )
        tail = (
            {"ABC": "-0.10"},
            {"ABC": "-0.20"},
            {"ABC": "0.05"},
            {"ABC": "-0.40"},
        )
        boundary = evaluate_risk(
            intent,
            context(tail_scenarios=tail),
            policy(
                max_expected_shortfall="90",
                expected_shortfall_tail_fraction="0.50",
            ),
        )
        rule = next(x for x in boundary.rules if x.rule == "expected_shortfall")
        self.assertTrue(rule.passed)
        self.assertEqual(rule.observed, "90")

        blocked = evaluate_risk(
            intent,
            context(tail_scenarios=tail),
            policy(
                max_expected_shortfall="89.99",
                expected_shortfall_tail_fraction="0.50",
            ),
        )
        self.assertFalse(blocked.admitted)
        blocked_rule = next(x for x in blocked.rules if x.rule == "expected_shortfall")
        self.assertFalse(blocked_rule.passed)
        self.assertEqual(blocked_rule.observed, "90")

    def test_expected_shortfall_fails_closed_without_complete_tail_evidence(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=7,
        )
        configured = policy(
            max_expected_shortfall="500",
            expected_shortfall_tail_fraction="0.25",
        )
        missing = evaluate_risk(
            intent,
            context(tail_scenarios=()),
            configured,
        )
        self.assertFalse(missing.admitted)
        self.assertFalse(next(x for x in missing.rules if x.rule == "tail_coverage").passed)
        self.assertEqual(
            next(x for x in missing.rules if x.rule == "expected_shortfall").observed,
            "UNKNOWN",
        )

        incomplete = evaluate_risk(
            intent,
            context(
                positions={"ABC": "2", "XYZ": "1"},
                tail_scenarios=({"ABC": "-0.10"},),
                stress_scenarios=({"ABC": "-0.10", "XYZ": "-0.10"},),
            ),
            configured,
        )
        coverage = next(x for x in incomplete.rules if x.rule == "tail_coverage")
        self.assertFalse(coverage.passed)
        self.assertEqual(coverage.observed, "XYZ")

    def test_expected_shortfall_policy_requires_explicit_tail_fraction(self):
        with self.assertRaisesRegex(ValueError, "configured together"):
            policy(max_expected_shortfall="100")
        with self.assertRaisesRegex(ValueError, "configured together"):
            policy(expected_shortfall_tail_fraction="0.05")
        with self.assertRaises(ValueError):
            policy(
                max_expected_shortfall="100",
                expected_shortfall_tail_fraction="1.01",
            )

    def test_liquidation_headroom_is_fail_closed_and_exact_at_boundary(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=7,
        )
        configured = policy(min_liquidation_headroom="0.25")
        missing = evaluate_risk(
            intent,
            context(liquidation_headroom=None),
            configured,
        )
        missing_rule = next(
            x for x in missing.rules if x.rule == "liquidation_headroom"
        )
        self.assertFalse(missing_rule.passed)
        self.assertEqual(missing_rule.observed, "UNKNOWN")

        exact = evaluate_risk(
            intent,
            context(liquidation_headroom="0.25"),
            configured,
        )
        self.assertTrue(
            next(x for x in exact.rules if x.rule == "liquidation_headroom").passed
        )

    def test_tail_and_liquidation_inputs_reject_binary_float(self):
        with self.assertRaises(TypeError):
            context(tail_scenarios=({"ABC": -0.10},))
        with self.assertRaises(TypeError):
            context(liquidation_headroom=0.25)
        with self.assertRaises(TypeError):
            policy(
                max_expected_shortfall=100.0,
                expected_shortfall_tail_fraction="0.05",
            )
        with self.assertRaises(TypeError):
            policy(
                max_expected_shortfall="100",
                expected_shortfall_tail_fraction=0.05,
            )

    def test_evaluate_risk_revalidates_direct_dataclass_construction(self):
        good_context = context()
        good_policy = policy()

        forged_intent = RiskIntent(
            symbol="ABC",
            side="BUY",
            quantity=1.0,
            price=Decimal("100"),
            expected_state_version=7,
        )
        with self.assertRaisesRegex(TypeError, "quantity must use Decimal"):
            evaluate_risk(forged_intent, good_context, good_policy)

        forged_context = RiskContext(
            **{
                **good_context.__dict__,
                "capability_allowed": "true",
            }
        )
        with self.assertRaisesRegex(TypeError, "capability_allowed must be a boolean"):
            evaluate_risk(
                RiskIntent.create(
                    symbol="ABC",
                    side="BUY",
                    quantity="1",
                    price="100",
                    expected_state_version=7,
                ),
                forged_context,
                good_policy,
            )

        forged_policy = RiskPolicy(
            **{
                **good_policy.__dict__,
                "max_drawdown_fraction": Decimal("1.01"),
            }
        )
        with self.assertRaisesRegex(ValueError, "max_drawdown_fraction cannot exceed 1"):
            evaluate_risk(
                RiskIntent.create(
                    symbol="ABC",
                    side="BUY",
                    quantity="1",
                    price="100",
                    expected_state_version=7,
                ),
                good_context,
                forged_policy,
            )

    def test_evaluate_risk_rejects_wrong_boundary_types(self):
        valid_intent = RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity="1",
            price="100",
            expected_state_version=7,
        )
        with self.assertRaisesRegex(TypeError, "intent must be RiskIntent"):
            evaluate_risk({}, context(), policy())
        with self.assertRaisesRegex(TypeError, "context must be RiskContext"):
            evaluate_risk(valid_intent, {}, policy())
        with self.assertRaisesRegex(TypeError, "policy must be RiskPolicy"):
            evaluate_risk(valid_intent, context(), {})

    def test_risk_decision_fingerprint_is_deterministic_and_evidence_sensitive(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=7,
        )
        configured = policy(max_clock_age_seconds="5")
        first = evaluate_risk(
            intent,
            context(clock_age_seconds="1"),
            configured,
        )
        repeated = evaluate_risk(
            intent,
            context(clock_age_seconds="1"),
            configured,
        )
        changed = evaluate_risk(
            intent,
            context(clock_age_seconds="2"),
            configured,
        )
        first_hash = risk_decision_fingerprint(first)
        self.assertEqual(first_hash, risk_decision_fingerprint(repeated))
        self.assertNotEqual(first_hash, risk_decision_fingerprint(changed))
        self.assertEqual(len(first_hash), 64)

    def test_risk_decision_fingerprint_rejects_wrong_type(self):
        with self.assertRaises(TypeError):
            risk_decision_fingerprint({"admitted": True})


if __name__ == "__main__":
    unittest.main()
