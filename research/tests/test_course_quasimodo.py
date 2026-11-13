from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.autotrade_research.strategies.course_quasimodo import (
    CourseConfiguration, detect_sweep, simulate,
)
from tools.index_trading_course_transcripts import build_index
from tools.screen_whitebit_course_quasimodo import screen
from tools.screen_whitebit_course_quasimodo_universe import _aggregate, screen_universe


def bars(count=1200):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    result = []
    price = Decimal("100")
    for index in range(count):
        price *= Decimal("1.00005")
        opened = start + timedelta(minutes=15 * index)
        close = price.quantize(Decimal("0.00000001"))
        result.append({"opened_at": opened, "closed_at": opened + timedelta(minutes=15),
            "open": str(close), "high": str(close * Decimal("1.001")),
            "low": str(close * Decimal("0.999")), "close": str(close), "volume": "1000"})
    return result


class CourseQuasimodoTests(unittest.TestCase):
    def test_universe_preserves_market_and_total_diagnostic_counters(self):
        base = {"closed_cycles": 0, "wins": 0, "losses": 0, "net_pnl": "0",
            "fees": "0", "funding": "0", "exit_reasons": {},
            "evidence_sha256": "a" * 64, "signal_count": 0,
            "pretrade_holds": 0, "pretrade_hold_reasons": {}}
        btc = {**base, "signal_count": 3, "pretrade_holds": 2,
            "pretrade_hold_reasons": {"GROSS_REWARD_RISK": 2}}
        eth = {**base, "signal_count": 1, "pretrade_holds": 3,
            "pretrade_hold_reasons": {"GROSS_REWARD_RISK": 1, "NET_REWARD": 2}}
        result = _aggregate({"ETH_PERP": eth, "BTC_PERP": btc})
        self.assertEqual(result["pretrade_hold_reasons"],
                         {"GROSS_REWARD_RISK": 3, "NET_REWARD": 2})
        self.assertEqual(result["markets"]["BTC_PERP"]["signal_count"], 3)

    def test_private_index_has_hashes_and_no_transcript_text(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "one.txt").write_text("risk liquidity Quasimodo", encoding="utf-8")
            value = build_index(root)
            self.assertEqual(value["file_count"], 1)
            self.assertFalse(value["contains_transcript_text"])
            self.assertEqual(len(value["files"][0]["sha256"]), 64)
            self.assertNotIn("text", value["files"][0])

    def test_frozen_strategy_is_inert_without_full_pattern(self):
        result = simulate(bars(), configuration=CourseConfiguration())
        self.assertEqual(result["closed_cycles"], 0)
        self.assertEqual(result["frozen_until_cycles"], 100)
        self.assertFalse(result["automatic_strategy_mutation"])
        self.assertFalse(result["trading_authority"])

    def test_detector_requires_full_context(self):
        self.assertIsNone(detect_sweep(bars(100), CourseConfiguration()))

    def test_top_down_gate_needs_completed_daily_history(self):
        self.assertIsNone(detect_sweep(bars(1200),
            CourseConfiguration(require_true_top_down=True)))

    def test_locked_screen_keeps_final_unread_after_train_failure(self):
        value = screen(bars(2500), train_count=1400, development_count=550,
                       dataset_sha256="a" * 64, minimum_cycles=3)
        self.assertFalse(value["final_consumed"])
        self.assertIn("MINIMUM_TRAIN_CYCLES_NOT_MET", value["train_rejection_reasons"])
        self.assertFalse(value["network_orders"])

    def test_universe_screen_keeps_later_partitions_locked(self):
        value = screen_universe([{"market": "BTC_PERP", "bars": bars(2500),
            "dataset_identity_sha256": "b" * 64}], train_count=1400,
            development_count=550, configuration=CourseConfiguration(),
            minimum_train_cycles=3)
        self.assertFalse(value["development_consumed"])
        self.assertFalse(value["final_consumed"])
        self.assertIn("MINIMUM_TRAIN_CYCLES_NOT_MET", value["train_rejection_reasons"])


if __name__ == "__main__":
    unittest.main()

