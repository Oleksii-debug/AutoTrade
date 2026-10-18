from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from tools.create_goal_directed_organism import build_manifest
from tools.evaluate_historical_objectives import evaluate_campaign


NOW = datetime(2026, 10, 18, 16, 0, tzinfo=timezone.utc)


def report(*, bounded: bool = True, drawdown: str = "0.02") -> dict:
    value = {
        "status": "INTERNAL_TWO_ACCOUNT_HISTORICAL_CUT_NOT_PRODUCT_QUALIFICATION",
        "symbol": "BTCUSDT",
        "interval": "1h",
        "accounts": {
            "1000": {
                "cash_baseline_equity_usd": "1000",
                "agent_mark_pnl_usd": "249",
                "agent_max_mark_drawdown_fraction": drawdown,
                "completed_episodes": 24,
            }
        },
    }
    if bounded:
        value["qualification_cut"] = {
            "scope": "BOUNDED_CAUSAL_PREFIX_NOT_FULL_MONTH_PERFORMANCE",
            "episode_limit": 24,
        }
    return value


class HistoricalObjectiveMatrixTests(unittest.TestCase):
    def manifest(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return build_manifest(
            root=Path(temporary.name),
            symbols=("BTC_USDT",),
            created_at=NOW,
        )

    def test_bounded_cut_is_inconclusive_for_every_profile(self):
        matrix = evaluate_campaign(
            campaign=report(),
            campaign_sha256="sha256:" + "1" * 64,
            manifest=self.manifest(),
            observed_at=NOW,
        )
        receipts = matrix["accounts"]["1000"]["objective_receipts"]
        self.assertFalse(matrix["horizon_complete"])
        by_profile = {item["profile_id"]: item["status"] for item in receipts}
        self.assertEqual(by_profile["TARGET_25"], "INCONCLUSIVE")
        self.assertEqual(by_profile["EVENT_DRIVEN_POSITION"], "INCONCLUSIVE")
        self.assertEqual(
            by_profile["OPPORTUNITY_RESEARCH"], "RISK_LIMIT_BREACHED"
        )
        self.assertFalse(matrix["automatic_champion_promotion"])
        self.assertFalse(matrix["network_orders"])

    def test_full_cut_measures_after_costs_against_exact_targets(self):
        matrix = evaluate_campaign(
            campaign=report(bounded=False),
            campaign_sha256="sha256:" + "2" * 64,
            manifest=self.manifest(),
            observed_at=NOW,
        )
        receipts = {
            item["profile_id"]: item
            for item in matrix["accounts"]["1000"]["objective_receipts"]
        }
        self.assertEqual(receipts["BASELINE"]["status"], "MEASURED_NO_TARGET")
        self.assertEqual(receipts["TARGET_25"]["status"], "TARGET_MISSED")
        self.assertEqual(
            receipts["TARGET_25"]["net_return_after_costs_fraction"], "0.249"
        )
        self.assertEqual(receipts["TARGET_100"]["status"], "TARGET_MISSED")

    def test_risk_breach_dominates_target_and_manifest_tamper_fails(self):
        manifest = self.manifest()
        breached = evaluate_campaign(
            campaign=report(bounded=False, drawdown="0.051"),
            campaign_sha256="sha256:" + "3" * 64,
            manifest=manifest,
            observed_at=NOW,
        )
        statuses = {
            item["status"]
            for item in breached["accounts"]["1000"]["objective_receipts"]
        }
        self.assertEqual(statuses, {"RISK_LIMIT_BREACHED"})
        manifest["workstreams"][0]["paid_model_budget_usd"] = "1"
        with self.assertRaisesRegex(ValueError, "digest"):
            evaluate_campaign(
                campaign=report(),
                campaign_sha256="sha256:" + "4" * 64,
                manifest=manifest,
                observed_at=NOW,
            )


if __name__ == "__main__":
    unittest.main()
