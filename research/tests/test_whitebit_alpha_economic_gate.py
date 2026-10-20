"""Focused causal, economic and restart tests for the Alpha holdout gate."""
from __future__ import annotations

from decimal import Decimal
from importlib.util import find_spec
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import numpy as np

from mvp.autotrade_mvp.alpha_research_worker import _source
from research.autotrade_research.integrations.alpha_economics import (
    AlphaInputError,
    CostBudget,
)
from tools.run_whitebit_alpha_economic_gate import _simulate, evaluate
from tools.run_whitebit_alpha_research import _durable_worker
from tools.run_whitebit_historical_alpha import build_alpha_input
from tools.historical_public_klines import HistoricalArchiveError
from research.tests.test_whitebit_historical_alpha import fixture


class AlphaEconomicPureTests(unittest.TestCase):
    def test_simulation_executes_only_after_decision_and_charges_every_cost(self):
        source = SimpleNamespace(available_at_ms=tuple(1001 + 1000 * i for i in range(10)))
        costs = CostBudget(
            entry_fee_ratio="0.001", exit_fee_ratio="0.001", spread_bps="4",
            entry_slippage_bps="1", exit_slippage_bps="1", funding_borrow_bps="2",
            model_cost_quote="0.02", notional_quote="1000", min_notional_quote="5",
            quote_currency="USDT", source_ref="fixture", observed_at_ms=1,
            valid_until_ms=20_000,
        )
        rows = [
            {"open_ms": i * 1000, "open": value, "high": value + 1,
             "low": value - 1, "close": value}
            for i, value in enumerate(
                Decimal("100") + Decimal(i)
                for i in range(10))
        ]
        split = {
            "y": np.zeros(4), "holdout": np.array([0, 1, 2, 3]),
            "decision_times": np.array([3001, 4001, 5001, 6001]),
            "horizon": 1,
        }
        report, trace = _simulate(
            name="TEST", predictions=np.full(4, 0.10),
            radius=Decimal("0.01"), split=split,
            source=source, costs=costs, rows=rows, minimum_trades=1,
            maximum_drawdown=Decimal("0.10"))
        executed = [row for row in trace if row["outcome"] == "EXECUTED_SIMULATION"]
        self.assertGreaterEqual(len(executed), 1)
        self.assertTrue(all(row["decision_ms"] < row["entry_open_ms"] < row["exit_open_ms"]
                            for row in executed))
        self.assertNotEqual(report["fee_cost_quote"], "0")
        self.assertNotEqual(report["spread_cost_quote"], "0")
        self.assertNotEqual(report["slippage_cost_quote"], "0")
        self.assertNotEqual(report["funding_borrow_cost_quote"], "0")
        self.assertEqual(report["model_cost_quote"], "0.08")
        decomposed = (
            Decimal(report["gross_reference_pnl_quote"])
            - Decimal(report["fee_cost_quote"])
            - Decimal(report["spread_cost_quote"])
            - Decimal(report["slippage_cost_quote"])
            - Decimal(report["funding_borrow_cost_quote"])
            - Decimal(report["model_cost_quote"])
        )
        self.assertLess(abs(Decimal(report["net_pnl_after_costs_quote"]) - decomposed),
                        Decimal("1e-20"))
        self.assertEqual(report["starting_equity_quote"], "1000")
        self.assertFalse(report["financial_operation_executed"])
        self.assertFalse(report["live_authority"])


@unittest.skipUnless(find_spec("threadpoolctl") is not None,
                     "Alpha native dependencies are not installed")
class AlphaEconomicDurableTests(unittest.TestCase):
    def test_verified_job_evaluates_once_replays_and_detects_tamper(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = fixture(root, rows=200)
            payload, _selection = build_alpha_input(manifest, bars=200)
            alpha_state = root / "alpha"
            worker = _durable_worker(alpha_state)
            job, created = worker.enqueue(payload, steps=1, backends=("DLINEAR",))
            self.assertTrue(created)
            worker.run(job["job_id"])
            state = root / "economic"
            first, reused = evaluate(
                alpha_state_dir=alpha_state, job_id=job["job_id"],
                manifest_path=manifest, evaluation_state_dir=state,
                minimum_trades=1, maximum_drawdown_ratio="0.50")
            self.assertFalse(reused)
            directory = state / first["evaluation_id"]
            (directory / "receipt.json").unlink()
            recovered, reused = evaluate(
                alpha_state_dir=alpha_state, job_id=job["job_id"],
                manifest_path=manifest, evaluation_state_dir=state,
                minimum_trades=1, maximum_drawdown_ratio="0.50")
            self.assertFalse(reused)
            self.assertEqual(first, recovered)
            second, reused = evaluate(
                alpha_state_dir=alpha_state, job_id=job["job_id"],
                manifest_path=manifest, evaluation_state_dir=state,
                minimum_trades=1, maximum_drawdown_ratio="0.50")
            self.assertTrue(reused)
            self.assertEqual(first, second)
            self.assertEqual(first["evaluation_class"],
                             "FROZEN_MODEL_UNTOUCHED_HOLDOUT_CAUSAL_SIMULATION")
            self.assertFalse(first["live_authority"])
            proposal = first["next_unseen_session_interventions"]["proposals"]["DLINEAR"]
            self.assertEqual(proposal["memory_action"], "RECORD_PROPOSAL_ONLY")
            self.assertFalse(proposal["strategy_change_applied"])
            self.assertTrue(proposal["same_holdout_reuse_for_selection_prohibited"])
            self.assertTrue(proposal["engineer_review_required"])
            self.assertTrue(proposal["proposal_version"].startswith("sha256:"))
            self.assertEqual({"request.json", "trace.json", "result.json", "receipt.json"},
                             {path.name for path in directory.iterdir()})
            trace = json.loads((directory / "trace.json").read_text(encoding="utf-8"))
            self.assertEqual(trace["walk_forward_semantics"],
                             "FROZEN_TRAIN_MODEL_SEQUENTIAL_UNTOUCHED_HOLDOUT_NO_REFIT")
            result_path = directory / "result.json"
            result_path.write_bytes(result_path.read_bytes() + b" ")
            with self.assertRaisesRegex(AlphaInputError, "identity differs"):
                evaluate(alpha_state_dir=alpha_state, job_id=job["job_id"],
                         manifest_path=manifest, evaluation_state_dir=state,
                         minimum_trades=1, maximum_drawdown_ratio="0.50")

    def test_archive_from_different_selection_is_rejected(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = fixture(root, rows=200)
            payload, _selection = build_alpha_input(manifest, bars=160)
            source, _costs = _source(payload)
            self.assertEqual(len(source.closes), 160)
            alpha_state = root / "alpha"
            worker = _durable_worker(alpha_state)
            job, _created = worker.enqueue(payload, steps=1, backends=("DLINEAR",))
            worker.run(job["job_id"])
            # Replacing the manifest tail changes the authenticated job input and
            # must fail rather than silently evaluate a different market cut.
            changed = json.loads(manifest.read_text(encoding="utf-8"))
            changed["market"] = "ETH_USDT"
            manifest.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(HistoricalArchiveError, "identity"):
                evaluate(alpha_state_dir=alpha_state, job_id=job["job_id"],
                         manifest_path=manifest, evaluation_state_dir=root / "economic")


if __name__ == "__main__":
    unittest.main()
