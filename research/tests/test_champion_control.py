from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.autotrade_research.learning.champion import (
    CandidateApproval,
    ChampionRegistry,
    PromotionConflict,
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def approval(candidate="candidate-a", valid_days=1, status="PASS"):
    return CandidateApproval.create(
        candidate_id=candidate,
        artifact_hash=f"sha256:{candidate}",
        evidence_id=f"evidence:{candidate}",
        evidence_valid_until=BASE + timedelta(days=valid_days),
        evaluation_status=status,
        retention_passed=True,
        risk_passed=True,
        authority_scope_id="paper-scope",
    )


class ChampionRegistryTests(unittest.TestCase):
    def test_atomic_initial_promotion_changes_future_pointer(self):
        with TemporaryDirectory() as directory:
            registry = ChampionRegistry(Path(directory) / "champion.sqlite3")
            state = registry.promote(
                approval(), expected_generation=0, now=BASE,
                open_position_count=0, existing_position_policy=None,
            )
            self.assertEqual(state.generation, 1)
            self.assertEqual(state.champion_candidate_id, "candidate-a")

    def test_stale_generation_cannot_overwrite_new_champion(self):
        with TemporaryDirectory() as directory:
            registry = ChampionRegistry(Path(directory) / "champion.sqlite3")
            registry.promote(approval(), expected_generation=0, now=BASE, open_position_count=0, existing_position_policy=None)
            with self.assertRaises(PromotionConflict):
                registry.promote(approval("candidate-b"), expected_generation=0, now=BASE, open_position_count=0, existing_position_policy=None)

    def test_expired_evidence_blocks_promotion(self):
        with TemporaryDirectory() as directory:
            registry = ChampionRegistry(Path(directory) / "champion.sqlite3")
            with self.assertRaises(ValueError):
                registry.promote(
                    approval(valid_days=0), expected_generation=0,
                    now=BASE + timedelta(seconds=1),
                    open_position_count=0, existing_position_policy=None,
                )

    def test_failed_evaluation_blocks_promotion(self):
        with TemporaryDirectory() as directory:
            registry = ChampionRegistry(Path(directory) / "champion.sqlite3")
            with self.assertRaises(ValueError):
                registry.promote(
                    approval(status="FAIL"), expected_generation=0,
                    now=BASE, open_position_count=0, existing_position_policy=None,
                )

    def test_open_positions_require_explicit_policy(self):
        with TemporaryDirectory() as directory:
            registry = ChampionRegistry(Path(directory) / "champion.sqlite3")
            with self.assertRaises(ValueError):
                registry.promote(
                    approval(), expected_generation=0, now=BASE,
                    open_position_count=2, existing_position_policy=None,
                )

    def test_rollback_changes_future_pointer_without_erasing_history(self):
        with TemporaryDirectory() as directory:
            registry = ChampionRegistry(Path(directory) / "champion.sqlite3")
            registry.promote(
                approval("candidate-a"), expected_generation=0, now=BASE,
                open_position_count=0, existing_position_policy=None,
            )
            registry.promote(
                approval("candidate-b"), expected_generation=1, now=BASE,
                open_position_count=0, existing_position_policy=None,
            )
            state = registry.rollback(
                target_generation=1, expected_generation=2, now=BASE,
                open_position_count=1,
                existing_position_policy="manage-under-original-exit-owner",
            )
            self.assertEqual(state.generation, 3)
            self.assertEqual(state.champion_candidate_id, "candidate-a")
            self.assertEqual(
                state.existing_position_policy,
                "manage-under-original-exit-owner",
            )
            self.assertEqual(
                [row["action"] for row in registry.history()],
                ["PROMOTE", "PROMOTE", "ROLLBACK"],
            )


if __name__ == "__main__":
    unittest.main()
