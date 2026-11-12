from datetime import UTC, datetime, timedelta
from decimal import Decimal
import unittest
from unittest.mock import patch

from research.autotrade_research.strategies.technical_confluence import (
    TechnicalConfiguration, TechnicalCosts, simulate,
)
from tools.screen_whitebit_technical_confluence import (
    default_grid, intraday_trend_configuration, locked_final_screen,
)


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

    def test_screen_configs_can_clear_the_pretrade_reward_gate(self):
        for config in (*default_grid(), intraday_trend_configuration()):
            self.assertGreater(Decimal(config.take_profit_atr) / Decimal(config.stop_atr), 2)
        intraday = intraday_trend_configuration()
        self.assertEqual((intraday.minimum_hold_bars, intraday.maximum_hold_bars), (2, 4))
        self.assertEqual(intraday.maximum_entries_per_utc_day, 1)

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

    def test_hourly_execution_attribution_reconciles_net_return_for_both_sides(self):
        for side in ("LONG", "SHORT"):
            with self.subTest(side=side):
                result = simulate(bars(count=65, falling=side == "SHORT"),
                                  candidate=f"HOLD_{side}", report_hourly_returns=True)
                receipt = result["execution_attribution"]
                direction = receipt["by_direction"][side]
                self.assertEqual(direction, receipt["by_exit_reason"]["PARTITION_END_EXIT"])
                self.assertEqual(direction["cycles"], 1)
                self.assertEqual(Decimal(direction["net_pnl"]), Decimal(result["net_pnl"]))
                self.assertGreater(Decimal(direction["spread_slippage_cost"]), 0)
                self.assertEqual(Decimal(direction["fees"]), Decimal(result["fees"]))
                self.assertEqual(Decimal(direction["funding"]), Decimal(result["funding"]))
                reconciled = (Decimal(direction["gross_pnl_at_reference"])
                              - Decimal(direction["spread_slippage_cost"])
                              - Decimal(direction["fees"]) - Decimal(direction["funding"]))
                self.assertAlmostEqual(reconciled, Decimal(result["net_pnl"]), places=24)

    def test_hourly_context_requires_warmup_and_rejects_a_countertrend_entry(self):
        values = bars(410)
        for index, bar in enumerate(values):
            opened_at = values[0]["opened_at"] + timedelta(minutes=15 * index)
            close = Decimal(bar["close"])
            bar.update(opened_at=opened_at, closed_at=opened_at + timedelta(minutes=15),
                       high=str(close * Decimal("1.02")), low=str(close * Decimal("0.98")))
        cfg = TechnicalConfiguration(fast_ema=8, slow_ema=32, stop_atr="1",
                                     take_profit_atr="4", maximum_hold_bars=4)
        with patch("research.autotrade_research.strategies.technical_confluence._entry",
                   return_value=-1):
            baseline = simulate(values, candidate="TECHNICAL_CONFLUENCE", configuration=cfg)
            gated = simulate(values, candidate="TECHNICAL_CONFLUENCE", configuration=cfg,
                             require_hourly_trend_alignment=True)
        self.assertGreater(baseline["closed_cycles"], 0)
        self.assertEqual(gated["closed_cycles"], 0)
        self.assertGreater(gated["pretrade_hold_reasons"]["HOURLY_TREND_WARMUP"], 0)
        self.assertGreater(gated["pretrade_hold_reasons"]["HOURLY_TREND_CONFLICT"], 0)

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
