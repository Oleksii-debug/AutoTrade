from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research.autotrade_research.strategies.volatility_expansion_breakout import (
    CheckpointStore,
    ExecutionCosts,
    GateConfiguration,
    StrategyConfiguration,
    screen,
    simulate,
)


def make_bars(rows):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    result = []
    for index, row in enumerate(rows):
        opened, high, low, close, volume = row
        result.append({
            "opened_at": start + timedelta(hours=index),
            "closed_at": start + timedelta(hours=index + 1),
            "open": str(opened),
            "high": str(high),
            "low": str(low),
            "close": str(close),
            "volume": str(volume),
        })
    return result


def flat_bars(count=24):
    return make_bars([(100, 101, 99, 100, 100)] * count)


def breakout_bars(*, stop_close=False, low_volume=False):
    rows = [
        (100, 101, 99, 100, 100),
        (100, 101, 99, 100, 100),
        (100, 101, 99, 100, 100),
        (100, 110, 99, 109, 100 if low_volume else 300),
        (120, 122, 50 if not stop_close else 95, 100 if stop_close else 121, 100),
        (90 if stop_close else 121, 123, 89 if stop_close else 120,
         91 if stop_close else 122, 100),
        (91 if stop_close else 122, 124, 90 if stop_close else 121,
         92 if stop_close else 123, 100),
    ]
    return make_bars(rows)


def strategy(**overrides):
    values = {
        "breakout_lookback": 2,
        "volatility_lookback": 2,
        "range_expansion_multiplier": "1.5",
        "minimum_range_fraction": "0",
        "volume_lookback": 2,
        "volume_multiplier": "1.5",
        "minimum_mean_quote_volume": "0",
        "breakout_buffer_bps": "0",
        "stop_loss_fraction": "0.04",
        "exit_lookback": 2,
        "minimum_hold_bars": 2,
        "maximum_hold_bars": 10,
    }
    values.update(overrides)
    return StrategyConfiguration(**values)


def fake_result(candidate, pnl, *, cycles=2, drawdown="0.01"):
    return {
        "candidate": candidate,
        "net_pnl": str(pnl),
        "closed_cycles": cycles,
        "max_drawdown_fraction": drawdown,
    }


