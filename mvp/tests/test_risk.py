from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.risk import build_limits, evaluate_order


NOW = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)


def limits():
    return build_limits(
        max_order_notional="1000",
        max_position_quantity="10",
        max_gross_exposure="1500",
        max_stress_loss="300",
        stress_fraction="0.20",
        max_market_age=timedelta(seconds=5),
    )


def evaluate(**overrides):
    values = dict(
        limits=limits(),
        now=NOW,
        market_observed_at=NOW - timedelta(seconds=1),
        capability_status="VERIFIED",
        capability_expires_at=NOW + timedelta(minutes=5),
        side="BUY",
        quantity="2",
        price="100",
        contract_multiplier="1",
        current_position="0",
        available_cash="1000",
        already_reserved_cash="0",
        settlement_currency="USD",
    )
    values.update(overrides)
    return evaluate_order(**values)


class RiskFoundationTests(unittest.TestCase):
    def test_all_hard_checks_allow_and_emit_reservation(self):
        decision = evaluate()
        self.assertEqual(decision.verdict, "ALLOW")
        self.assertEqual(decision.reason_codes, ())
        self.assertEqual(decision.reservation_delta["CASH:USD"], Decimal("200"))
        self.assertTrue(all(check.status == "PASS" for check in decision.checks))

    def test_reserved_cash_reduces_current_availability(self):
        decision = evaluate(
            quantity="5",
            price="100",
            available_cash="600",
            already_reserved_cash="200",
        )
        self.assertEqual(decision.verdict, "REJECT")
        self.assertIn("RISK.INSUFFICIENT_AVAILABLE", decision.reason_codes)
        self.assertEqual(decision.reservation_delta, {})

    def test_expired_or_unknown_capability_fails_closed(self):
        expired = evaluate(capability_expires_at=NOW)
        self.assertEqual(expired.verdict, "REJECT")
        self.assertIn("AUTH.EXPIRED", expired.reason_codes)
        unknown = evaluate(capability_status="UNKNOWN")
        self.assertEqual(unknown.verdict, "REJECT")
        self.assertIn("CAPABILITY.UNKNOWN", unknown.reason_codes)

    def test_stale_or_future_market_data_fails_closed(self):
        stale = evaluate(market_observed_at=NOW - timedelta(seconds=6))
        self.assertEqual(stale.verdict, "REJECT")
        self.assertIn("DATA.STALE", stale.reason_codes)
        future = evaluate(market_observed_at=NOW + timedelta(seconds=1))
        self.assertEqual(future.verdict, "REJECT")
        self.assertIn("DATA.STALE", future.reason_codes)

    def test_position_and_gross_exposure_are_checked_after_order(self):
        decision = evaluate(
            side="BUY",
            quantity="3",
            price="100",
            current_position="9",
            available_cash="1000",
        )
        self.assertEqual(decision.verdict, "REJECT")
        self.assertIn("RISK.LIMIT_BREACH", decision.reason_codes)

    def test_stress_loss_is_an_independent_hard_limit(self):
        strict = build_limits(
            max_order_notional="5000",
            max_position_quantity="100",
            max_gross_exposure="5000",
            max_stress_loss="50",
            stress_fraction="0.20",
            max_market_age=timedelta(seconds=5),
        )
        decision = evaluate(
            limits=strict,
            quantity="3",
            price="100",
            available_cash="1000",
        )
        self.assertEqual(decision.verdict, "REJECT")
        failed = {check.rule_id for check in decision.checks if check.status == "FAIL"}
        self.assertEqual(failed, {"RISK.STRESS_LOSS"})

    def test_exact_boundary_is_allowed(self):
        decision = evaluate(
            quantity="10",
            price="100",
            current_position="0",
            available_cash="1000",
        )
        self.assertEqual(decision.verdict, "ALLOW")

    def test_binary_float_money_is_rejected(self):
        with self.assertRaises(TypeError):
            evaluate(price=100.1)


if __name__ == "__main__":
    unittest.main()
