from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research.autotrade_research.strategies.fast_screen import Costs, simulate, screen
from research.autotrade_research.strategies.deterministic import ReturnThresholdBaseline, CausalObservation
from tools.screen_historical_candidates import stage_checkpoint


def bars(closes):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [dict(opened_at=start+timedelta(hours=i), closed_at=start+timedelta(hours=i+1),
                 open=str(closes[i-1] if i else closes[0]), close=str(p)) for i, p in enumerate(closes)]


class FastScreenTests(unittest.TestCase):
    def test_next_open_execution_matches_existing_strategy_signal(self):
        data = bars([100, 110, 110, 90, 90, 100])
        data[2]["open"] = "120"
        result = simulate(data, candidate="MOMENTUM", costs=Costs(lookback=2), trace=True)
        self.assertEqual(result["trace"][0]["at"], data[2]["opened_at"].isoformat())
        self.assertEqual(Decimal(result["trace"][0]["price"]), Decimal("120.12"))
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.002", proposal_quantity="1")
        for i, row in enumerate(data[:2]):
            strategy.ingest(CausalObservation.create(event_id=str(i), symbol="SCREEN",
                available_at=row["closed_at"], price=row["close"]), simulation_time=row["closed_at"])
        self.assertEqual(strategy.propose(symbol="SCREEN", decision_time=data[1]["closed_at"]).action,
                         result["trace"][0]["action"])

    def test_round_trip_charges_both_sides_and_cannot_borrow(self):
        data = bars([100, 110, 110, 110])
        result = simulate(data, candidate="MOMENTUM", costs=Costs(lookback=2, capital_fraction="1"), trace=True)
        self.assertEqual(result["fills"], 2)
        self.assertEqual(result["closed_cycles"], 1)
        self.assertLess(Decimal(result["net_pnl"]), 0)
        self.assertEqual(Decimal(result["fees"]), sum(Decimal(row["fee"]) for row in result["trace"]))
        self.assertTrue(all(Decimal(row["cash"]) >= 0 for row in result["trace"]))
        free = simulate(data, candidate="MOMENTUM", costs=Costs(lookback=2, fee_bps="0", spread_bps="0", slippage_bps="0"))
        self.assertEqual(Decimal(free["net_pnl"]), 0)

    def test_future_changes_do_not_change_training_or_earlier_fills(self):
        data = bars([100, 110, 120, 130, 140, 150, 160, 170])
        changed = [dict(row) for row in data]
        for row in changed[4:]:
            row["open"], row["close"] = "1", "2"
        a = screen(data, train_count=4, dataset_sha256="a"*64, costs=Costs(lookback=2))
        b = screen(changed, train_count=4, dataset_sha256="b"*64, costs=Costs(lookback=2))
        self.assertEqual(a["training"], b["training"])
        self.assertEqual(a["selected_candidate"], b["selected_candidate"])

    def test_drawdown_cash_control_and_bad_inputs(self):
        data = bars([100, 110, 120, 1, 1, 1, 1, 1])
        result = simulate(data, candidate="MOMENTUM", costs=Costs(lookback=2))
        self.assertGreater(Decimal(result["max_drawdown_fraction"]), 0)
        cash = simulate(data, candidate="CASH", costs=Costs())
        self.assertEqual(cash["net_pnl"], "0")
        for bad in (Costs(fee_bps="-1"), Costs(capital_fraction="2"), Costs(threshold="NaN")):
            with self.assertRaises(ValueError):
                simulate(data, candidate="CASH", costs=bad)
        data[1]["opened_at"] = data[0]["opened_at"]
        with self.assertRaises(ValueError):
            simulate(data, candidate="CASH", costs=Costs())

    def test_stage_resume_and_tamper_rejection(self):
        data = bars([100, 110, 120, 130, 140, 150, 160, 170])
        with TemporaryDirectory() as directory:
            checkpoint = stage_checkpoint(directory)
            expected = screen(data, train_count=4, dataset_sha256="a"*64, checkpoint=checkpoint)
            with patch("research.autotrade_research.strategies.fast_screen.simulate", side_effect=AssertionError("must reuse completed stage")):
                self.assertEqual(screen(data, train_count=4, dataset_sha256="a"*64, checkpoint=checkpoint), expected)
            path = next(Path(directory).glob("*.json"))
            path.write_text(path.read_text().replace('"sha256": "', '"sha256": "bad'), encoding="utf-8")
            with self.assertRaises(ValueError):
                screen(data, train_count=4, dataset_sha256="a"*64, checkpoint=checkpoint)


if __name__ == "__main__":
    unittest.main()
