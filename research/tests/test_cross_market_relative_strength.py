from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research.autotrade_research.strategies.cross_market_relative_strength import (
    CheckpointStore,
    ExecutionCosts,
    GateConfiguration,
    StrategyConfiguration,
    screen,
    simulate,
)


def aligned_bars(btc_closes, eth_closes=None):
    if eth_closes is None:
        eth_closes = [100] * len(btc_closes)
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    btc, eth = [], []
    for index, (btc_close, eth_close) in enumerate(zip(btc_closes, eth_closes)):
        opened_at = start + timedelta(hours=index)
        closed_at = opened_at + timedelta(hours=1)
        btc_open = btc_closes[index - 1] if index else btc_close
        eth_open = eth_closes[index - 1] if index else eth_close
        btc.append(dict(opened_at=opened_at, closed_at=closed_at,
                        open=str(btc_open), close=str(btc_close)))
        eth.append(dict(opened_at=opened_at, closed_at=closed_at,
                        open=str(eth_open), close=str(eth_close)))
    return btc, eth


class CrossMarketRelativeStrengthTests(unittest.TestCase):
    def setUp(self):
        self.strategy = StrategyConfiguration(
            ratio_lookback=2, volatility_lookback=2,
            volatility_cap_fraction="1", momentum_lookback=2,
            min_hold_bars=3, max_hold_bars=3,
        )
        self.free = ExecutionCosts(fee_bps="0", spread_bps="0", slippage_bps="0")

    def test_alignment_and_configuration_reject_bad_inputs(self):
        btc, eth = aligned_bars([100, 101, 102, 103])
        eth[1]["opened_at"] += timedelta(minutes=1)
        with self.assertRaisesRegex(ValueError, "aligned"):
            simulate(btc, eth, candidate="CASH")
        for strategy in (
            StrategyConfiguration(ratio_lookback=1),
            StrategyConfiguration(min_hold_bars=2),
            StrategyConfiguration(min_hold_bars=4, max_hold_bars=3),
            StrategyConfiguration(volatility_cap_fraction="NaN"),
        ):
            with self.assertRaises(ValueError):
                strategy.validate()

    def test_completed_signal_executes_at_following_btc_open_with_costs(self):
        btc, eth = aligned_bars([100, 101, 103, 104, 105, 106, 107])
        btc[3]["open"] = "120"
        result = simulate(
            btc, eth, candidate="CROSS_RELATIVE_STRENGTH",
            strategy=self.strategy,
            costs=ExecutionCosts(fee_bps="10", spread_bps="10", slippage_bps="5"),
            trace=True,
        )
        self.assertEqual(result["trace"][0]["action"], "BUY")
        self.assertEqual(result["trace"][0]["signal_at"], btc[2]["closed_at"].isoformat())
        self.assertEqual(Decimal(result["trace"][0]["btc_price"]), Decimal("120.12"))
        self.assertTrue(all(Decimal(row["btc_quantity"]) >= 0 for row in result["trace"]))
        self.assertTrue(result["network_orders"] is False)

    def test_future_bars_cannot_change_earlier_fill(self):
        btc, eth = aligned_bars([100, 101, 103, 104, 105, 106, 107])
        first = simulate(btc, eth, candidate="CROSS_RELATIVE_STRENGTH",
                         strategy=self.strategy, costs=self.free, trace=True)
        changed_btc = [dict(row) for row in btc]
        changed_eth = [dict(row) for row in eth]
        for row in changed_btc[3:]:
            row["close"] = "1"
        for row in changed_eth[3:]:
            row["close"] = "10000"
        changed_btc[3]["open"] = btc[3]["open"]
        changed_eth[3]["open"] = eth[3]["open"]
        second = simulate(changed_btc, changed_eth,
                          candidate="CROSS_RELATIVE_STRENGTH",
                          strategy=self.strategy, costs=self.free, trace=True)
        self.assertEqual(first["trace"][0], second["trace"][0])

    def test_volatility_cap_can_abstain_from_breakout(self):
        btc, eth = aligned_bars([100, 200, 50, 250, 25, 300])
        strict = StrategyConfiguration(
            ratio_lookback=2, volatility_lookback=2,
            volatility_cap_fraction="0.01", momentum_lookback=2,
            min_hold_bars=3, max_hold_bars=3,
        )
        result = simulate(btc, eth, candidate="CROSS_RELATIVE_STRENGTH",
                          strategy=strict, costs=self.free, trace=True)
        self.assertEqual(result["fills"], 0)
        self.assertEqual(result["entry_signals"], 0)

    def test_train_rejection_keeps_later_partitions_untouched(self):
        btc, eth = aligned_bars([100] * 24)
        result = screen(
            btc, eth, train_count=8, development_count=8,
            dataset_sha256="a" * 64, strategy=self.strategy, costs=self.free,
            gate=GateConfiguration(minimum_closed_cycles=1),
        )
        self.assertEqual(result["phase"], "REJECTED_TRAIN")
        self.assertIn("MINIMUM_CLOSED_CYCLES_NOT_MET", result["training_rejection_reasons"])
        self.assertFalse(result["development_consumed"])
        self.assertFalse(result["final_consumed"])
        self.assertEqual(result["development"], [])
        self.assertEqual(result["final"], [])

    def test_development_rejection_keeps_final_untouched(self):
        btc_values = list(range(100, 112)) + [111] * 8 + list(range(112, 120))
        btc, eth = aligned_bars(btc_values)
        result = screen(
            btc, eth, train_count=12, development_count=8,
            dataset_sha256="b" * 64, strategy=self.strategy, costs=self.free,
            gate=GateConfiguration(minimum_closed_cycles=1),
        )
        self.assertFalse(result["training_rejection_reasons"])
        self.assertEqual(result["phase"], "REJECTED_DEVELOPMENT")
        self.assertTrue(result["development_consumed"])
        self.assertFalse(result["final_consumed"])

    def test_both_gates_are_required_before_one_final_evaluation(self):
        values = []
        for base in (100, 120, 140):
            values.extend(range(base, base + 12))
        btc, eth = aligned_bars(values)
        result = screen(
            btc, eth, train_count=12, development_count=12,
            dataset_sha256="c" * 64, strategy=self.strategy, costs=self.free,
            gate=GateConfiguration(minimum_closed_cycles=1, maximum_drawdown_fraction="1"),
        )
        self.assertFalse(result["training_rejection_reasons"])
        self.assertFalse(result["development_rejection_reasons"])
        self.assertTrue(result["final_consumed"])
        self.assertEqual({row["candidate"] for row in result["final"]},
                         {"CASH", "BTC_HOLD", "BTC_MOMENTUM", "CROSS_RELATIVE_STRENGTH"})

    def test_checkpoint_reuse_and_tamper_detection(self):
        btc, eth = aligned_bars([100] * 24)
        with TemporaryDirectory() as directory:
            checkpoint = CheckpointStore(directory)
            expected = screen(
                btc, eth, train_count=8, development_count=8,
                dataset_sha256="d" * 64, strategy=self.strategy, costs=self.free,
                gate=GateConfiguration(minimum_closed_cycles=1), checkpoint=checkpoint,
            )
            with patch(
                "research.autotrade_research.strategies.cross_market_relative_strength.simulate",
                side_effect=AssertionError("completed TRAIN stages must be reused"),
            ):
                self.assertEqual(
                    screen(
                        btc, eth, train_count=8, development_count=8,
                        dataset_sha256="d" * 64, strategy=self.strategy, costs=self.free,
                        gate=GateConfiguration(minimum_closed_cycles=1), checkpoint=checkpoint,
                    ),
                    expected,
                )
            target = next(Path(directory).glob("*.json"))
            target.write_text(target.read_text(encoding="utf-8").replace(
                '"value_sha256":"', '"value_sha256":"bad', 1,
            ), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "authentication"):
                screen(
                    btc, eth, train_count=8, development_count=8,
                    dataset_sha256="d" * 64, strategy=self.strategy, costs=self.free,
                    gate=GateConfiguration(minimum_closed_cycles=1), checkpoint=checkpoint,
                )


if __name__ == "__main__":
    unittest.main()
