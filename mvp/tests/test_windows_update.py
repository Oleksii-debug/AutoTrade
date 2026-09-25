from hashlib import sha256
import json
import unittest

from mvp.autotrade_mvp.release_candidate import (
    ReleaseArtifactEvidence,
    ReleaseCandidateDecision,
    ReleaseCandidateInput,
    freeze_release_candidate,
)
from mvp.autotrade_mvp.windows_update import (
    BackupEvidence,
    MigrationEvidence,
    WindowsUpdateCheckpoint,
    WindowsUpdateError,
    WindowsUpdatePlan,
    advance_rollback_checkpoint,
    advance_update_checkpoint,
    assess_windows_update_restart,
    build_windows_update_plan,
    restore_update_checkpoint,
    serialize_update_checkpoint,
    start_rollback_checkpoint,
    start_update_checkpoint,
)


CURRENT_SOURCE = "1" * 40
CANDIDATE_SOURCE = "2" * 40
BASELINE = "sha256:" + "a" * 64
CONTRACTS = "sha256:" + "b" * 64
REQUIRED_ROLES = (
    "HOST",
    "WEB",
    "DESKTOP",
    "WINDOWS_PACKAGE",
    "SBOM",
    "DEPENDENCY_RIGHTS",
    "ACCESSIBILITY",
    "API_COMPATIBILITY",
    "CLEAN_INSTALL",
    "LICENSE_NOTICES",
    "RELEASE_QUALIFICATION",
)


def frozen_release(release_id: str, source_sha: str, digest_seed: int):
    artifacts = []
    for index, role in enumerate(REQUIRED_ROLES):
        digest_char = hex((digest_seed + index) % 16)[2:]
        artifacts.append(
            ReleaseArtifactEvidence.create(
                role=role,
                artifact_sha256="sha256:" + digest_char * 64,
                source_sha=source_sha,
                signature_status=(
                    "VERIFIED"
                    if role in {"HOST", "WEB", "DESKTOP", "WINDOWS_PACKAGE"}
                    else "NOT_APPLICABLE"
                ),
                evidence_status="PASS",
            )
        )
    candidate = ReleaseCandidateInput.create(
        release_id=release_id,
        source_sha=source_sha,
        baseline_hash=BASELINE,
        schema_contract_hash=CONTRACTS,
        artifacts=tuple(artifacts),
        unresolved_blockers=(),
    )
    decision = freeze_release_candidate(candidate)
    if decision.status != "FROZEN":
        raise AssertionError(decision.reasons)
    return decision


def rehashed_plan(plan: WindowsUpdatePlan, mutate) -> WindowsUpdatePlan:
    body = json.loads(plan.plan_json)
    mutate(body)
    forged_json = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return WindowsUpdatePlan(
        status="PLAN_READY",
        reasons=(),
        plan_json=forged_json,
        plan_sha256="sha256:" + sha256(forged_json.encode("utf-8")).hexdigest(),
    )


