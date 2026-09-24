from decimal import Decimal
import unittest

from mvp.autotrade_mvp.risk import RiskContext, RiskIntent, RiskPolicy, evaluate_risk


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


if __name__ == "__main__":
    unittest.main()
