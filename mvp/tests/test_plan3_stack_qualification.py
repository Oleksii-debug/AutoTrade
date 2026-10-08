"""Plan 3: executable provider-free cross-component qualification.

Real production modules are exercised together against a fresh temporary
JournalStore and explicit synthetic reconciliation. No brokerage connection,
financial transport, credentials or PAPER/LIVE endpoint can be reached.
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.backup import (
    create_backup, restore_backup, restore_requires_reconciliation, verify_backup,
)
from mvp.autotrade_mvp.diagnostics import build_diagnostic_snapshot
from mvp.autotrade_mvp.operator_observability import build_operator_observability
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.pipeline import run_vertical_slice
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.autotrade_mvp.recovery import (
    HostState, OutboundAttempt, RecoveryController,
)
from mvp.tests.test_backup_restore import _artifact_store
from mvp.tests.test_operator_observability import _signals, _snapshot
from mvp.tests.test_reconciliation_journal import reconciliation


class Plan3CrossComponentQualificationTests(unittest.TestCase):
    def test_recovery_restart_reconciliation_and_secret_safe_diagnostic_cut(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            journal = JournalStore(path)
            recovery = RecoveryController(
                owner_store=journal,
                owner_scope="PAPER:test-account",
            )
            owner = recovery.start("host-a")
            self.assertEqual(recovery.state, HostState.RECOVERING)
            ui = _snapshot()
            ui["account_id"] = "test-account"
            ui["host_id"] = "host-a"
            ui["risk"]["active_reservations"] = [
                {"api_secret": "never-publish-this-recovery-secret"}
            ]
            before = build_operator_observability(
                ui_snapshot=ui, recovery=recovery, signals=_signals()
            )
            self.assertEqual(before.mode, "RECOVERING")
            self.assertNotIn("never-publish", before.to_text())
            self.assertEqual(before.as_dict()["financial_authority"], "NONE")

            record_reconciliation_checkpoint(
                journal,
                reconciliation_id="section8-reconciliation",
                result=reconciliation(),
                observed_at="2026-10-04T02:01:00Z",
                host_id=owner.owner_id,
                owner_epoch=str(owner.epoch),
            )
            recovery.record_reconciliation_checkpoint(
                reconciliation_id="section8-reconciliation",
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
            )
            self.assertEqual(recovery.state, HostState.READY)
            after = build_operator_observability(
                ui_snapshot=ui, recovery=recovery, signals=_signals()
            )
            self.assertEqual(after.mode, "READY")
            self.assertNotIn("never-publish", str(after.as_dict()))

            # A valid reconciliation for one account must not let an unrelated
            # authenticated Host snapshot advertise that account as READY.
            foreign = dict(ui)
            foreign["account_id"] = "foreign-account"
            wrong_scope = build_operator_observability(
                ui_snapshot=foreign, recovery=recovery, signals=_signals()
            )
            self.assertNotEqual(wrong_scope.mode, "READY")
            self.assertIn("host_recovery_scope_mismatch", wrong_scope.reasons)

            # A new process observes the durable prior epoch. Merely constructing
            # a fresh controller must never produce a second sender authority.
            restarted = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:test-account",
            )
            with self.assertRaises(PermissionError):
                restarted.start("host-b")
            self.assertEqual(restarted.state, HostState.STOPPED)
            self.assertIsNone(restarted.owner)
            self.assertEqual(restarted.durable_owner_chain(), (owner,))
            original_again = build_operator_observability(
                ui_snapshot=ui,
                recovery=recovery,
                signals=_signals(unknown_send_count=1),
            )
            self.assertEqual(original_again.mode, "DEGRADED")
            self.assertIn("unknown_sends_present", original_again.reasons)
            self.assertFalse(
                original_again.as_dict()["domains"]["readiness"]["new_exposure_allowed"]
            )

    def test_ambiguous_send_is_never_a_blind_retry_or_a_fill(self):
        attempt = OutboundAttempt(
            attempt_id="plan3-ambiguous-send",
            intent_id="plan3-intent",
            owner_epoch=1,
        )
        attempt.persist()
        self.assertEqual(attempt.retry_disposition, "SAFE_WITH_NEW_ADMISSION")
        attempt.mark_send_started("fixture:wire-side-effect-uncertain")
        self.assertEqual(attempt.retry_disposition, "RECONCILE_FIRST")
        self.assertIsNone(attempt.provider_order_id)
        with self.assertRaises(ValueError):
            attempt.persist()
        with self.assertRaises(ValueError):
            attempt.mark_send_started("fixture:blind-retry")
        self.assertEqual(attempt.retry_disposition, "RECONCILE_FIRST")

    def test_legacy_backup_restore_preserves_diagnostics_and_blocks_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            artifacts = root / "artifacts"
            run_vertical_slice([100, 101, 102, 103], state)
            _artifact_store(artifacts)
            before = build_diagnostic_snapshot(state)
            self.assertGreater(before.evidence_count, 0)
            bundle = create_backup(state, artifacts, root / "backup")
            verified = verify_backup(bundle)
            self.assertTrue(verified["reconciliation_required_after_restore"])

            restored = restore_backup(bundle, root / "restored")
            self.assertTrue(restore_requires_reconciliation(restored))
            self.assertTrue(
                (restored / "RESTORE_RECONCILIATION_REQUIRED.json").is_file()
            )
            after = build_diagnostic_snapshot(restored / "state")
            self.assertEqual(after.symbol, before.symbol)
            self.assertEqual(after.evidence_count, before.evidence_count)
            self.assertEqual(after.to_text(), before.to_text())
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_cross_plan_qualification_never_reinterprets_ci_as_scientific_pass(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            recovery = RecoveryController(
                owner_store=journal, owner_scope="PAPER:test-account",
            )
            recovery.start("host-a")
            view = build_operator_observability(
                ui_snapshot=_snapshot(),
                recovery=recovery,
                signals=_signals(
                    provider_reconciled=False,
                    unknown_send_count=2,
                    recovery_in_progress=True,
                ),
            )
            fields = view.as_dict()
            self.assertEqual(fields["financial_authority"], "NONE")
            self.assertFalse(fields["domains"]["evidence"]["science_pass"])
            self.assertEqual(fields["domains"]["evidence"]["source"], "UNAVAILABLE")
            self.assertNotEqual(fields["mode"], "READY")
            self.assertEqual(fields["domains"]["unknown"]["send_count"], 2)


if __name__ == "__main__":
    unittest.main()