class WindowsUpdatePlanTests(unittest.TestCase):
    def setUp(self):
        self.current = frozen_release("autotrade-1.0.0", CURRENT_SOURCE, 3)
        self.candidate = frozen_release("autotrade-1.1.0", CANDIDATE_SOURCE, 5)
        self.backup = BackupEvidence(
            manifest_sha256="sha256:" + "c" * 64,
            source_sha=CURRENT_SOURCE,
            journal_schema_version=1,
            verification_status="PASS",
            reconciliation_required_after_restore=True,
        )

    def test_same_schema_update_is_deterministic_and_never_grants_authority(self):
        first = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        )
        second = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        )
        self.assertEqual(first.status, "PLAN_READY")
        self.assertEqual(first.plan_json, second.plan_json)
        self.assertEqual(first.plan_sha256, second.plan_sha256)
        payload = json.loads(first.plan_json)
        self.assertFalse(payload["trading_authority_granted_by_plan"])
        self.assertEqual(
            payload["rollback"]["mode"],
            "REINSTALL_PREVIOUS_BUNDLE",
        )
        self.assertIn(
            "RUN_POST_UPDATE_RECONCILIATION",
            payload["install_steps"],
        )
        self.assertIn(
            "ENTER_READY_FOR_SEPARATE_AUTHORITY_REACQUISITION",
            payload["install_steps"],
        )

    def test_schema_change_without_verified_migration_is_blocked(self):
        decision = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=2,
            backup_evidence=self.backup,
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn(
            "schema_change_requires_verified_migration_evidence",
            decision.reasons,
        )
        self.assertIsNone(decision.plan_json)

    def test_verified_schema_transition_binds_candidate_source_and_rollback(self):
        migration = MigrationEvidence(
            from_schema_version=1,
            to_schema_version=2,
            source_sha=CANDIDATE_SOURCE,
            evidence_sha256="sha256:" + "d" * 64,
            verification_status="PASS",
            rollback_mode="RESTORE_PRE_UPDATE_BACKUP",
        )
        decision = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=2,
            backup_evidence=self.backup,
            migration_evidence=migration,
        )
        self.assertEqual(decision.status, "PLAN_READY")
        payload = json.loads(decision.plan_json)
        self.assertEqual(
            payload["journal_schema_transition"],
            {"from": 1, "to": 2},
        )
        self.assertEqual(
            payload["rollback"]["mode"],
            "RESTORE_PRE_UPDATE_BACKUP",
        )
        self.assertEqual(
            payload["migration_evidence"]["source_sha"],
            CANDIDATE_SOURCE,
        )

    def test_migration_bound_to_wrong_source_fails_closed(self):
        migration = MigrationEvidence(
            from_schema_version=1,
            to_schema_version=2,
            source_sha=CURRENT_SOURCE,
            evidence_sha256="sha256:" + "d" * 64,
            verification_status="PASS",
            rollback_mode="RESTORE_PRE_UPDATE_BACKUP",
        )
        decision = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=2,
            backup_evidence=self.backup,
            migration_evidence=migration,
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn("migration_source_sha_mismatch", decision.reasons)

    def test_backup_from_different_release_source_blocks_update(self):
        wrong_backup = BackupEvidence(
            manifest_sha256="sha256:" + "c" * 64,
            source_sha=CANDIDATE_SOURCE,
            journal_schema_version=1,
            verification_status="PASS",
            reconciliation_required_after_restore=True,
        )
        decision = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=wrong_backup,
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn(
            "backup_source_does_not_match_current_release",
            decision.reasons,
        )

    def test_unverified_or_schema_mismatched_backup_blocks_update(self):
        for backup, reason in (
            (
                BackupEvidence(
                    manifest_sha256="sha256:" + "c" * 64,
                    source_sha=CURRENT_SOURCE,
                    journal_schema_version=1,
                    verification_status="INCONCLUSIVE",
                    reconciliation_required_after_restore=True,
                ),
                "pre_update_backup_not_verified",
            ),
            (
                BackupEvidence(
                    manifest_sha256="sha256:" + "c" * 64,
                    source_sha=CURRENT_SOURCE,
                    journal_schema_version=2,
                    verification_status="PASS",
                    reconciliation_required_after_restore=True,
                ),
                "backup_schema_does_not_match_current_runtime",
            ),
            (
                BackupEvidence(
                    manifest_sha256="sha256:" + "c" * 64,
                    source_sha=CURRENT_SOURCE,
                    journal_schema_version=1,
                    verification_status="PASS",
                    reconciliation_required_after_restore=False,
                ),
                "backup_restore_reconciliation_gate_missing",
            ),
        ):
            with self.subTest(reason=reason):
                decision = build_windows_update_plan(
                    current_release=self.current,
                    candidate_release=self.candidate,
                    current_journal_schema_version=1,
                    candidate_journal_schema_version=1,
                    backup_evidence=backup,
                )
                self.assertEqual(decision.status, "BLOCKED")
                self.assertIn(reason, decision.reasons)

    def test_identical_release_is_not_an_update(self):
        decision = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.current,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn(
            "candidate_is_identical_to_current_release",
            decision.reasons,
        )

    def test_migration_evidence_without_schema_change_is_rejected(self):
        migration = MigrationEvidence(
            from_schema_version=1,
            to_schema_version=2,
            source_sha=CANDIDATE_SOURCE,
            evidence_sha256="sha256:" + "d" * 64,
            verification_status="PASS",
            rollback_mode="RESTORE_PRE_UPDATE_BACKUP",
        )
        decision = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
            migration_evidence=migration,
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn(
            "migration_evidence_supplied_without_schema_change",
            decision.reasons,
        )

    def test_forged_frozen_manifest_without_windows_package_fails_closed(self):
        payload = json.loads(self.current.manifest_json)
        payload["artifacts"] = [
            item for item in payload["artifacts"] if item["role"] != "WINDOWS_PACKAGE"
        ]
        forged_json = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        from hashlib import sha256

        forged = ReleaseCandidateDecision(
            status="FROZEN",
            reasons=(),
            manifest_json=forged_json,
            manifest_sha256="sha256:" + sha256(forged_json.encode("utf-8")).hexdigest(),
        )
        with self.assertRaisesRegex(
            WindowsUpdateError,
            "exactly one WINDOWS_PACKAGE",
        ):
            build_windows_update_plan(
                current_release=forged,
                candidate_release=self.candidate,
                current_journal_schema_version=1,
                candidate_journal_schema_version=1,
                backup_evidence=self.backup,
            )

    def test_forged_manifest_digest_is_rejected(self):
        forged = ReleaseCandidateDecision(
            status="FROZEN",
            reasons=(),
            manifest_json=self.current.manifest_json,
            manifest_sha256="sha256:" + "f" * 64,
        )
        with self.assertRaisesRegex(
            WindowsUpdateError,
            "digest does not match",
        ):
            build_windows_update_plan(
                current_release=forged,
                candidate_release=self.candidate,
                current_journal_schema_version=1,
                candidate_journal_schema_version=1,
                backup_evidence=self.backup,
            )

    def test_schema_versions_reject_bool_and_nonpositive_values(self):
        with self.assertRaisesRegex(WindowsUpdateError, "positive integer"):
            build_windows_update_plan(
                current_release=self.current,
                candidate_release=self.candidate,
                current_journal_schema_version=True,
                candidate_journal_schema_version=1,
                backup_evidence=self.backup,
            )
        with self.assertRaisesRegex(WindowsUpdateError, "positive integer"):
            BackupEvidence(
                manifest_sha256="sha256:" + "c" * 64,
                journal_schema_version=0,
                verification_status="PASS",
                reconciliation_required_after_restore=True,
            )


    def test_checkpoint_enforces_order_and_exact_retry_is_idempotent(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        )
        checkpoint = start_update_checkpoint(plan)
        with self.assertRaisesRegex(WindowsUpdateError, "out-of-order update step"):
            advance_update_checkpoint(
                plan,
                checkpoint,
                "VERIFY_PRE_UPDATE_BACKUP",
            )
        checkpoint = advance_update_checkpoint(
            plan,
            checkpoint,
            "STOP_AND_FENCE_FINANCIAL_SENDER",
        )
        replay = advance_update_checkpoint(
            plan,
            checkpoint,
            "STOP_AND_FENCE_FINANCIAL_SENDER",
        )
        self.assertIs(replay, checkpoint)
        checkpoint = advance_update_checkpoint(
            plan,
            checkpoint,
            "VERIFY_PRE_UPDATE_BACKUP",
        )
        self.assertEqual(
            checkpoint.update_completed_steps,
            (
                "STOP_AND_FENCE_FINANCIAL_SENDER",
                "VERIFY_PRE_UPDATE_BACKUP",
            ),
        )

    def test_restart_checkpoint_rejects_nonprefix_history(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        )
        corrupt = WindowsUpdateCheckpoint(
            plan_sha256=plan.plan_sha256,
            update_completed_steps=("VERIFY_PRE_UPDATE_BACKUP",),
        )
        with self.assertRaisesRegex(WindowsUpdateError, "valid ordered plan prefix"):
            advance_update_checkpoint(
                plan,
                corrupt,
                "VERIFY_CANDIDATE_SIGNATURE_AND_EXACT_HASH",
            )

    def test_rollback_blocks_forward_progress_and_has_its_own_order(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        )
        checkpoint = start_update_checkpoint(plan)
        checkpoint = advance_update_checkpoint(
            plan,
            checkpoint,
            "STOP_AND_FENCE_FINANCIAL_SENDER",
        )
        checkpoint = start_rollback_checkpoint(plan, checkpoint)
        with self.assertRaisesRegex(WindowsUpdateError, "cannot continue"):
            advance_update_checkpoint(
                plan,
                checkpoint,
                "VERIFY_PRE_UPDATE_BACKUP",
            )
        with self.assertRaisesRegex(WindowsUpdateError, "out-of-order rollback step"):
            advance_rollback_checkpoint(
                plan,
                checkpoint,
                "RUN_POST_RESTORE_RECONCILIATION",
            )
        checkpoint = advance_rollback_checkpoint(
            plan,
            checkpoint,
            "STOP_AND_FENCE_FINANCIAL_SENDER",
        )
        replay = advance_rollback_checkpoint(
            plan,
            checkpoint,
            "STOP_AND_FENCE_FINANCIAL_SENDER",
        )
        self.assertIs(replay, checkpoint)

    def test_checkpoint_is_bound_to_exact_plan_digest(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        )
        other_candidate = frozen_release(
            "autotrade-1.2.0",
            "3" * 40,
            7,
        )
        other_plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=other_candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        )
        checkpoint = start_update_checkpoint(plan)
        with self.assertRaisesRegex(WindowsUpdateError, "different update plan"):
            advance_update_checkpoint(
                other_plan,
                checkpoint,
                "STOP_AND_FENCE_FINANCIAL_SENDER",
            )

    def test_plan_stops_before_separate_authority_reacquisition(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        )
        payload = json.loads(plan.plan_json)
        self.assertEqual(
            payload["install_steps"][-1],
            "ENTER_READY_FOR_SEPARATE_AUTHORITY_REACQUISITION",
        )
        self.assertEqual(
            payload["rollback"]["steps"][-1],
            "ENTER_READY_FOR_SEPARATE_AUTHORITY_REACQUISITION",
        )
        self.assertNotIn(
            "REACQUIRE_AUTHORITY",
            "|".join(payload["install_steps"]),
        )


    def test_checkpoint_serialization_roundtrip_is_restart_equivalent(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        )
        checkpoint = start_update_checkpoint(plan)
        checkpoint = advance_update_checkpoint(
            plan,
            checkpoint,
            "STOP_AND_FENCE_FINANCIAL_SENDER",
        )
        checkpoint = advance_update_checkpoint(
            plan,
            checkpoint,
            "VERIFY_PRE_UPDATE_BACKUP",
        )
        serialized = serialize_update_checkpoint(checkpoint)
        restored = restore_update_checkpoint(plan, serialized)
        self.assertEqual(restored, checkpoint)
        self.assertEqual(
            serialize_update_checkpoint(restored),
            serialized,
        )

    def test_checkpoint_restore_fails_closed_on_tamper_or_wrong_plan(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        )
        checkpoint = start_update_checkpoint(plan)
        serialized = json.loads(serialize_update_checkpoint(checkpoint))
        serialized["update_completed_steps"] = ["VERIFY_PRE_UPDATE_BACKUP"]
        with self.assertRaisesRegex(
            WindowsUpdateError,
            "valid ordered plan prefix",
        ):
            restore_update_checkpoint(
                plan,
                json.dumps(serialized, sort_keys=True, separators=(",", ":")),
            )

        other_candidate = frozen_release("autotrade-1.2.0", "3" * 40, 7)
        other_plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=other_candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        )
        with self.assertRaisesRegex(WindowsUpdateError, "different update plan"):
            restore_update_checkpoint(
                other_plan,
                serialize_update_checkpoint(checkpoint),
            )

    def test_checkpoint_restore_rejects_unknown_fields_and_schema(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        )
        checkpoint = start_update_checkpoint(plan)
        body = json.loads(serialize_update_checkpoint(checkpoint))
        body["surprise"] = True
        with self.assertRaisesRegex(WindowsUpdateError, "structure"):
            restore_update_checkpoint(
                plan,
                json.dumps(body, sort_keys=True, separators=(",", ":")),
            )

        body = json.loads(serialize_update_checkpoint(checkpoint))
        body["schema_version"] = 99
        with self.assertRaisesRegex(WindowsUpdateError, "unsupported"):
            restore_update_checkpoint(
                plan,
                json.dumps(body, sort_keys=True, separators=(",", ":")),
            )


    def test_restart_assessment_uses_observed_state_not_checkpoint_hope(self):
        migration = MigrationEvidence(
            from_schema_version=1,
            to_schema_version=2,
            source_sha=CANDIDATE_SOURCE,
            evidence_sha256="sha256:" + "d" * 64,
            verification_status="PASS",
            rollback_mode="RESTORE_PRE_UPDATE_BACKUP",
        )
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=2,
            backup_evidence=self.backup,
            migration_evidence=migration,
        )
        document = json.loads(plan.plan_json)
        current_package = document["current_release"]["windows_package_sha256"]
        candidate_package = document["candidate_release"]["windows_package_sha256"]
        checkpoint = start_update_checkpoint(plan)

        preinstall = assess_windows_update_restart(
            plan,
            checkpoint,
            observed_windows_package_sha256=current_package,
            observed_journal_schema_version=1,
        )
        self.assertEqual(
            preinstall.disposition,
            "RESUME_PREINSTALL_VERIFICATION",
        )

        candidate_observed = assess_windows_update_restart(
            plan,
            checkpoint,
            observed_windows_package_sha256=candidate_package,
            observed_journal_schema_version=2,
        )
        self.assertEqual(
            candidate_observed.disposition,
            "DEGRADED_RECONCILIATION_REQUIRED",
        )
        self.assertIn(
            "blind_replay_of_install_or_migration_forbidden",
            candidate_observed.reasons,
        )

        mixed = assess_windows_update_restart(
            plan,
            checkpoint,
            observed_windows_package_sha256=candidate_package,
            observed_journal_schema_version=1,
        )
        self.assertEqual(mixed.disposition, "ROLLBACK_REQUIRED")

    def test_restart_assessment_blocks_unknown_durable_state(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        )
        checkpoint = start_update_checkpoint(plan)
        unknown_package = assess_windows_update_restart(
            plan,
            checkpoint,
            observed_windows_package_sha256="sha256:" + "9" * 64,
            observed_journal_schema_version=1,
        )
        self.assertEqual(
            unknown_package.disposition,
            "BLOCKED_UNKNOWN_STATE",
        )

        document = json.loads(plan.plan_json)
        current_package = document["current_release"]["windows_package_sha256"]
        unknown_schema = assess_windows_update_restart(
            plan,
            checkpoint,
            observed_windows_package_sha256=current_package,
            observed_journal_schema_version=99,
        )
        self.assertEqual(
            unknown_schema.disposition,
            "BLOCKED_UNKNOWN_STATE",
        )

    def test_restart_during_rollback_never_resumes_forward_update(self):
        migration = MigrationEvidence(
            from_schema_version=1,
            to_schema_version=2,
            source_sha=CANDIDATE_SOURCE,
            evidence_sha256="sha256:" + "d" * 64,
            verification_status="PASS",
            rollback_mode="RESTORE_PRE_UPDATE_BACKUP",
        )
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=2,
            backup_evidence=self.backup,
            migration_evidence=migration,
        )
        document = json.loads(plan.plan_json)
        current_package = document["current_release"]["windows_package_sha256"]
        candidate_package = document["candidate_release"]["windows_package_sha256"]
        checkpoint = start_rollback_checkpoint(
            plan,
            start_update_checkpoint(plan),
        )
        restored = assess_windows_update_restart(
            plan,
            checkpoint,
            observed_windows_package_sha256=current_package,
            observed_journal_schema_version=1,
        )
        self.assertEqual(
            restored.disposition,
            "DEGRADED_RECONCILIATION_REQUIRED",
        )
        not_restored = assess_windows_update_restart(
            plan,
            checkpoint,
            observed_windows_package_sha256=candidate_package,
            observed_journal_schema_version=2,
        )
        self.assertEqual(not_restored.disposition, "ROLLBACK_REQUIRED")


    def test_exact_identity_rejects_noncanonical_uppercase(self):
        with self.assertRaisesRegex(WindowsUpdateError, "lowercase"):
            BackupEvidence(
                manifest_sha256="sha256:" + "A" * 64,
                source_sha=CURRENT_SOURCE,
                journal_schema_version=1,
                verification_status="PASS",
                reconciliation_required_after_restore=True,
            )
        with self.assertRaisesRegex(WindowsUpdateError, "lowercase Git SHA"):
            MigrationEvidence(
                from_schema_version=1,
                to_schema_version=2,
                source_sha="A" * 40,
                evidence_sha256="sha256:" + "d" * 64,
                verification_status="PASS",
                rollback_mode="RESTORE_PRE_UPDATE_BACKUP",
            )


    def test_reversible_migration_requires_distinct_reverse_evidence(self):
        with self.assertRaisesRegex(
            WindowsUpdateError,
            "requires reverse_evidence_sha256",
        ):
            MigrationEvidence(
                from_schema_version=1,
                to_schema_version=2,
                source_sha=CANDIDATE_SOURCE,
                evidence_sha256="sha256:" + "d" * 64,
                verification_status="PASS",
                rollback_mode="REVERSIBLE_MIGRATION",
            )

        with self.assertRaisesRegex(
            WindowsUpdateError,
            "must be distinct",
        ):
            MigrationEvidence(
                from_schema_version=1,
                to_schema_version=2,
                source_sha=CANDIDATE_SOURCE,
                evidence_sha256="sha256:" + "d" * 64,
                verification_status="PASS",
                rollback_mode="REVERSIBLE_MIGRATION",
                reverse_evidence_sha256="sha256:" + "d" * 64,
            )

        migration = MigrationEvidence(
            from_schema_version=1,
            to_schema_version=2,
            source_sha=CANDIDATE_SOURCE,
            evidence_sha256="sha256:" + "d" * 64,
            verification_status="PASS",
            rollback_mode="REVERSIBLE_MIGRATION",
            reverse_evidence_sha256="sha256:" + "e" * 64,
        )
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=2,
            backup_evidence=self.backup,
            migration_evidence=migration,
        )
        self.assertEqual(plan.status, "PLAN_READY")
        payload = json.loads(plan.plan_json)
        self.assertEqual(
            payload["migration_evidence"]["reverse_evidence_sha256"],
            "sha256:" + "e" * 64,
        )
        self.assertEqual(
            payload["rollback"]["mode"],
            "REVERSIBLE_MIGRATION",
        )

    def test_backup_restore_rollback_rejects_fake_reverse_migration_evidence(self):
        with self.assertRaisesRegex(
            WindowsUpdateError,
            "cannot claim reverse migration evidence",
        ):
            MigrationEvidence(
                from_schema_version=1,
                to_schema_version=2,
                source_sha=CANDIDATE_SOURCE,
                evidence_sha256="sha256:" + "d" * 64,
                verification_status="PASS",
                rollback_mode="RESTORE_PRE_UPDATE_BACKUP",
                reverse_evidence_sha256="sha256:" + "e" * 64,
            )


    def test_rehashed_backup_evidence_cannot_bypass_planner_gates(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        )
        mutations = (
            (
                lambda body: body["pre_update_backup"].__setitem__(
                    "verification_status", "FAIL"
                ),
                "backup is not verified",
            ),
            (
                lambda body: body["pre_update_backup"].__setitem__(
                    "source_sha", CANDIDATE_SOURCE
                ),
                "backup source does not match current release",
            ),
            (
                lambda body: body["pre_update_backup"].__setitem__(
                    "journal_schema_version", 2
                ),
                "backup schema does not match current runtime",
            ),
            (
                lambda body: body["pre_update_backup"].__setitem__(
                    "reconciliation_required_after_restore", False
                ),
                "reconciliation gate is missing",
            ),
        )
        for mutate, message in mutations:
            with self.subTest(message=message):
                forged = rehashed_plan(plan, mutate)
                with self.assertRaisesRegex(WindowsUpdateError, message):
                    start_update_checkpoint(forged)

    def test_rehashed_release_identity_or_package_cannot_escape_frozen_manifest(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        )
        mutations = (
            lambda body: body["candidate_release"].__setitem__(
                "windows_package_sha256", "sha256:" + "f" * 64
            ),
            lambda body: body["candidate_release"].__setitem__(
                "source_sha", "4" * 40
            ),
            lambda body: body["current_release"].__setitem__(
                "release_id", "forged-current"
            ),
            lambda body: body["current_release"].__setitem__(
                "manifest_sha256", "sha256:" + "e" * 64
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                forged = rehashed_plan(plan, mutate)
                with self.assertRaises(WindowsUpdateError):
                    start_update_checkpoint(forged)

    def test_rehashed_migration_evidence_cannot_bypass_schema_gates(self):
        migration = MigrationEvidence(
            from_schema_version=1,
            to_schema_version=2,
            source_sha=CANDIDATE_SOURCE,
            evidence_sha256="sha256:" + "d" * 64,
            verification_status="PASS",
            rollback_mode="RESTORE_PRE_UPDATE_BACKUP",
        )
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=2,
            backup_evidence=self.backup,
            migration_evidence=migration,
        )
        mutations = (
            (
                lambda body: body["migration_evidence"].__setitem__(
                    "verification_status", "FAIL"
                ),
                "migration evidence is not verified",
            ),
            (
                lambda body: body["migration_evidence"].__setitem__(
                    "source_sha", CURRENT_SOURCE
                ),
                "migration source release mismatch",
            ),
            (
                lambda body: body["migration_evidence"].__setitem__(
                    "from_schema_version", 2
                ),
                "migration source schema mismatch",
            ),
            (
                lambda body: body["rollback"].__setitem__(
                    "mode", "REVERSIBLE_MIGRATION"
                ),
                "rollback mode does not match migration evidence",
            ),
        )
        for mutate, message in mutations:
            with self.subTest(message=message):
                forged = rehashed_plan(plan, mutate)
                with self.assertRaisesRegex(WindowsUpdateError, message):
                    start_update_checkpoint(forged)

    def test_rehashed_custom_step_sequence_cannot_bypass_canonical_plan(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        )
        body = json.loads(plan.plan_json)
        body["install_steps"][1] = "ARBITRARY_SIDE_EFFECT"
        forged_json = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        forged = WindowsUpdatePlan(
            status="PLAN_READY",
            reasons=(),
            plan_json=forged_json,
            plan_sha256="sha256:" + sha256(forged_json.encode("utf-8")).hexdigest(),
        )
        with self.assertRaisesRegex(WindowsUpdateError, "step sequence is not canonical"):
            start_update_checkpoint(forged)

    def test_rehashed_plan_cannot_claim_trading_authority(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        )
        body = json.loads(plan.plan_json)
        body["trading_authority_granted_by_plan"] = True
        forged_json = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        forged = WindowsUpdatePlan(
            status="PLAN_READY",
            reasons=(),
            plan_json=forged_json,
            plan_sha256="sha256:" + sha256(forged_json.encode("utf-8")).hexdigest(),
        )
        with self.assertRaisesRegex(WindowsUpdateError, "cannot grant trading authority"):
            start_update_checkpoint(forged)


if __name__ == "__main__":
    unittest.main()
