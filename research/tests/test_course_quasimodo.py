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

    def test_locked_screen_keeps_final_unread_after_train_failure(self):
        value = screen(bars(2500), train_count=1400, development_count=550,
                       dataset_sha256="a" * 64, minimum_cycles=3)
        self.assertFalse(value["final_consumed"])
        self.assertIn("MINIMUM_TRAIN_CYCLES_NOT_MET", value["train_rejection_reasons"])
        self.assertFalse(value["network_orders"])


if __name__ == "__main__":
    unittest.main()
