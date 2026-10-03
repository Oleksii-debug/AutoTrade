from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
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
        max_abs_position="9999999999999999999999999999",
        max_single_notional="9999999999999999999999999999",
        max_gross_leverage="1",
        max_net_leverage="1",
        max_daily_loss="9999999999999999999999999999",
        max_drawdown_fraction="1",
        max_data_age_seconds="5",
        max_fx_age_seconds="60",
        min_margin_headroom="0",
        max_stress_loss="9999999999999999999999999999",
    )
    values.update(overrides)
    return RiskPolicy.create(**values)


def exact_context():
    expected = "1234567890123456789012345679"
    return RiskContext.create(
        state_version=7,
        equity=expected,
        positions={"ABC": "1234567890123456789012345678.1"},
        marks={"ABC": "1"},
        reserved_position_delta={"ABC": "0.8"},
        daily_pnl="-0.1",
        drawdown_fraction="0",
        market_data_age_seconds="0",
        fx_age_seconds={},
        margin_headroom="1",
        capability_allowed=True,
        borrow_available=True,
        stress_scenarios=({"ABC": "-1"},),
        equivalent_exposure_per_unit={"ABC": "1"},
        instrument_types={"ABC": "GENERIC"},
    )


class RiskExactArithmeticTests(unittest.TestCase):
    def test_high_significance_financial_path_is_context_independent(self):
        intent = RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity="0.1",
            price="1",
            expected_state_version=7,
        )
        fingerprints = set()
        decision_fingerprints = set()
        snapshots = set()
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING):
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as decimal_context:
                        decimal_context.prec = precision
                        decimal_context.rounding = rounding
                        decision = evaluate_risk(intent, exact_context(), policy())
                    self.assertTrue(decision.admitted)
                    self.assertEqual(
                        decision.resulting_position,
                        Decimal("1234567890123456789012345679"),
                    )
                    self.assertEqual(
                        decision.worst_stress_loss,
                        Decimal("1234567890123456789012345679"),
                    )
                    self.assertEqual(decision.gross_leverage, Decimal("1"))
                    self.assertEqual(decision.net_leverage, Decimal("1"))
                    fingerprints.add(decision.input_fingerprint)
                    decision_fingerprints.add(
                        risk_decision_fingerprint(decision)
                    )
                    snapshots.add(
                        tuple(
                            (rule.rule, rule.passed, rule.observed, rule.limit)
                            for rule in decision.rules
                        )
                    )
        self.assertEqual(len(fingerprints), 1)
        self.assertEqual(len(decision_fingerprints), 1)
        self.assertEqual(len(snapshots), 1)

    def test_exact_resource_overflow_fails_before_risk_authority(self):
        oversized = "9" * 129
        with self.assertRaisesRegex(
            ValueError,
            "bounded finite decimal",
        ):
            RiskContext.create(
                state_version=7,
                equity="1000",
                positions={"ABC": oversized},
                marks={"ABC": "1"},
                daily_pnl="0",
                drawdown_fraction="0",
                market_data_age_seconds="0",
                fx_age_seconds={},
                margin_headroom="1",
                capability_allowed=True,
                borrow_available=True,
            )


if __name__ == "__main__":
    unittest.main()
