from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from tools.run_learning_fleet_diagnostics import fleet_snapshot, main


def checkpoint(path, phase="RUNNING"):
    path.write_text(json.dumps({"mode":"ZERO", "episode_count":3,
        "invalid_episode_count":0, "model_version_count":2,
        "last_available_at":"2026-01-01T00:00:00+00:00",
        "latest_model_sha256":"sha256:"+"a"*64,
        "network_orders":False, "trading_authority":False,
        "learning_cycle":{"market":"BTC_USDT", "phase":phase, "cycle":1,
            "trials":0, "episodes":3, "net":"-1", "drawdown":"1",
            "reason":"TEST", "analysis":{"finding":"costs"},
            "config":{"fee_bps":"10", "slippage_bps":"5",
                "short_carry_bps_per_day":"2", "notional":"100",
                "outcome_horizon_seconds":300, "signal_lookback_seconds":60,
                "max_observation_gap_seconds":180,
                "feature_mode":"TOP_BOOK_IMBALANCE", "secret":"excluded"},
            "evaluation":{"pending":{"candidate_action":"SELL",
                "incumbent_action":"BUY", "available_at":"2026-01-01T00:05:00+00:00",
                "ask":"secret-free-but-unnecessary"}}}}), encoding="utf-8")


class LearningFleetDiagnosticsTests(unittest.TestCase):
    def test_projection_has_learning_analysis_and_no_authority(self):
        with TemporaryDirectory() as directory:
            source=Path(directory)/"source.json"; checkpoint(source)
            value=fleet_snapshot([("btc",source)])
            self.assertEqual(value["total_completed_episodes"],3)
            self.assertEqual(value["learners"][0]["learning_cycle"]["analysis"]["finding"],"costs")
            cycle=value["learners"][0]["learning_cycle"]
            self.assertEqual(cycle["cost_and_horizon_config"]["short_carry_bps_per_day"],"2")
            self.assertEqual(cycle["cost_and_horizon_config"]["feature_mode"],
                             "TOP_BOOK_IMBALANCE")
            self.assertEqual(cycle["pending_actions"]["candidate"],"SELL")
            self.assertNotIn("secret",json.dumps(cycle["cost_and_horizon_config"]).lower())
            self.assertNotIn("ask",cycle["pending_actions"])
            self.assertFalse(value["network_orders"])
            self.assertNotIn("password",json.dumps(value).lower())

    def test_unchanged_restart_does_not_duplicate_event(self):
        with TemporaryDirectory() as directory:
            root=Path(directory); source=root/"source.json"; state=root/"state"
            checkpoint(source)
            argv=["--source",f"btc={source}","--state-dir",str(state),"--once"]
            self.assertEqual(main(argv),0); self.assertEqual(main(argv),0)
            log=(state/"agent-fleet-diagnostics.jsonl").read_text().splitlines()
            self.assertEqual(len(log),1)
            checkpoint(source,"EVALUATING"); self.assertEqual(main(argv),0)
            self.assertEqual(len((state/"agent-fleet-diagnostics.jsonl").read_text().splitlines()),2)


if __name__ == "__main__":
    unittest.main()

