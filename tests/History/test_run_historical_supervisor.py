"""A restarted supervisor learns from completed months without replay orders."""
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
from tests.History.test_run_historical_zero import full_month_fixture
from tools.historical_public_klines import collect_month
from tools.run_historical_supervisor import supervise


class SupervisorTests(unittest.TestCase):
    def test_restart_advances_only_completed_months_and_detects_changed_history(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            workspace = root / "workspace"
            store = ArtifactStore(data / ".artifact-store")
            for month in ("2024-01", "2024-02", "2024-03", "2024-04"):
                raw, checksum, _ = full_month_fixture(
                    month=month,
                    close_at=lambda index: ("100", "100", "102", "102")[index % 4],
                )
                collect_month(symbol="BTCUSDT", interval="1h", month=month,
                              output=data, artifact_store=store,
                              fetch=lambda url, _, r=raw, c=checksum:
                              c if url.endswith(".CHECKSUM") else r)
            options = dict(data_root=data, workspace=workspace,
                           symbol="BTCUSDT", first_holdout="2024-03",
                           episode_limit=2, max_cuts=1, auto_refresh=False,
                           clock=lambda: datetime(2024, 5, 1, tzinfo=timezone.utc))
            first = supervise(**options)
            self.assertEqual(first["next_holdout_month"], "2024-04")
            second = supervise(**options)
            self.assertEqual(second["next_holdout_month"], "2024-05")
            self.assertEqual(second["total_completed"], 2)
            waiting = supervise(**options)
            self.assertEqual(waiting["completed_this_run"], 0)
            state = json.loads((workspace / "supervisor" / "BTCUSDT" /
                                "state.json").read_text())
            self.assertNotEqual(state["completed"][0]["model_sha256"],
                                state["completed"][1]["model_sha256"])
            report = workspace / "reports" / "BTCUSDT-2024-03-2024-03-2.json"
            report.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "report missing or changed"):
                supervise(**options)

    def test_future_cut_waits_and_invalid_config_does_not_create_state(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            options = dict(data_root=root / "data", workspace=root / "state",
                           symbol="BTCUSDT", first_holdout="2024-03",
                           max_cuts=1, auto_refresh=False,
                           clock=lambda: datetime(2024, 3, 15, tzinfo=timezone.utc))
            self.assertEqual(supervise(**options)["completed_this_run"], 0)
            with self.assertRaisesRegex(ValueError, "training_months"):
                supervise(**dict(options, training_months=0))
            self.assertFalse((root / "state" / "supervisor" / "BTCUSDT" /
                              "state.json").exists())

    def test_unpublished_month_waits_without_advancing_or_running_accounts(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = []
            with patch("tools.run_historical_supervisor.update_history", return_value={
                "complete": False, "entries": [{"status": "MISSING"}],
            }), patch("tools.run_historical_supervisor.campaign") as run_account:
                result = supervise(
                    data_root=root / "data", workspace=root / "state",
                    symbol="BTCUSDT", first_holdout="2024-03", max_cuts=1,
                    clock=lambda: datetime(2024, 4, 1, tzinfo=timezone.utc),
                    progress=events.append,
                )
                run_account.assert_not_called()
            self.assertEqual(result["next_holdout_month"], "2024-03")
            self.assertEqual(events[0]["status"], "WAITING_FOR_VERIFIED_PUBLIC_ARCHIVE")


if __name__ == "__main__":
    unittest.main()
