"""Plan-5 Section-5 write-ahead, no-replay Windows update intent tests.

These are synthetic repository-controlled fixtures only. No test invokes a real
installer, migrates a journal, mints provider authority or launches a Host.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.windows_update as update


SOURCE_PLAN_DIGEST = "sha256:" + "a" * 64


class WindowsUpdateIntentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "update-intent.json"
        self.plan = update.WindowsUpdatePlan(
            status="PLAN_READY", reasons=(), plan_json="{}",
            plan_sha256=SOURCE_PLAN_DIGEST,
        )
        self.trust = object()
        self.fixture = {
            "install_steps": list(update._INSTALL_STEPS),
            "rollback": {"steps": list(update._ROLLBACK_STEPS)},
        }
        p = patch.object(update, "_plan_document", return_value=self.fixture)
        p.start()
        self.addCleanup(p.stop)

    def first(self):
        checkpoint = update.WindowsUpdateCheckpoint(plan_sha256=SOURCE_PLAN_DIGEST)
        return update.prepare_windows_update_step_intent(
            self.plan, checkpoint, trust=self.trust,
        )

    def test_first_intent_is_correct_next_step_and_non_authoritative(self):
        intent = self.first()
        self.assertEqual(intent.phase, "UPDATE")
        self.assertEqual(intent.step, update._INSTALL_STEPS[0])
        self.assertIs(intent.no_trading_authority, True)
        self.assertIs(
            update.assess_windows_update_intent_after_restart(intent)["may_replay_step"],
            False,
        )
        self.assertIs(
            update.assess_windows_update_intent_after_restart(intent)["may_start_second_host"],
            False,
        )

    def test_next_intent_is_derived_from_checkpoint_not_caller_command(self):
        cp = update.WindowsUpdateCheckpoint(
            plan_sha256=SOURCE_PLAN_DIGEST,
            update_completed_steps=(update._INSTALL_STEPS[0],),
        )
        intent = update.prepare_windows_update_step_intent(
            self.plan, cp, trust=self.trust,
        )
        self.assertEqual(intent.step, update._INSTALL_STEPS[1])

    def test_out_of_order_checkpoint_never_selects_action(self):
        cp = update.WindowsUpdateCheckpoint(
            plan_sha256=SOURCE_PLAN_DIGEST,
            update_completed_steps=(update._INSTALL_STEPS[2],),
        )
        with self.assertRaisesRegex(update.WindowsUpdateError, "valid ordered plan prefix"):
            update.prepare_windows_update_step_intent(self.plan, cp, trust=self.trust)

    def test_wrong_plan_digest_rejected(self):
        cp = update.WindowsUpdateCheckpoint(plan_sha256="sha256:" + "b"*64)
        with self.assertRaisesRegex(update.WindowsUpdateError, "different plan"):
            update.prepare_windows_update_step_intent(self.plan, cp, trust=self.trust)

    def test_update_cannot_continue_after_rollback_started(self):
        cp = update.WindowsUpdateCheckpoint(
            plan_sha256=SOURCE_PLAN_DIGEST, rollback_started=True,
        )
        with self.assertRaisesRegex(update.WindowsUpdateError, "phase"):
            update.prepare_windows_update_step_intent(self.plan, cp, trust=self.trust)

    def test_explicit_rollback_intent_only_after_canonical_switch(self):
        cp = update.WindowsUpdateCheckpoint(
            plan_sha256=SOURCE_PLAN_DIGEST, rollback_started=True,
        )
        intent = update.prepare_windows_update_step_intent(
            self.plan, cp, trust=self.trust, rollback=True,
        )
        self.assertEqual(intent.phase, "ROLLBACK")
        self.assertEqual(intent.step, update._ROLLBACK_STEPS[0])
        self.assertFalse(update.assess_windows_update_intent_after_restart(intent)["may_replay_step"])

    def test_forged_rollback_parameter_is_rejected(self):
        cp = update.WindowsUpdateCheckpoint(plan_sha256=SOURCE_PLAN_DIGEST)
        with self.assertRaisesRegex(update.WindowsUpdateError, "exact bool"):
            update.prepare_windows_update_step_intent(
                self.plan, cp, trust=self.trust, rollback=1,
            )

    def test_durable_intent_is_readable_but_never_resumable(self):
        intent = self.first()
        update.publish_windows_update_step_intent(intent, path=self.path)
        returned = update.read_windows_update_step_intent(
            self.plan, trust=self.trust, path=self.path,
        )
        self.assertEqual(returned, intent)
        recovered = update.assess_windows_update_intent_after_restart(returned)
        self.assertEqual(recovered["disposition"], "BLOCKED_UNCERTAIN_EFFECT")
        self.assertTrue(recovered["requires_independent_reconciliation"])
        self.assertFalse(recovered["trading_authority_granted"])

    def test_second_process_cannot_overwrite_unreconciled_intent(self):
        intent = self.first()
        update.publish_windows_update_step_intent(intent, path=self.path)
        recorded = self.path.read_bytes()
        with self.assertRaisesRegex(update.WindowsUpdateError, "already exists"):
            update.publish_windows_update_step_intent(intent, path=self.path)
        self.assertEqual(recorded, self.path.read_bytes())

    def test_disk_full_while_publishing_does_not_create_success_receipt(self):
        with patch(
            "research.autotrade_research.artifacts.durable_publish.atomic_write_json",
            side_effect=OSError("simulated disk full"),
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                update.publish_windows_update_step_intent(
                    self.first(), path=self.path,
                )
        self.assertFalse(self.path.exists())

    def test_file_replace_with_symlink_is_rejected(self):
        other = self.root / "other.json"
        other.write_text("{}", encoding="utf-8")
        try:
            self.path.symlink_to(other)
        except (OSError, NotImplementedError):
            self.skipTest("symlink privileges are unavailable")
        with self.assertRaisesRegex(update.WindowsUpdateError, "symbolic link"):
            update.read_windows_update_step_intent(
                self.plan, trust=self.trust, path=self.path,
            )

    def test_hardlink_aliased_intent_cannot_be_read(self):
        update.publish_windows_update_step_intent(self.first(), path=self.path)
        try:
            os.link(self.path, self.root / "aliased.json")
        except (OSError, NotImplementedError):
            self.skipTest("hard links are unavailable")
        with self.assertRaisesRegex(update.WindowsUpdateError, "ordinary file"):
            update.read_windows_update_step_intent(
                self.plan, trust=self.trust, path=self.path,
            )

    def test_exact_source_digest_tamper_rejected(self):
        update.publish_windows_update_step_intent(self.first(), path=self.path)
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        doc["step"] = update._INSTALL_STEPS[1]
        self.path.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaisesRegex(update.WindowsUpdateError, "content digest mismatch"):
            update.read_windows_update_step_intent(
                self.plan, trust=self.trust, path=self.path,
            )

    def test_claim_of_completed_effect_not_accepted_even_if_rehashed(self):
        update.publish_windows_update_step_intent(self.first(), path=self.path)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        document["effect_outcome"] = "COMPLETED"
        import hashlib
        values = {k: v for k, v in document.items() if k != "content_sha256"}
        payload = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        document["content_sha256"] = "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
        self.path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(update.WindowsUpdateError, "completed or replayable"):
            update.read_windows_update_step_intent(
                self.plan, trust=self.trust, path=self.path,
            )

    def test_unknown_future_envelope_fields_are_rejected(self):
        update.publish_windows_update_step_intent(self.first(), path=self.path)
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        doc["authorizes_real_orders"] = True
        self.path.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaisesRegex(update.WindowsUpdateError, "noncanonical shape"):
            update.read_windows_update_step_intent(
                self.plan, trust=self.trust, path=self.path,
            )

    def test_duplicate_json_keys_fail_closed(self):
        update.publish_windows_update_step_intent(self.first(), path=self.path)
        raw = self.path.read_text(encoding="utf-8")
        self.path.write_text(raw.replace(
            '"effect_outcome": "UNKNOWN"',
            '"effect_outcome": "UNKNOWN", "effect_outcome": "UNKNOWN"',
        ), encoding="utf-8")
        with self.assertRaisesRegex(update.WindowsUpdateError, "duplicate fields"):
            update.read_windows_update_step_intent(
                self.plan, trust=self.trust, path=self.path,
            )

    def test_stale_inflight_step_is_not_accepted_after_plan_checkpoint_progress(self):
        intent = self.first()
        doc = update._windows_update_intent_payload(intent)
        cp = update.WindowsUpdateCheckpoint(
            plan_sha256=SOURCE_PLAN_DIGEST,
            update_completed_steps=(update._INSTALL_STEPS[0],),
        )
        doc["checkpoint_json"] = update.serialize_update_checkpoint(cp)
        import hashlib
        values = {k: v for k, v in doc.items() if k != "content_sha256"}
        data = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        doc["content_sha256"] = "sha256:" + hashlib.sha256(data.encode()).hexdigest()
        self.path.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaisesRegex(update.WindowsUpdateError, "stale or changes"):
            update.read_windows_update_step_intent(
                self.plan, trust=self.trust, path=self.path,
            )

    def test_bounded_file_input_rejects_oversized_intent(self):
        self.path.write_bytes(b"X" * 16385)
        with self.assertRaisesRegex(update.WindowsUpdateError, "truncated or unstable"):
            update.read_windows_update_step_intent(
                self.plan, trust=self.trust, path=self.path,
            )

    def test_absolute_path_is_required_before_mutation(self):
        with self.assertRaisesRegex(update.WindowsUpdateError, "absolute Path"):
            update.publish_windows_update_step_intent(
                self.first(), path=Path("relative.json"),
            )

    def test_canonical_restart_is_blocked_when_pre_effect_intent_is_unresolved(self):
        cp = update.WindowsUpdateCheckpoint(plan_sha256=SOURCE_PLAN_DIGEST)
        disposition = update.assess_windows_update_restart(
            self.plan,
            cp,
            trust=self.trust,
            observed_windows_package_sha256="sha256:" + "c"*64,
            observed_journal_schema_version=1,
            pending_intent=self.first(),
        )
        self.assertEqual(disposition.disposition, "BLOCKED_UNKNOWN_STATE")
        self.assertIn("INDEPENDENT_RECONCILIATION", disposition.reasons[0])

    def test_restart_cannot_accept_intent_of_different_checkpoint(self):
        progressed = update.WindowsUpdateCheckpoint(
            plan_sha256=SOURCE_PLAN_DIGEST,
            update_completed_steps=(update._INSTALL_STEPS[0],),
        )
        with self.assertRaisesRegex(update.WindowsUpdateError, "differs from restart"):
            update.assess_windows_update_restart(
                self.plan,
                progressed,
                trust=self.trust,
                observed_windows_package_sha256="sha256:" + "c"*64,
                observed_journal_schema_version=1,
                pending_intent=self.first(),
            )

    def test_no_generic_action_can_be_disguised_as_canonical_step(self):
        with self.assertRaisesRegex(update.WindowsUpdateError, "noncanonical step"):
            update.WindowsUpdateStepIntent(
                plan_sha256=SOURCE_PLAN_DIGEST,
                checkpoint_json="{}",
                phase="UPDATE",
                step="SUBMIT_REAL_ORDER",
            )


if __name__ == "__main__":
    unittest.main()
