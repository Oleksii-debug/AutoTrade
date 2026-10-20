from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research.autotrade_research.strategies.fast_screen import (
    AdaptiveExit, Costs, adaptive_exit_screen, simulate_adaptive_exit,
)
from tools.screen_historical_candidates import stage_checkpoint


def bars(closes):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    result = []
    for index, close in enumerate(closes):
        opened = closes[index-1] if index else close
        result.append(dict(
            opened_at=start+timedelta(hours=index),
            closed_at=start+timedelta(hours=index+1),
            open=str(opened), high=str(max(opened, close)),
            low=str(min(opened, close)), close=str(close), volume="100000",
        ))
    return result


def fake_result(candidate, partition, pnl, cycles=5, drawdown="0.01"):
    return dict(candidate=candidate, observations=len(partition), final_cash=str(1000+pnl),
        net_pnl=str(pnl), return_fraction=str(Decimal(pnl)/1000),
        max_drawdown_fraction=drawdown, fees="0", fills=cycles*2,
        closed_cycles=cycles, execution_digest=(candidate[0].lower() or "a")*64,
        entry_signals=cycles, exit_signals=cycles, exit_reasons={},
        first_open=partition[0]["opened_at"].isoformat(),
        last_close=partition[-1]["closed_at"].isoformat())


class AdaptiveExitScreenTests(unittest.TestCase):
    def test_hold_control_charges_both_sides(self):
        result = simulate_adaptive_exit(
            bars([100, 101, 102, 103]), candidate="HOLD",
            costs=Costs(capital_fraction="1"), trace=True)
        self.assertEqual([row["action"] for row in result["trace"]], ["BUY", "SELL"])
        self.assertEqual(result["closed_cycles"], 1)
        self.assertGreater(Decimal(result["fees"]), 0)
        self.assertEqual(result["trace"][0]["reason"], "PRECOMMITTED_PARTITION_START")
        self.assertEqual(result["trace"][-1]["reason"], "PARTITION_END_EXIT")

    def test_adaptive_exit_is_completed_bar_causal(self):
        data = bars([100, 101, 102, 120, 121, 122, 123, 124])
        settings = dict(candidate="MOMENTUM_ADAPTIVE_EXIT",
            costs=Costs(lookback=2, fee_bps="0", spread_bps="0", slippage_bps="0"),
            adaptive_exit=AdaptiveExit(lookback=3, exit_volatility_fraction="0.001",
                                       min_hold_bars=1, max_hold_bars=100), trace=True)
        original = simulate_adaptive_exit(data, **settings)
        self.assertEqual(original["trace"][1]["reason"], "VOLATILITY_EXIT")
        changed = [dict(row) for row in data]
        for row in changed[4:]:
            row.update(high=str(max(Decimal(row["open"]), Decimal("1"))),
                       low="1", close="1", volume="1")
        future = simulate_adaptive_exit(changed, **settings)
        self.assertEqual(original["trace"][:2], future["trace"][:2])

    def test_bounds_and_identity(self):
        data = bars(range(100, 118))
        for settings in (AdaptiveExit(lookback=2),
                         AdaptiveExit(exit_volatility_fraction="1.01"),
                         AdaptiveExit(min_hold_bars=0),
                         AdaptiveExit(min_hold_bars=5, max_hold_bars=4)):
            with self.assertRaises(ValueError):
                simulate_adaptive_exit(data, candidate="CASH", adaptive_exit=settings)
        first = adaptive_exit_screen(data, train_count=6, development_count=6,
            dataset_sha256="a"*64, adaptive_exit=AdaptiveExit(lookback=3))
        second = adaptive_exit_screen(data, train_count=6, development_count=6,
            dataset_sha256="a"*64, adaptive_exit=AdaptiveExit(lookback=4))
        self.assertNotEqual(first["config_sha256"], second["config_sha256"])

    def test_train_reject_never_reads_development_or_final(self):
        data = bars(range(100, 118))
        # Deliberately invalid later values prove the rejected TRAIN path does
        # not validate, hash or simulate either later partition.
        data[12]["close"] = "NaN"
        seen = []

        def fake(partition, *, candidate, **unused):
            seen.append(partition[0]["opened_at"])
            pnl = {"CASH": 0, "HOLD": 2, "MOMENTUM": 5,
                   "MOMENTUM_ADAPTIVE_EXIT": 4}[candidate]
            return fake_result(candidate, partition, pnl)

        with patch("research.autotrade_research.strategies.fast_screen.simulate_adaptive_exit",
                   side_effect=fake):
            result = adaptive_exit_screen(data, train_count=6, development_count=6,
                dataset_sha256="b"*64)
        self.assertEqual(len(seen), 4)
        self.assertTrue(all(value == data[0]["opened_at"] for value in seen))
        self.assertIn("ADAPTIVE_EXIT_DID_NOT_BEAT_MOMENTUM",
                      result["training_rejection_reasons"])
        self.assertEqual(result["development"], [])
        self.assertFalse(result["holdout_consumed"])

    def test_development_gate_precedes_single_final_read(self):
        data = bars(range(100, 118))

        def run(development_pnl):
            seen = []
            def fake(partition, *, candidate, **unused):
                stage = partition[0]["opened_at"].hour // 6
                seen.append((stage, candidate))
                table = {
                    0: {"CASH": 0, "HOLD": 1, "MOMENTUM": 2,
                        "MOMENTUM_ADAPTIVE_EXIT": 3},
                    1: {"CASH": 0, "HOLD": 1,
                        "MOMENTUM_ADAPTIVE_EXIT": development_pnl},
                    2: {"CASH": 0, "HOLD": -1,
                        "MOMENTUM_ADAPTIVE_EXIT": 2},
                }
                return fake_result(candidate, partition, table[stage][candidate])
            with patch("research.autotrade_research.strategies.fast_screen.simulate_adaptive_exit",
                       side_effect=fake):
                result = adaptive_exit_screen(data, train_count=6, development_count=6,
                    dataset_sha256="c"*64)
            return result, seen

        rejected, rejected_seen = run(0)
        self.assertFalse(rejected["holdout_consumed"])
        self.assertFalse(any(stage == 2 for stage, _ in rejected_seen))
        passed, passed_seen = run(2)
        self.assertTrue(passed["holdout_consumed"])
        self.assertEqual(sum(stage == 2 for stage, _ in passed_seen), 3)

    def test_restart_reuses_stages_and_detects_tamper(self):
        data = bars(range(100, 118))
        with TemporaryDirectory() as directory:
            checkpoint = stage_checkpoint(directory)
            expected = adaptive_exit_screen(data, train_count=6, development_count=6,
                dataset_sha256="d"*64, checkpoint=checkpoint)
            with patch("research.autotrade_research.strategies.fast_screen.simulate_adaptive_exit",
                       side_effect=AssertionError("must reuse checkpoint")):
                self.assertEqual(adaptive_exit_screen(
                    data, train_count=6, development_count=6,
                    dataset_sha256="d"*64, checkpoint=checkpoint), expected)
            path = next(Path(directory).glob("*.json"))
            path.write_text(path.read_text().replace('"sha256": "', '"sha256": "bad'),
                            encoding="utf-8")
            with self.assertRaises(ValueError):
                adaptive_exit_screen(data, train_count=6, development_count=6,
                    dataset_sha256="d"*64, checkpoint=checkpoint)


if __name__ == "__main__":
    unittest.main()
