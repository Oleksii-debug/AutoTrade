from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research.autotrade_research.strategies.fast_screen import Costs, QuickReject, simulate, screen
from research.autotrade_research.strategies.deterministic import ReturnThresholdBaseline, CausalObservation
from tools.screen_historical_candidates import stage_checkpoint


def bars(closes):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    result = []
    for i, close in enumerate(closes):
        opened = closes[i-1] if i else closes[0]
        result.append(dict(
            opened_at=start+timedelta(hours=i), closed_at=start+timedelta(hours=i+1),
            open=str(opened), high=str(max(opened, close)), low=str(min(opened, close)),
            close=str(close), volume="100000",
        ))
    return result


class FastScreenTests(unittest.TestCase):
    def test_next_open_execution_matches_existing_strategy_signal(self):
        data = bars([100, 110, 110, 90, 90, 100])
        data[2]["open"] = "120"
        data[2]["high"] = "120"
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
            row.update(open="1", high="2", low="1", close="2")
        a = screen(data, train_count=4, dataset_sha256="a"*64, costs=Costs(lookback=2))
        b = screen(changed, train_count=4, dataset_sha256="b"*64, costs=Costs(lookback=2))
        self.assertEqual(a["training"], b["training"])
        self.assertEqual(a["selected_candidate"], b["selected_candidate"])
        self.assertEqual({row["candidate"] for row in a["evaluation_matrix"]},
                         {"CASH", "MOMENTUM", "MEAN_REVERSION", "MOMENTUM_QUICK_REJECT"})

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

    def test_quick_reject_uses_only_completed_prefix_and_abstains(self):
        data = bars([100, 102, 104, 106, 108, 110, 112, 114])
        gate = QuickReject(lookback=3, min_volatility_fraction="0",
                           max_volatility_fraction="1", min_direction_agreement="0.5",
                           min_quote_volume="999999999999")
        gated = simulate(data, candidate="MOMENTUM_QUICK_REJECT",
                         costs=Costs(lookback=2), quick_reject=gate, trace=True)
        baseline = simulate(data, candidate="MOMENTUM",
                            costs=Costs(lookback=2), quick_reject=gate, trace=True)
        self.assertGreater(baseline["fills"], 0)
        self.assertEqual(gated["fills"], 0)
        self.assertEqual(gated["entry_signals"], gated["entry_rejections"])
        self.assertEqual(gated["entry_rejection_reasons"],
                         {"INSUFFICIENT_HISTORY": 1, "LOW_LIQUIDITY": 6})

        # The order at bar 3 was decided at bar 2 close. Mutating bar 3 and all
        # later OHLCV cannot rewrite that earlier causal decision/fill.
        permissive = QuickReject(lookback=3, min_volatility_fraction="0",
                                 max_volatility_fraction="1", min_direction_agreement="0.5",
                                 min_quote_volume="0")
        changed = [dict(row) for row in data]
        for row in changed[3:]:
            row.update(high="1000000", low="1", close="1", volume="0")
        original = simulate(data, candidate="MOMENTUM_QUICK_REJECT",
                            costs=Costs(lookback=2), quick_reject=permissive, trace=True)
        future = simulate(changed, candidate="MOMENTUM_QUICK_REJECT",
                          costs=Costs(lookback=2), quick_reject=permissive, trace=True)
        self.assertEqual(original["trace"][0], future["trace"][0])

    def test_quick_reject_bounds_and_ohlcv_are_evidence_identity(self):
        data = bars([100, 102, 104, 106, 108, 110, 112, 114])
        for gate in (
            QuickReject(lookback=2),
            QuickReject(min_volatility_fraction="0.2", max_volatility_fraction="0.1"),
            QuickReject(min_direction_agreement="0.49"),
            QuickReject(min_quote_volume="NaN"),
        ):
            with self.assertRaises(ValueError):
                simulate(data, candidate="MOMENTUM_QUICK_REJECT", costs=Costs(),
                         quick_reject=gate)
        incomplete = [dict(row) for row in data]
        del incomplete[0]["volume"]
        with self.assertRaisesRegex(ValueError, "OHLCV"):
            simulate(incomplete, candidate="MOMENTUM_QUICK_REJECT", costs=Costs())

        first = screen(data, train_count=4, dataset_sha256="a"*64,
                       quick_reject=QuickReject(lookback=3, min_quote_volume="0"))
        changed = [dict(row) for row in data]
        changed[-1]["volume"] = "100001"
        second = screen(changed, train_count=4, dataset_sha256="a"*64,
                        quick_reject=QuickReject(lookback=3, min_quote_volume="0"))
        self.assertNotEqual(first["config_sha256"], second["config_sha256"])
        changed_gate = screen(data, train_count=4, dataset_sha256="a"*64,
                              quick_reject=QuickReject(lookback=3, min_quote_volume="1"))
        self.assertNotEqual(first["config_sha256"], changed_gate["config_sha256"])


if __name__ == "__main__":
    unittest.main()
