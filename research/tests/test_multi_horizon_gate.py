from __future__ import annotations

from decimal import Decimal
import unittest

from autotrade_research.learning.multi_horizon_gate import evaluate_multi_horizon_gate


def series(count: int, step: str) -> list[dict[str, str]]:
    result = []
    price = Decimal("100")
    for index in range(count):
        result.append({
            "available_at": f"2026-11-11T00:{index:02d}:00+00:00",
            "bid": format(price, "f"),
            "ask": format(price + Decimal("0.01"), "f"),
        })
        price += Decimal(step)
    return result


class MultiHorizonGateTests(unittest.TestCase):
    def test_selects_on_train_and_gates_only_later_cut(self):
        report = evaluate_multi_horizon_gate(
            series(50, "1"), minimum_train_trades=5,
            minimum_evaluation_trades=5,
        )
        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(report["selected_candidate"]["family"], "MOMENTUM")
        self.assertGreater(Decimal(report["train"]["net_after_costs_usdt"]), 0)
        self.assertGreater(Decimal(report["evaluation"]["net_after_costs_usdt"]), 0)
        self.assertTrue(report["qualifies_for_research_simulation"])
        self.assertFalse(report["profitability_proven"])
        self.assertFalse(report["automatic_trading_promotion"])
        self.assertFalse(report["trading_authority"])

    def test_flat_market_fails_after_cost_gate(self):
        report = evaluate_multi_horizon_gate(
            series(50, "0"), minimum_train_trades=5,
            minimum_evaluation_trades=5,
        )
        self.assertIsNone(report["selected_candidate"])
        self.assertEqual(report["status"], "INSUFFICIENT_TRAIN_SAMPLE")
        self.assertFalse(report["profitability_proven"])

    def test_rejects_reordered_or_float_prices(self):
        data = series(20, "1")
        data[2]["available_at"] = data[1]["available_at"]
        with self.assertRaisesRegex(ValueError, "increasing"):
            evaluate_multi_horizon_gate(data)
        data = series(20, "1")
        data[0]["bid"] = 100.0
        with self.assertRaisesRegex(ValueError, "exact"):
            evaluate_multi_horizon_gate(data)

    def test_gap_jump_is_not_a_tradeable_feature(self):
        data = []
        for day, price in (("11", "100"), ("12", "200")):
            for minute in range(30):
                data.append({
                    "available_at": f"2026-11-{day}T00:{minute:02d}:00+00:00",
                    "bid": price,
                    "ask": format(Decimal(price) + Decimal("0.01"), "f"),
                })
        report = evaluate_multi_horizon_gate(
            data, minimum_train_trades=1, minimum_evaluation_trades=1
        )
        self.assertEqual(report["status"], "INSUFFICIENT_TRAIN_SAMPLE")
        self.assertIsNone(report["selected_candidate"])


if __name__ == "__main__":
    unittest.main()
