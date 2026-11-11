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
            "reason":"TEST", "analysis":{"finding":"costs"}}}), encoding="utf-8")


class LearningFleetDiagnosticsTests(unittest.TestCase):
    def test_projection_has_learning_analysis_and_no_authority(self):
        with TemporaryDirectory() as directory:
            source=Path(directory)/"source.json"; checkpoint(source)
            value=fleet_snapshot([("btc",source)])
            self.assertEqual(value["total_completed_episodes"],3)
            self.assertEqual(value["learners"][0]["learning_cycle"]["analysis"]["finding"],"costs")
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