class VolatilityExpansionBreakoutTests(unittest.TestCase):
    def setUp(self):
        self.free = ExecutionCosts(fee_bps="0", spread_bps="0", slippage_bps="0")

    def test_configuration_and_bar_validation_are_strict(self):
        for configuration in (
            StrategyConfiguration(breakout_lookback=1),
            StrategyConfiguration(range_expansion_multiplier="0.9"),
            StrategyConfiguration(stop_loss_fraction="0"),
            StrategyConfiguration(minimum_hold_bars=4, maximum_hold_bars=3),
        ):
            with self.assertRaises(ValueError):
                configuration.validate()
        bad = flat_bars(4)
        bad[2]["opened_at"] += timedelta(minutes=1)
        with self.assertRaisesRegex(ValueError, "continuous"):
            simulate(bad, candidate="CASH")

    def test_breakout_signal_fills_following_open_with_explicit_costs(self):
        result = simulate(
            breakout_bars(), candidate="VOLATILITY_EXPANSION_BREAKOUT",
            costs=ExecutionCosts(fee_bps="10", spread_bps="10", slippage_bps="5"),
            strategy=strategy(), trace=True,
        )
        buy = result["trace"][0]
        self.assertEqual(buy["action"], "BUY")
        self.assertEqual(buy["signal_at"], breakout_bars()[3]["closed_at"].isoformat())
        self.assertEqual(Decimal(buy["reference_price"]), Decimal("120"))
        self.assertEqual(Decimal(buy["execution_price"]), Decimal("120.12"))
        self.assertGreater(Decimal(result["fees"]), 0)
        self.assertGreater(Decimal(result["modeled_spread_cost"]), 0)
        self.assertGreater(Decimal(result["modeled_slippage_cost"]), 0)
        self.assertFalse(result["network_orders"])
        self.assertEqual(result["paid_model_calls"], 0)

    def test_future_candles_cannot_change_earlier_fill(self):
        bars = breakout_bars()
        first = simulate(
            bars, candidate="VOLATILITY_EXPANSION_BREAKOUT",
            costs=self.free, strategy=strategy(), trace=True,
        )
        changed = [dict(row) for row in bars]
        for row in changed[4:]:
            opened = Decimal(row["open"])
            row.update(high=str(opened + 100), low="1", close=str(opened), volume="1")
        second = simulate(
            changed, candidate="VOLATILITY_EXPANSION_BREAKOUT",
            costs=self.free, strategy=strategy(), trace=True,
        )
        self.assertEqual(first["trace"][0], second["trace"][0])

    def test_volume_confirmation_rejects_otherwise_identical_breakout(self):
        rejected = simulate(
            breakout_bars(low_volume=True),
            candidate="VOLATILITY_EXPANSION_BREAKOUT",
            costs=self.free, strategy=strategy(), trace=True,
        )
        accepted = simulate(
            breakout_bars(), candidate="VOLATILITY_EXPANSION_BREAKOUT",
            costs=self.free, strategy=strategy(), trace=True,
        )
        self.assertEqual(rejected["fills"], 0)
        self.assertGreaterEqual(accepted["fills"], 2)

    def test_intrabar_low_does_not_trigger_stop_but_close_stop_fills_next_open(self):
        intrabar_only = simulate(
            breakout_bars(stop_close=False),
            candidate="VOLATILITY_EXPANSION_BREAKOUT",
            costs=self.free,
            strategy=strategy(minimum_hold_bars=10, maximum_hold_bars=10),
            trace=True,
        )
        self.assertNotIn("CLOSE_STOP", intrabar_only["exit_reasons"])

        bars = breakout_bars(stop_close=True)
        stopped = simulate(
            bars, candidate="VOLATILITY_EXPANSION_BREAKOUT",
            costs=self.free,
            strategy=strategy(minimum_hold_bars=10, maximum_hold_bars=10),
            trace=True,
        )
        sell = stopped["trace"][1]
        self.assertEqual(sell["reason"], "CLOSE_STOP")
        self.assertEqual(sell["signal_at"], bars[4]["closed_at"].isoformat())
        self.assertEqual(sell["at"], bars[5]["opened_at"].isoformat())
        self.assertEqual(Decimal(sell["reference_price"]), Decimal("90"))

    def test_maximum_holding_exit_is_filled_at_next_open(self):
        bars = breakout_bars()
        result = simulate(
            bars, candidate="VOLATILITY_EXPANSION_BREAKOUT",
            costs=self.free,
            strategy=strategy(minimum_hold_bars=1, maximum_hold_bars=1),
            trace=True,
        )
        self.assertEqual(result["trace"][1]["reason"], "MAXIMUM_HOLD")
        self.assertEqual(result["trace"][1]["at"], bars[5]["opened_at"].isoformat())

    def test_cash_and_hold_controls_are_explicit(self):
        bars = breakout_bars()
        cash = simulate(bars, candidate="CASH", trace=True)
        hold = simulate(bars, candidate="BTC_HOLD", trace=True)
        self.assertEqual(cash["fills"], 0)
        self.assertEqual(Decimal(cash["net_pnl"]), 0)
        self.assertEqual([row["action"] for row in hold["trace"]], ["BUY", "SELL"])
        self.assertEqual(hold["trace"][0]["reason"], "PRECOMMITTED_PARTITION_START")
        self.assertEqual(hold["trace"][1]["reason"], "PARTITION_END_LIQUIDATION")

    def test_train_rejection_never_reads_development_or_final(self):
        bars = flat_bars(18)
        bars[12]["close"] = "NaN"
        seen = []

        def fake(partition, *, candidate, **unused):
            seen.append(partition[0]["opened_at"])
            pnl = {"CASH": 0, "BTC_HOLD": 10,
                   "VOLATILITY_EXPANSION_BREAKOUT": 5}[candidate]
            return fake_result(candidate, pnl)

        with patch(
            "research.autotrade_research.strategies.volatility_expansion_breakout.simulate",
            side_effect=fake,
        ):
            result = screen(
                bars, train_count=6, development_count=6,
                dataset_sha256="a" * 64,
                gate=GateConfiguration(minimum_closed_cycles=1),
            )
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(item == bars[0]["opened_at"] for item in seen))
        self.assertEqual(result["phase"], "REJECTED_TRAIN")
        self.assertIn("DID_NOT_BEAT_HOLD_AFTER_COSTS", result["training_rejection_reasons"])
        self.assertFalse(result["development_consumed"])
        self.assertFalse(result["final_consumed"])
        self.assertEqual(result["development"], [])
        self.assertEqual(result["final"], [])

    def test_development_rejection_keeps_final_untouched(self):
        bars = flat_bars(18)
        seen = []

        def fake(partition, *, candidate, **unused):
            stage = partition[0]["opened_at"].hour // 6
            seen.append((stage, candidate))
            table = {
                0: {"CASH": 0, "BTC_HOLD": 10,
                    "VOLATILITY_EXPANSION_BREAKOUT": 20},
                1: {"CASH": 0, "BTC_HOLD": 10,
                    "VOLATILITY_EXPANSION_BREAKOUT": 5},
            }
            return fake_result(candidate, table[stage][candidate])

        with patch(
            "research.autotrade_research.strategies.volatility_expansion_breakout.simulate",
            side_effect=fake,
        ):
            result = screen(
                bars, train_count=6, development_count=6,
                dataset_sha256="b" * 64,
                gate=GateConfiguration(minimum_closed_cycles=1),
            )
        self.assertEqual(result["phase"], "REJECTED_DEVELOPMENT")
        self.assertTrue(result["development_consumed"])
        self.assertFalse(result["final_consumed"])
        self.assertFalse(any(stage == 2 for stage, _ in seen))

    def test_checkpoint_reuse_and_tamper_detection(self):
        bars = flat_bars(24)
        with TemporaryDirectory() as directory:
            checkpoint = CheckpointStore(directory)
            expected = screen(
                bars, train_count=8, development_count=8,
                dataset_sha256="c" * 64,
                gate=GateConfiguration(minimum_closed_cycles=1),
                checkpoint=checkpoint,
            )
            with patch(
                "research.autotrade_research.strategies.volatility_expansion_breakout.simulate",
                side_effect=AssertionError("completed TRAIN stages must be reused"),
            ):
                self.assertEqual(
                    screen(
                        bars, train_count=8, development_count=8,
                        dataset_sha256="c" * 64,
                        gate=GateConfiguration(minimum_closed_cycles=1),
                        checkpoint=checkpoint,
                    ),
                    expected,
                )
            target = next(Path(directory).glob("*.json"))
            target.write_text(
                target.read_text(encoding="utf-8").replace(
                    '"value_sha256":"', '"value_sha256":"bad', 1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "authentication"):
                screen(
                    bars, train_count=8, development_count=8,
                    dataset_sha256="c" * 64,
                    gate=GateConfiguration(minimum_closed_cycles=1),
                    checkpoint=checkpoint,
                )


if __name__ == "__main__":
    unittest.main()
