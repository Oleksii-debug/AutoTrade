"""A real, bounded Plan10 pipeline cut: data -> ZERO -> learning -> restart -> evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore
from tools.historical_public_klines import collect_month
from tools.run_historical_e2e_qualification import run_qualification_cut
from tests.History.test_run_historical_zero import full_month_fixture


class HistoricalE2EQualificationCutTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("AUTOTRADE_RUN_SLOW_E2E") == "1",
        "opt-in real campaign fixture; set AUTOTRADE_RUN_SLOW_E2E=1",
    )
    def test_real_bounded_campaign_restart_and_dual_diagnostics_pass(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            store = ArtifactStore(data / ".artifact-store")
            for month in ("2024-01", "2024-02", "2024-03"):
                raw, checksum, _ = full_month_fixture(
                    month=month,
                    close_at=lambda index: ("100", "100", "102", "102")[index % 4],
                )
                collect_month(
                    symbol="BTCUSDT",
                    interval="1h",
                    month=month,
                    output=data,
                    artifact_store=store,
                    fetch=lambda url, _, body=raw, check=checksum: (
                        check if url.endswith(".CHECKSUM") else body
                    ),
                )

            workspace = root / "workspace"
            result = run_qualification_cut(
                data_root=data,
                workspace=workspace,
                source_sha="a" * 40,
                symbol="BTCUSDT",
                train_from="2024-01",
                train_through="2024-01",
                validate_from="2024-02",
                validate_through="2024-02",
                holdout_from="2024-03",
                holdout_through="2024-03",
                episode_limit=12,
            )

            self.assertEqual(result["status"], "PASS")
            self.assertTrue(all(check["status"] == "PASS" for check in result["checks"]))
            model = json.loads(next((workspace / "models").glob("*.json")).read_text())
            self.assertEqual(model["schema"], "autotrade-closed-simulation-training-v2")
            report = json.loads(next((workspace / "reports").glob("*.json")).read_text())
            self.assertEqual(
                report["qualification_cut"]["scope"],
                "BOUNDED_CAUSAL_PREFIX_NOT_FULL_MONTH_PERFORMANCE",
            )
            self.assertEqual(
                {account["status"] for account in report["accounts"].values()},
                {"BOUNDED_CAUSAL_PREFIX_COMPLETED_NOT_FULL_MONTH"},
            )
            restart = json.loads(
                (workspace / "qualification" / "restart-evidence.json").read_text()
            )
            self.assertTrue(all(
                row["journal_sequence_before"] == row["journal_sequence_after"]
                and row["checkpoint_sha256_before"] == row["checkpoint_sha256_after"]
                for row in restart["accounts"].values()
            ))
            self.assertEqual(len(list((workspace / "diagnostics").glob(
                "autotrade-agent-report*.zip"))), 2)
            self.assertEqual(len(list((workspace / "diagnostics").glob(
                "autotrade-technical-diagnostics*.zip"))), 1)


if __name__ == "__main__":
    unittest.main()
