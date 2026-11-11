from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

from research.autotrade_research.strategies.fast_screen import Costs
from research.autotrade_research.strategies.trend_regime import TrendRegime, screen, simulate


def bars(closes):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    result = []
    for index, close in enumerate(closes):
        opened = closes[index-1] if index else close
        result.append(dict(opened_at=start+timedelta(hours=index),
            closed_at=start+timedelta(hours=index+1), open=str(opened),
            high=str(max(opened, close)), low=str(min(opened, close)),
            close=str(close), volume="100000"))
    return result


def fake_result(candidate, partition, pnl, cycles=4, drawdown="0.01"):
    return dict(candidate=candidate, observations=len(partition), final_cash=str(1000+pnl),
        net_pnl=str(pnl), return_fraction=str(Decimal(pnl)/1000),
        max_drawdown_fraction=drawdown, fees="0", fills=cycles*2,
        closed_cycles=cycles, execution_digest="a"*64, entry_signals=cycles,
        exit_signals=cycles, exit_reasons={},
        first_open=partition[0]["opened_at"].isoformat(),
        last_close=partition[-1]["closed_at"].isoformat())


class TrendRegimeTests(unittest.TestCase):
    def test_next_open_execution_and_completed_close_causality(self):
        data = bars([100, 101, 102, 103, 104, 105, 106, 107])
        config = TrendRegime(fast_lookback=2, slow_lookback=3,
            entry_threshold="0", slow_threshold="0", exit_threshold="0.5",
            trailing_stop_fraction="0.5", min_hold_bars=1, max_hold_bars=20)
        result = simulate(data, candidate="TREND_REGIME_TRAILING",
            costs=Costs(fee_bps="0", spread_bps="0", slippage_bps="0",
                        capital_fraction="1"), configuration=config, trace=True)
        self.assertEqual(result["trace"][0]["action"], "BUY")
        self.assertEqual(result["trace"][0]["at"], data[4]["opened_at"].isoformat())
        changed = [dict(row) for row in data]
        changed[5].update(close="1", low="1")
        future = simulate(changed, candidate="TREND_REGIME_TRAILING",
            costs=Costs(fee_bps="0", spread_bps="0", slippage_bps="0",
                        capital_fraction="1"), configuration=config, trace=True)
        self.assertEqual(result["trace"][0], future["trace"][0])

    def test_costs_and_trailing_exit_are_accounted(self):
        data = bars([100, 101, 102, 103, 110, 100, 99, 98])
        config = TrendRegime(fast_lookback=2, slow_lookback=3,
            entry_threshold="0", slow_threshold="0", exit_threshold="0.5",
            trailing_stop_fraction="0.05", min_hold_bars=1, max_hold_bars=20)
        result = simulate(data, candidate="TREND_REGIME_TRAILING",
            configuration=config, trace=True)
        self.assertGreater(Decimal(result["fees"]), 0)
        self.assertIn("TRAILING_STOP_EXIT", result["exit_reasons"])

    def test_train_rejection_does_not_read_later_partitions(self):
        data = bars(range(100, 118))
        data[12]["close"] = "NaN"
        seen = []
        def fake(partition, *, candidate, **unused):
            seen.append(partition[0]["opened_at"])
            pnl = {"CASH": 0, "HOLD": 2, "TREND_REGIME_TRAILING": 1}[candidate]
            return fake_result(candidate, partition, pnl)
        with patch("research.autotrade_research.strategies.trend_regime.simulate", side_effect=fake):
            result = screen(data, train_count=6, development_count=6, dataset_sha256="b"*64)
        self.assertEqual(len(seen), 3)
        self.assertFalse(result["holdout_consumed"])

    def test_development_gate_precedes_final(self):
        data = bars(range(100, 118))
        seen = []
        def fake(partition, *, candidate, **unused):
            stage = partition[0]["opened_at"].hour // 6
            seen.append((stage, candidate))
            tables = ({"CASH": 0, "HOLD": 1, "TREND_REGIME_TRAILING": 3},
                      {"CASH": 0, "HOLD": 1, "TREND_REGIME_TRAILING": 2},
                      {"CASH": 0, "HOLD": -1, "TREND_REGIME_TRAILING": 1})
            return fake_result(candidate, partition, tables[stage][candidate])
        with patch("research.autotrade_research.strategies.trend_regime.simulate", side_effect=fake):
            result = screen(data, train_count=6, development_count=6, dataset_sha256="c"*64)
        self.assertTrue(result["holdout_consumed"])
        self.assertEqual(sum(stage == 2 for stage, _ in seen), 3)

    def test_configuration_bounds(self):
        for config in (TrendRegime(fast_lookback=1),
                       TrendRegime(fast_lookback=10, slow_lookback=5),
                       TrendRegime(trailing_stop_fraction="0"),
                       TrendRegime(min_hold_bars=4, max_hold_bars=3)):
            with self.assertRaises(ValueError):
                simulate(bars(range(100, 120)), candidate="CASH", configuration=config)


if __name__ == "__main__":
    unittest.main()
