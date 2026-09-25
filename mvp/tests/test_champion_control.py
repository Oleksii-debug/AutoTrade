from pathlib import Path
import tempfile
import unittest

from mvp.autotrade_mvp.champion_control import (
    ChampionConflict,
    ChampionControlError,
    DurableChampionControl,
    PromotionDecision,
)
from mvp.autotrade_mvp.persistence import JournalStore


POLICY = "a" * 64
ENVELOPE = "b" * 64
SOURCE = "c" * 64


def decision(**overrides):
    values = dict(
        decision_id="promote-1",
        scope_id="strategy:alpha",
        candidate_version="candidate-v2",
        prior_version="champion-v1",
        result_refs=("evaluation:locked-1", "evaluation:forward-1"),
        envelope_digest=ENVELOPE,
        authority_policy_digest=POLICY,
        independent_gate_signer="science-gate:v1",
        gate_verdict="PASS",
        rollback_target="champion-v1",
        source_sha256=SOURCE,
    )
    values.update(overrides)
    return PromotionDecision(**values)


class ChampionControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "journal.sqlite"
        self.store = JournalStore(self.path)
        self.control = DurableChampionControl(
            journal=self.store,
            scope_id="strategy:alpha",
            initial_champion_version="champion-v1",
            authority_policy_digest=POLICY,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_promotion_atomically_switches_future_route_only(self):
        before = self.control.snapshot()
        promoted = self.control.promote(
            decision(),
            expected_generation=before.generation,
        )
        self.assertEqual(promoted.future_route_version, "candidate-v2")
        self.assertEqual(
            promoted.existing_position_management_version,
            "champion-v1",
        )
        self.assertTrue(promoted.existing_positions_need_explicit_migration)
        self.assertIn("champion-v1", promoted.retained_versions)
        self.assertIn("candidate-v2", promoted.retained_versions)

    def test_fail_or_inconclusive_cannot_promote(self):
        for verdict in ("FAIL", "INCONCLUSIVE"):
            with self.subTest(verdict=verdict):
                with self.assertRaisesRegex(
                    ChampionControlError,
                    "only independent PASS",
                ):
                    self.control.promote(
                        decision(
                            decision_id="p-" + verdict.lower(),
                            gate_verdict=verdict,
                        ),
                        expected_generation=self.control.snapshot().generation,
                    )

    def test_promotion_cannot_expand_or_change_hard_authority(self):
        with self.assertRaisesRegex(
            ChampionControlError,
            "cannot change hard authority policy",
        ):
            self.control.promote(
                decision(authority_policy_digest="d" * 64),
                expected_generation=self.control.snapshot().generation,
            )

    def test_stale_prior_champion_is_rejected(self):
        state = self.control.snapshot()
        with self.assertRaisesRegex(ChampionConflict, "prior version is stale"):
            self.control.promote(
                decision(prior_version="old-v0", rollback_target="old-v0"),
                expected_generation=state.generation,
            )

    def test_stale_generation_is_compare_and_swap_conflict(self):
        state = self.control.snapshot()
        first = self.control.promote(
            decision(),
            expected_generation=state.generation,
        )
        with self.assertRaisesRegex(ChampionConflict, "generation changed"):
            self.control.promote(
                decision(
                    decision_id="promote-racing",
                    candidate_version="candidate-v3",
                    prior_version="champion-v1",
                    rollback_target="champion-v1",
                ),
                expected_generation=state.generation,
            )
        self.assertEqual(
            self.control.snapshot().future_route_version,
            first.future_route_version,
        )

    def test_restart_replays_same_champion_and_retained_artifacts(self):
        promoted = self.control.promote(
            decision(),
            expected_generation=self.control.snapshot().generation,
        )
        restarted = DurableChampionControl(
            journal=JournalStore(self.path),
            scope_id="strategy:alpha",
            initial_champion_version="champion-v1",
            authority_policy_digest=POLICY,
        )
        replayed = restarted.snapshot()
        self.assertEqual(replayed, promoted)

    def test_rollback_changes_future_route_but_not_open_position_management(self):
        promoted = self.control.promote(
            decision(),
            expected_generation=self.control.snapshot().generation,
        )
        rolled = self.control.rollback(
            rollback_id="rollback-1",
            target_version="champion-v1",
            expected_generation=promoted.generation,
            reason_ref="monitor:operational-invariant-failure",
        )
        self.assertEqual(rolled.future_route_version, "champion-v1")
        self.assertEqual(
            rolled.existing_position_management_version,
            "champion-v1",
        )
        self.assertIn("candidate-v2", rolled.retained_versions)

    def test_unknown_rollback_target_fails_closed(self):
        with self.assertRaisesRegex(
            ChampionControlError,
            "not a retained champion artifact",
        ):
            self.control.rollback(
                rollback_id="rollback-unknown",
                target_version="never-qualified",
                expected_generation=self.control.snapshot().generation,
                reason_ref="monitor:test",
            )

    def test_existing_position_management_requires_explicit_compatibility_evidence(self):
        promoted = self.control.promote(
            decision(),
            expected_generation=self.control.snapshot().generation,
        )
        with self.assertRaisesRegex(
            ChampionControlError,
            "requires compatibility evidence",
        ):
            self.control.migrate_existing_position_management(
                migration_id="migrate-1",
                target_version="candidate-v2",
                expected_generation=promoted.generation,
                compatibility_evidence_refs=(),
            )
        migrated = self.control.migrate_existing_position_management(
            migration_id="migrate-2",
            target_version="candidate-v2",
            expected_generation=promoted.generation,
            compatibility_evidence_refs=("exit-policy:compatible-v2",),
        )
        self.assertEqual(
            migrated.existing_position_management_version,
            "candidate-v2",
        )
        self.assertFalse(migrated.existing_positions_need_explicit_migration)

    def test_restart_with_different_hard_policy_fails_closed(self):
        with self.assertRaisesRegex(
            ChampionControlError,
            "authority policy conflicts",
        ):
            DurableChampionControl(
                journal=JournalStore(self.path),
                scope_id="strategy:alpha",
                initial_champion_version="champion-v1",
                authority_policy_digest="e" * 64,
            )

    def test_invalid_digest_and_empty_evidence_are_rejected(self):
        with self.assertRaisesRegex(ChampionControlError, "SHA-256"):
            decision(envelope_digest="bad")
        with self.assertRaisesRegex(
            ChampionControlError,
            "requires evaluation result references",
        ):
            decision(result_refs=())


if __name__ == "__main__":
    unittest.main()
