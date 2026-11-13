from datetime import UTC, datetime, timedelta
import json
import unittest

import pytest

from tools import run_whitebit_course_supervisor as course
from tools.run_whitebit_course_supervisor import (
    ACTIVE_FAMILIES, completed_window, partition_counts, _compact,
)


class CourseSupervisorTests(unittest.TestCase):
    def test_only_full_analysis_families_are_active(self):
        self.assertEqual(ACTIVE_FAMILIES,
            ("TREND_QUASIMODO_TOP_DOWN", "RANGE_DEVIATION"))
        self.assertNotIn("TREND_QUASIMODO", ACTIVE_FAMILIES)

    def test_window_uses_only_completed_aligned_candles(self):
        start, end = completed_window(datetime(2026, 11, 12, 10, 7, tzinfo=UTC), 4000)
        self.assertEqual(end % 900, 0)
        self.assertLess(end, int(datetime(2026, 11, 12, 10, 7, tzinfo=UTC).timestamp()))
        self.assertEqual((end - start) // 900 + 1, 4000)

    def test_partitions_retain_substantial_locked_final(self):
        train, development, final = partition_counts(6000)
        self.assertEqual(train + development + final, 6000)
        self.assertGreaterEqual(train, 2000)
        self.assertGreaterEqual(development, 1000)
        self.assertGreaterEqual(final, 1000)

    def test_checkpoint_projection_cannot_grant_authority(self):
        value = _compact({"strategy_schema": "x", "train": {
            "market_count": 3, "closed_cycles": 1, "wins": 1, "losses": 0,
            "win_rate": "1", "net_pnl": "1", "fees": "1", "funding": "0"},
            "train_rejection_reasons": [], "development": None,
            "development_rejection_reasons": [], "final": None,
            "final_rejection_reasons": ["LOCKED"], "development_consumed": False,
            "final_consumed": False, "prospective_qualification": False,
            "result_sha256": "a" * 64,
            "train_process_review": {"schema": "process-v1", "source_partition": "TRAIN_ONLY",
                "phase": "PAUSED_ANALYZING", "episode_count": 3, "market_count": 1,
                "journal_complete": True, "incomplete_journal_count": 0,
                "maximum_observed_consecutive_losses": 3,
                "pause_reasons": ["CONSECUTIVE_LOSS_PAUSE_REQUIRED"],
                "resume_condition": "REVIEW_CHECKPOINT_WRITTEN_AND_COOLDOWN_ELAPSED",
                "same_frozen_strategy_required": True,
                "loss_streak_pause_events": [{"market": "BTC_PERP"}],
                "daily_process_events": [], "weekly_process_events": [],
                "review_sha256": "b" * 64}})
        self.assertFalse(value["network_orders"])
        self.assertFalse(value["trading_authority"])
        self.assertFalse(value["automatic_promotion"])
        self.assertEqual(value["paid_model_calls"], 0)
        self.assertEqual(value["train_process_review"]["loss_streak_pause_event_count"], 1)
        self.assertEqual(value["train_process_review"]["weekly_process_event_count"], 0)
        self.assertNotIn("loss_streak_pause_events", value["train_process_review"])
        self.assertIsNone(value["train"]["signal_count"])

    def test_checkpoint_projection_preserves_aggregate_diagnostics_only(self):
        value = _compact({"strategy_schema": "range-v1", "train": {
            "market_count": 2, "closed_cycles": 0, "wins": 0, "losses": 0,
            "win_rate": None, "net_pnl": "0", "fees": "0", "funding": "0",
            "signal_count": 2, "detected_setups": 4, "expired_setups": 1,
            "detection_rejections": {"NO_REENTRY_SWEEP": 12},
            "confirmation_rejections": {"STOP_INVALIDATED": 3},
            "pretrade_holds": 1, "pretrade_hold_reasons": {"REWARD_RISK": 1},
            "markets": {"BTC_PERP": {"signal_count": 2}}},
            "train_rejection_reasons": ["MINIMUM_TRAIN_CYCLES_NOT_MET"],
            "development": None, "development_rejection_reasons": [],
            "final": None, "final_rejection_reasons": ["LOCKED"],
            "development_consumed": False, "final_consumed": False,
            "prospective_qualification": False, "result_sha256": "a" * 64})
        self.assertEqual(value["train"]["detection_rejections"],
                         {"NO_REENTRY_SWEEP": 12})
        self.assertNotIn("markets", value["train"])


if __name__ == "__main__":
    unittest.main()


def test_supervisor_resumes_saved_screen_and_finishes_analysis_before_cooldown(tmp_path, monkeypatch):
    path = tmp_path / "checkpoint.json"
    events = tmp_path / "course-session-events.jsonl"
    screen_calls = []
    family = {
        "train": {"pretrade_hold_reasons": {"REWARD_RISK": 2}},
        "train_rejection_reasons": ["TRAIN_NO_POSITIVE_AFTER_COST_PNL"],
        "train_process_review": {"phase": "PAUSED_ANALYZING",
                                 "pause_reasons": ["CONSECUTIVE_LOSS_PAUSE_REQUIRED"],
                                 "review_sha256": "review"},
    }
    episode = {"exit_reason": "STOP", "profitable": False,
               "pnl_after_costs": "-0.10", "fees": "0.11", "funding": "0.00"}

    def screen(*, state_dir, bars, now):
        assert json.loads(path.read_bytes())["status"] == "RUNNING"
        screen_calls.append(now)
        return {"schema": course.SCHEMA, "window": {"bars": bars}, "manifests": {},
                "observed_at": now.isoformat(), "families": {"RANGE_DEVIATION": family},
                "train_episodes": {"RANGE_DEVIATION": {"BTC_PERP": [episode]}}}

    review = course._review_training

    def interrupt(full):
        assert json.loads(path.read_bytes())["status"] == "ANALYZING"
        assert (tmp_path / "pending-course-screen-1.json").exists()
        raise KeyboardInterrupt

    monkeypatch.setattr(course, "_screen_cycle", screen)
    monkeypatch.setattr(course, "_review_training", interrupt)
    args = ["--state-dir", str(tmp_path), "--once", "--interval-seconds", "3600"]
    with pytest.raises(KeyboardInterrupt):
        course.main(args)
    saved = json.loads(path.read_bytes())
    assert saved["pending_cycle"]["phase"] == "ANALYZING"
    assert "last_result" not in saved

    def resume(full):
        assert json.loads(path.read_bytes())["status"] == "ANALYZING"
        return review(full)

    monkeypatch.setattr(course, "_review_training", resume)
    assert course.main(args) == 0
    assert len(screen_calls) == 1
    final = json.loads(path.read_bytes())
    assert final["status"] == "STOPPED"
    assert final["cycle_count"] == 1
    result = final["last_result"]
    reviewed = result["training_review"]["families"]["RANGE_DEVIATION"]
    assert reviewed["loss_and_cost_by_exit"]["STOP"]["fees_or_funding_turned_win_to_loss"] == 1
    assert reviewed["pretrade_hold_reasons"] == {"REWARD_RISK": 2}
    assert result["session_lifecycle"]["review_sha256"] == result["training_review"]["review_sha256"]
    receipt = json.loads((tmp_path / "runs" / f"{result['cycle_sha256']}.json").read_bytes())
    assert receipt["train_episodes"]["RANGE_DEVIATION"]["BTC_PERP"] == [episode]
    rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    assert [row["phase"] for row in rows] == ["RUNNING", "PAUSED", "ANALYZING", "COOLDOWN"]
    assert datetime.fromisoformat(final["next_run_at"]) == (
        datetime.fromisoformat(rows[-1]["observed_at"]) + timedelta(hours=1)
    )
    assert not (tmp_path / "pending-course-screen-1.json").exists()
    assert course.main(args) == 0
    assert len(screen_calls) == 1

