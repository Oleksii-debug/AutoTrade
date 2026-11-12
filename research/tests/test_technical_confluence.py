from datetime import UTC, datetime, timedelta
from decimal import Decimal
import unittest

from research.autotrade_research.strategies.technical_confluence import (
    TechnicalConfiguration, TechnicalCosts, simulate,
)
from tools.screen_whitebit_technical_confluence import default_grid, locked_final_screen


def bars(count=900, *, falling=False):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    price = Decimal("100")
    result = []
    for index in range(count):
        price *= Decimal("0.9998" if falling else "1.0002")
        opened = start + timedelta(minutes=index)
        close = price.quantize(Decimal("0.00000001"))
        result.append({"opened_at": opened, "closed_at": opened + timedelta(minutes=1),
            "open": str(close), "high": str(close * Decimal("1.001")),
            "low": str(close * Decimal("0.999")), "close": str(close),
            "volume": "1000"})
    return result


def flat_bars(count=900):
    result = bars(count)
    for row in result:
        row.update(open="100", high="100", low="100", close="100")
    return result


class TechnicalConfluenceTests(unittest.TestCase):
    def test_cost_envelope_is_whitebit_futures_fifteen_bps(self):
        self.assertEqual(TechnicalCosts().round_trip_bps, Decimal("15.0"))

    def test_signal_uses_next_open_and_has_no_order_authority(self):
        result = simulate(bars(), candidate="TECHNICAL_CONFLUENCE",
            configuration=TechnicalConfiguration(rsi_long_max="100",
                minimum_expected_move_fraction="0.001", maximum_hold_bars=30,
                stop_atr="0.10", take_profit_atr="3"))
        self.assertGreater(result["closed_cycles"], 0)
        self.assertEqual(result["round_trip_cost_bps"], "15.0")
        self.assertFalse(result["network_orders"])
        self.assertFalse(result["trading_authority"])

    def test_sub_two_to_one_thesis_is_hold_before_entry(self):
        result = simulate(bars(), candidate="TECHNICAL_CONFLUENCE",
            configuration=TechnicalConfiguration(rsi_long_max="100",
                minimum_expected_move_fraction="0.001", maximum_hold_bars=30,
                stop_atr="2", take_profit_atr="3"))
        self.assertEqual(result["closed_cycles"], 0)
        self.assertGreater(result["pretrade_holds"], 0)
        self.assertIn("GROSS_REWARD_RISK", result["pretrade_hold_reasons"])

    def test_short_control_profits_in_falling_market(self):
        result = simulate(bars(falling=True), candidate="HOLD_SHORT")
        self.assertGreater(Decimal(result["net_pnl"]), 0)
        self.assertEqual(result["short_cycles"], 1)

    def test_flat_market_does_not_create_phantom_pending_entries(self):
        result = simulate(flat_bars(), candidate="TECHNICAL_CONFLUENCE")
        self.assertEqual(result["closed_cycles"], 0)
        self.assertEqual(result["fill_count"], 0)

    def test_locked_final_does_not_consume_final_without_prior_pass(self):
        value = locked_final_screen(bars(1200), train_count=700,
            development_count=300, dataset_sha256="a" * 64,
            configurations=default_grid()[:2], minimum_cycles=3)
        self.assertFalse(value["final_consumed"])
        self.assertEqual(value["selected_candidate"], "CASH")
        self.assertFalse(value["network_orders"])

    def test_absolute_profit_gate_is_explicit_and_preserves_controls(self):
        value = locked_final_screen(bars(1200), train_count=700,
            development_count=300, dataset_sha256="b" * 64,
            configurations=default_grid()[:2], minimum_cycles=3,
            gate_policy="POSITIVE_AFTER_COSTS")
        self.assertEqual(value["gate_policy"], "POSITIVE_AFTER_COSTS")
        self.assertEqual(set(value["controls"]), {"TRAIN", "DEVELOPMENT"})
        self.assertFalse(value["canonical_qualification"])


if __name__ == "__main__":
    unittest.main()

