from datetime import UTC, datetime, timedelta
from decimal import Decimal
import unittest

from research.autotrade_research.learning.course_process_discipline import (
    process_curriculum_packet, review_course_episodes,
)


def episode(index: int, pnl: str) -> dict:
    entered = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index)
    value = Decimal(pnl)
    return {"detected_at": (entered - timedelta(minutes=15)).isoformat(),
        "entered_at": entered.isoformat(),
        "exited_at": (entered + timedelta(minutes=30)).isoformat(),
        "direction": "LONG", "entry_price": "100", "stop_price": "99",
        "target_price": "102", "planned_reward_risk": "2",
        "exit_reason": "TEST", "pnl_after_costs": pnl,
        "return_on_start_equity": str(value / Decimal("1000")),
        "fees": "0.2", "funding": "0", "profitable": value > 0}


class CourseProcessDisciplineTests(unittest.TestCase):
    def test_packet_is_content_free_and_digest_bound(self):
        packet = process_curriculum_packet()
        self.assertEqual(packet["source"]["file_count"], 100)
        self.assertFalse(packet["source"]["transcript_text_embedded"])
        self.assertEqual(len(packet["source"]["index_sha256"]), 64)
        self.assertEqual(len(packet["packet_sha256"]), 64)

    def test_three_losses_force_pause_and_analysis_without_strategy_mutation(self):
        result = review_course_episodes([{"market": "BTC_PERP", "episodes": [
            episode(0, "-10"), episode(1, "-10"), episode(2, "-10")]}])
        self.assertEqual(result["phase"], "PAUSED_ANALYZING")
        self.assertIn("CONSECUTIVE_LOSS_PAUSE_REQUIRED", result["pause_reasons"])
        self.assertEqual(len(result["loss_streak_pause_events"]), 1)
        self.assertTrue(result["same_frozen_strategy_required"])
        self.assertFalse(result["automatic_strategy_mutation"])
        self.assertFalse(result["development_consumed"])

    def test_complete_bounded_series_is_eligible_to_continue(self):
        result = review_course_episodes([{"market": "ETH_PERP", "episodes": [
            episode(0, "5"), episode(1, "-2"), episode(2, "5")]}])
        self.assertEqual(result["phase"], "RUNNING_ELIGIBLE")
        self.assertEqual(result["pause_reasons"], [])
        self.assertTrue(result["journal_complete"])

    def test_incomplete_journal_fails_closed(self):
        value = episode(0, "5"); del value["exit_reason"]
        result = review_course_episodes([{"market": "SOL_PERP", "episodes": [value]}])
        self.assertEqual(result["phase"], "COLLECTING")
        self.assertFalse(result["journal_complete"])
        self.assertIn("INCOMPLETE_TRADE_JOURNAL", result["pause_reasons"])


if __name__ == "__main__":
    unittest.main()
