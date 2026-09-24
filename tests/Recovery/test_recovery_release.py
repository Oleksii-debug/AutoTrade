import unittest

from mvp.autotrade_mvp.recovery_release import (
    RecoveryReleaseEvidence,
    qualify_recovery_release,
)


class RecoveryReleaseTests(unittest.TestCase):
    def evidence(self, **changes):
        values = dict(
            source_revision="source-sha",
            package_hash="package-sha256",
            backup_artifact_hash="backup-sha256",
            clean_restore_verified=True,
            backup_integrity_verified=True,
            journal_replay_equivalent=True,
            checkpoint_resume_equivalent=True,
            unknown_send_reconciled=True,
            provider_state_reconciled_after_restart=True,
            stale_owner_fenced=True,
            credential_rotation_exercised=True,
            rollback_verified=True,
            windows_restart_verified=True,
            evidence_ids=("backup-1", "restore-1", "crash-1", "rollback-1"),
        )
        values.update(changes)
        return RecoveryReleaseEvidence(**values)

    def test_complete_recovery_evidence_passes(self):
        result = qualify_recovery_release(self.evidence())
        self.assertTrue(result.passed)
        self.assertEqual(result.reasons, ())

    def test_each_missing_gate_is_named_and_blocks(self):
        result = qualify_recovery_release(
            self.evidence(
                unknown_send_reconciled=False,
                stale_owner_fenced=False,
                windows_restart_verified=False,
            )
        )
        self.assertFalse(result.passed)
        self.assertIn("missing_unknown_send_reconciled", result.reasons)
        self.assertIn("missing_stale_owner_fenced", result.reasons)
        self.assertIn("missing_windows_restart_verified", result.reasons)

    def test_green_backup_without_restore_is_not_release_evidence(self):
        result = qualify_recovery_release(self.evidence(clean_restore_verified=False))
        self.assertFalse(result.passed)
        self.assertEqual(result.reasons, ("missing_clean_restore_verified",))

    def test_evidence_identity_is_required(self):
        with self.assertRaisesRegex(ValueError, "evidence_ids"):
            qualify_recovery_release(self.evidence(evidence_ids=()))


if __name__ == "__main__":
    unittest.main()
