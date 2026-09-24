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


if __name__ == "__main__":
    unittest.main()
