import base64
from hashlib import sha256
import json
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp.qualification_attestation import (
    EvidenceArtifactRef,
    QualificationAttestation,
    QualificationScope,
    QualificationTrustPolicy,
    SignedQualificationAttestation,
    TrustRoot,
)
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
    WindowsUpdateTrustContext,
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
RELEASE_MEDIA_TYPE = "application/vnd.autotrade.release-artifact"
RELEASE_EVIDENCE_KIND = "AUTOTRADE_RELEASE_EVIDENCE_V1"
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

# Non-production RSA fixture. Runtime trust contains only the public half.
_RSA_N = int(
    "ae5f6165c50e6af720388e7649c53e3ec1c539b5d6cfea06c10d9895b362fc9f"
    "ae4d7afc9c4496d4ef3716fd49f8f9321f81e9794be8861f078cd25c702e57a6"
    "f135cb6e6cc7ee562c5aee6a52eae29a4b61fd6db7a0e0272525888588247d3b"
    "86f94992b9fe6471da90a6db05bd1108702adadba98e0130a903e1fa3ee58211b"
    "61c036a81442fb25824cc4b90dd8ae2eeee4112d01f80c5b44898f03bf3e7d278"
    "6906aa2ea7d3065074c5a6ab72f20926fa34edef80433f64ab60f57e91e22a700"
    "f09f442796f17b142331d8b6764c8ccc9545320f11a2f52512915a3085e41beaf"
    "2e3437e1a2f91cd674c49f165fa296a50d1692d78ea3e4bc0fcc9b021f53",
    16,
)
_RSA_D = int(
    "43e0b631df1923336af40924ebc79fd8df262eb665d60eb42d5f6507d54a51abb"
    "936c90adfabe589234baf23cf315f940ee6cbe35f54b72d0a0bdbf186ebcb4c1d"
    "b682a7cc29b1d212b71cfaffa716a9d8715f2d601f7c5250a8013275d23a7bbb2"
    "97c65e5082dc29241dfe9ff9c5f2e89376d75b7d5a309f5a920c500c9e7ace61"
    "f85eefc7665f3dff9bab2e28da848e0ea0c5c32a90043fdaf056c3a21431d4aa2"
    "9eaed6a2d70bbbaff3b78e1bdd9bbd4a617e3f16d5da358864c538408994097358"
    "54c4cecdadc8f082693679b823270b8d2402102916cb2b94bb92615d480b184a8d"
    "cd479ac90fd9c18ee4e341b9bee6238f231b672d8715c649e28cdde9",
    16,
)
_DER_SHA256 = bytes.fromhex("3031300d060960864801650304020105000420")


def _trust_root():
    return TrustRoot(
        producer_id="qualifier.release.service",
        verifier_id="autotrade.trust.verifier",
        public_modulus_hex=format(_RSA_N, "x"),
        public_exponent=65537,
        allowed_scopes=(QualificationScope("RELEASE", "FREEZE"),),
        valid_from="2026-09-01T00:00:00Z",
    )


def _sign(attestation):
    digest_info = _DER_SHA256 + sha256(attestation.canonical_bytes()).digest()
    width = (_RSA_N.bit_length() + 7) // 8
    encoded = (
        b"\x00\x01"
        + b"\xff" * (width - len(digest_info) - 3)
        + b"\x00"
        + digest_info
    )
    signature = pow(
        int.from_bytes(encoded, "big"), _RSA_D, _RSA_N
    ).to_bytes(width, "big")
    return base64.b64encode(signature).decode("ascii")


def frozen_release(
    store,
    trust_root,
    trust_policy,
    release_id: str,
    source_sha: str,
    digest_seed: int,
):
    artifacts = []
    for index, role in enumerate(REQUIRED_ROLES):
        digest_char = hex((digest_seed + index) % 16)[2:]
        identity = (
            f"autotrade-windows-update-test:{release_id}:"
            f"{source_sha}:{role}:{digest_char}"
        )
        artifact_id = str(uuid5(NAMESPACE_URL, identity))
        data = ("release-evidence:" + identity).encode("utf-8")
        signature_status = (
            "VERIFIED"
            if role in {"HOST", "WEB", "DESKTOP", "WINDOWS_PACKAGE"}
            else "NOT_APPLICABLE"
        )
        artifact = ReleaseArtifactEvidence.create(
            role=role,
            artifact_id=artifact_id,
            artifact_sha256="sha256:" + sha256(data).hexdigest(),
            source_sha=source_sha,
            signature_status=signature_status,
            evidence_status="PASS",
        )
        store.publish_bytes(
            artifact_id=artifact_id,
            data=data,
            media_type=RELEASE_MEDIA_TYPE,
            rights={"storage": True, "export": False},
            source_refs=[f"git:{source_sha}"],
            metadata={
                "evidence_kind": RELEASE_EVIDENCE_KIND,
                "role": role,
                "source_sha": source_sha,
                "signature_status": signature_status,
                "evidence_status": "PASS",
            },
        )
        artifacts.append(artifact)

    candidate = ReleaseCandidateInput.create(
        release_id=release_id,
        source_sha=source_sha,
        baseline_hash=BASELINE,
        schema_contract_hash=CONTRACTS,
        artifacts=tuple(artifacts),
        unresolved_blockers=(),
    )
    evidence_refs = tuple(
        EvidenceArtifactRef(
            artifact_id=item.artifact_id,
            sha256=item.artifact_sha256,
            media_type=RELEASE_MEDIA_TYPE,
            evidence_kind=RELEASE_EVIDENCE_KIND,
            source_sha=item.source_sha,
        )
        for item in candidate.artifacts
    )
    windows_package = next(
        item for item in candidate.artifacts if item.role == "WINDOWS_PACKAGE"
    )
    attestation = QualificationAttestation(
        attestation_id=str(
            uuid5(
                NAMESPACE_URL,
                f"autotrade-windows-update-test:{release_id}:qualification",
            )
        ),
        source_sha=source_sha,
        domain="RELEASE",
        gate="FREEZE",
        package_id="WP-54",
        protocol_id="release-freeze-v1",
        protocol_version="1.0.0",
        requirement_ids=("release-candidate-freeze",),
        evidence_refs=evidence_refs,
        producer_id=trust_root.producer_id,
        verifier_id=trust_root.verifier_id,
        trust_root_id=trust_root.root_id,
        runner_id="windows-update-test-qualification",
        harness_version="1.0.0",
        started_at="2026-09-25T02:00:00Z",
        completed_at="2026-09-25T02:10:00Z",
        signed_at="2026-09-25T02:11:00Z",
        result="PASS",
        release_artifact_id=windows_package.artifact_id,
        release_artifact_sha256=windows_package.artifact_sha256,
    )
    receipt = SignedQualificationAttestation(attestation, _sign(attestation))
    decision = freeze_release_candidate(
        candidate,
        evidence_store=store,
        qualification_receipt=receipt,
        qualification_policy=trust_policy,
        expected_policy_id=trust_policy.policy_id,
        expected_policy_version=trust_policy.policy_version,
    )
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
        self._tempdir = TemporaryDirectory()
        self.store = ArtifactStore(self._tempdir.name)
        self.trust_root = _trust_root()
        self.trust_policy = QualificationTrustPolicy(
            policy_version="2026.09",
            roots=(self.trust_root,),
        )
        self.trust = WindowsUpdateTrustContext(
            evidence_store=self.store,
            qualification_policy=self.trust_policy,
            expected_policy_id=self.trust_policy.policy_id,
            expected_policy_version=self.trust_policy.policy_version,
        )
        self.current = frozen_release(
            self.store,
            self.trust_root,
            self.trust_policy,
            "autotrade-1.0.0",
            CURRENT_SOURCE,
            3,
        )
        self.candidate = frozen_release(
            self.store,
            self.trust_root,
            self.trust_policy,
            "autotrade-1.1.0",
            CANDIDATE_SOURCE,
            5,
        )
        self.backup = BackupEvidence(
            manifest_sha256="sha256:" + "c" * 64,
            source_sha=CURRENT_SOURCE,
            journal_schema_version=1,
            verification_status="PASS",
            reconciliation_required_after_restore=True,
        )

    def tearDown(self):
        self._tempdir.cleanup()

    def test_same_schema_update_is_deterministic_and_never_grants_authority(self):
        first = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        trust=self.trust,
        )
        second = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        trust=self.trust,
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

    def test_update_plan_orders_verification_quiesce_reconciliation_and_fence(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
            trust=self.trust,
        )
        payload = json.loads(plan.plan_json)
        self.assertEqual(
            payload["install_steps"][:5],
            [
                "VERIFY_CANDIDATE_SIGNATURE_AND_EXACT_HASH",
                "QUIESCE_NEW_ADMISSIONS",
                "SURFACE_AND_RECONCILE_IN_FLIGHT_PROVIDER_SENDS",
                "VERIFY_PRE_UPDATE_BACKUP",
                "STOP_AND_FENCE_FINANCIAL_SENDER",
            ],
        )
        self.assertEqual(
            payload["rollback"]["steps"][:3],
            [
                "QUIESCE_NEW_ADMISSIONS",
                "SURFACE_AND_RECONCILE_IN_FLIGHT_PROVIDER_SENDS",
                "STOP_AND_FENCE_FINANCIAL_SENDER",
            ],
        )

    def test_post_start_update_barriers_match_canonical_recovery_sequence(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
            trust=self.trust,
        )
        payload = json.loads(plan.plan_json)
        install = payload["install_steps"]
        degraded = install.index("START_DEGRADED_NO_TRADING_AUTHORITY")
        self.assertEqual(
            install[degraded:],
            [
                "START_DEGRADED_NO_TRADING_AUTHORITY",
                "VALIDATE_JOURNAL_STORAGE_CLOCK_SECURITY_IDENTITY",
                "REESTABLISH_AUTHENTICATED_PROVIDER_SESSIONS",
                "RUN_POST_UPDATE_RECONCILIATION",
                "VERIFY_HOST_UI_COMPATIBILITY",
                "ENTER_READY_FOR_SEPARATE_AUTHORITY_REACQUISITION",
            ],
        )
        rollback = payload["rollback"]["steps"]
        rollback_degraded = rollback.index("START_DEGRADED_NO_TRADING_AUTHORITY")
        self.assertEqual(
            rollback[rollback_degraded:],
            [
                "START_DEGRADED_NO_TRADING_AUTHORITY",
                "VALIDATE_JOURNAL_STORAGE_CLOCK_SECURITY_IDENTITY",
                "REESTABLISH_AUTHENTICATED_PROVIDER_SESSIONS",
                "RUN_POST_RESTORE_RECONCILIATION",
                "VERIFY_HOST_UI_COMPATIBILITY",
                "ENTER_READY_FOR_SEPARATE_AUTHORITY_REACQUISITION",
            ],
        )

    def test_post_start_reconciliation_cannot_skip_identity_or_session_barriers(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
            trust=self.trust,
        )
        checkpoint = start_update_checkpoint(plan, trust=self.trust)
        payload = json.loads(plan.plan_json)
        for step in payload["install_steps"]:
            if step == "VALIDATE_JOURNAL_STORAGE_CLOCK_SECURITY_IDENTITY":
                break
            checkpoint = advance_update_checkpoint(
                plan,
                checkpoint,
                step,
                trust=self.trust,
            )

        with self.assertRaisesRegex(
            WindowsUpdateError,
            "out-of-order update step",
        ):
            advance_update_checkpoint(
                plan,
                checkpoint,
                "REESTABLISH_AUTHENTICATED_PROVIDER_SESSIONS",
                trust=self.trust,
            )
        with self.assertRaisesRegex(
            WindowsUpdateError,
            "out-of-order update step",
        ):
            advance_update_checkpoint(
                plan,
                checkpoint,
                "RUN_POST_UPDATE_RECONCILIATION",
                trust=self.trust,
            )

        checkpoint = advance_update_checkpoint(
            plan,
            checkpoint,
            "VALIDATE_JOURNAL_STORAGE_CLOCK_SECURITY_IDENTITY",
            trust=self.trust,
        )
        with self.assertRaisesRegex(
            WindowsUpdateError,
            "out-of-order update step",
        ):
            advance_update_checkpoint(
                plan,
                checkpoint,
                "RUN_POST_UPDATE_RECONCILIATION",
                trust=self.trust,
            )

    def test_authority_reacquisition_requires_host_ui_compatibility_barrier(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
            trust=self.trust,
        )
        checkpoint = start_update_checkpoint(plan, trust=self.trust)
        payload = json.loads(plan.plan_json)
        for step in payload["install_steps"]:
            if step == "VERIFY_HOST_UI_COMPATIBILITY":
                break
            checkpoint = advance_update_checkpoint(
                plan,
                checkpoint,
                step,
                trust=self.trust,
            )
        with self.assertRaisesRegex(
            WindowsUpdateError,
            "out-of-order update step",
        ):
            advance_update_checkpoint(
                plan,
                checkpoint,
                "ENTER_READY_FOR_SEPARATE_AUTHORITY_REACQUISITION",
                trust=self.trust,
            )

    def test_schema_change_without_verified_migration_is_blocked(self):
        decision = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=2,
            backup_evidence=self.backup,
        trust=self.trust,
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
        trust=self.trust,
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
        trust=self.trust,
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
        trust=self.trust,
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
                trust=self.trust,
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
        trust=self.trust,
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
        trust=self.trust,
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
            qualification_attestation_id=self.current.qualification_attestation_id,
            qualification_attestation_digest=self.current.qualification_attestation_digest,
            qualification_policy_id=self.current.qualification_policy_id,
            qualification_trust_root_id=self.current.qualification_trust_root_id,
        )
        with self.assertRaisesRegex(
            WindowsUpdateError,
            "independently reverified",
        ):
            build_windows_update_plan(
                current_release=forged,
                candidate_release=self.candidate,
                current_journal_schema_version=1,
                candidate_journal_schema_version=1,
                backup_evidence=self.backup,
            trust=self.trust,
            )

    def test_self_published_frozen_release_with_forged_signature_fails_closed(self):
        payload = json.loads(self.current.manifest_json)
        payload["qualification"]["receipt"]["signature_b64"] = base64.b64encode(
            b"x" * 256
        ).decode("ascii")
        forged_json = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        forged = ReleaseCandidateDecision(
            status="FROZEN",
            reasons=(),
            manifest_json=forged_json,
            manifest_sha256=(
                "sha256:" + sha256(forged_json.encode("utf-8")).hexdigest()
            ),
            qualification_attestation_id=self.current.qualification_attestation_id,
            qualification_attestation_digest=self.current.qualification_attestation_digest,
            qualification_policy_id=self.current.qualification_policy_id,
            qualification_trust_root_id=self.current.qualification_trust_root_id,
        )
        with self.assertRaisesRegex(
            WindowsUpdateError,
            "independently reverified",
        ):
            build_windows_update_plan(
                current_release=forged,
                candidate_release=self.candidate,
                current_journal_schema_version=1,
                candidate_journal_schema_version=1,
                backup_evidence=self.backup,
                trust=self.trust,
            )

    def test_forged_manifest_digest_is_rejected(self):
        forged = ReleaseCandidateDecision(
            status="FROZEN",
            reasons=(),
            manifest_json=self.current.manifest_json,
            manifest_sha256=self.current.manifest_sha256,
            qualification_attestation_id=self.current.qualification_attestation_id,
            qualification_attestation_digest=self.current.qualification_attestation_digest,
            qualification_policy_id=self.current.qualification_policy_id,
            qualification_trust_root_id=self.current.qualification_trust_root_id,
        )
        object.__setattr__(forged, "manifest_sha256", "sha256:" + "f" * 64)
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
            trust=self.trust,
            )

    def test_schema_versions_reject_bool_and_nonpositive_values(self):
        with self.assertRaisesRegex(WindowsUpdateError, "positive integer"):
            build_windows_update_plan(
                current_release=self.current,
                candidate_release=self.candidate,
                current_journal_schema_version=True,
                candidate_journal_schema_version=1,
                backup_evidence=self.backup,
            trust=self.trust,
            )
        with self.assertRaisesRegex(WindowsUpdateError, "positive integer"):
            BackupEvidence(
                manifest_sha256="sha256:" + "c" * 64,
                source_sha=CURRENT_SOURCE,
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
            trust=self.trust,
        )
        checkpoint = start_update_checkpoint(plan, trust=self.trust)
        with self.assertRaisesRegex(WindowsUpdateError, "out-of-order update step"):
            advance_update_checkpoint(
                plan,
                checkpoint,
                "VERIFY_PRE_UPDATE_BACKUP",
                trust=self.trust,
            )

        expected_prefix = (
            "VERIFY_CANDIDATE_SIGNATURE_AND_EXACT_HASH",
            "QUIESCE_NEW_ADMISSIONS",
            "SURFACE_AND_RECONCILE_IN_FLIGHT_PROVIDER_SENDS",
            "VERIFY_PRE_UPDATE_BACKUP",
            "STOP_AND_FENCE_FINANCIAL_SENDER",
        )
        checkpoint = advance_update_checkpoint(
            plan,
            checkpoint,
            expected_prefix[0],
            trust=self.trust,
        )
        replay = advance_update_checkpoint(
            plan,
            checkpoint,
            expected_prefix[0],
            trust=self.trust,
        )
        self.assertIs(replay, checkpoint)
        for step in expected_prefix[1:]:
            checkpoint = advance_update_checkpoint(
                plan,
                checkpoint,
                step,
                trust=self.trust,
            )
        self.assertEqual(checkpoint.update_completed_steps, expected_prefix)

    def test_restart_checkpoint_rejects_nonprefix_history(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        trust=self.trust,
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
            trust=self.trust,
            )

    def test_rollback_blocks_forward_progress_and_has_its_own_order(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
            trust=self.trust,
        )
        checkpoint = start_update_checkpoint(plan, trust=self.trust)
        for step in (
            "VERIFY_CANDIDATE_SIGNATURE_AND_EXACT_HASH",
            "QUIESCE_NEW_ADMISSIONS",
        ):
            checkpoint = advance_update_checkpoint(
                plan,
                checkpoint,
                step,
                trust=self.trust,
            )
        checkpoint = start_rollback_checkpoint(
            plan,
            checkpoint,
            trust=self.trust,
        )
        with self.assertRaisesRegex(WindowsUpdateError, "cannot continue"):
            advance_update_checkpoint(
                plan,
                checkpoint,
                "SURFACE_AND_RECONCILE_IN_FLIGHT_PROVIDER_SENDS",
                trust=self.trust,
            )
        with self.assertRaisesRegex(
            WindowsUpdateError,
            "out-of-order rollback step",
        ):
            advance_rollback_checkpoint(
                plan,
                checkpoint,
                "RUN_POST_RESTORE_RECONCILIATION",
                trust=self.trust,
            )
        checkpoint = advance_rollback_checkpoint(
            plan,
            checkpoint,
            "QUIESCE_NEW_ADMISSIONS",
            trust=self.trust,
        )
        replay = advance_rollback_checkpoint(
            plan,
            checkpoint,
            "QUIESCE_NEW_ADMISSIONS",
            trust=self.trust,
        )
        self.assertIs(replay, checkpoint)
        checkpoint = advance_rollback_checkpoint(
            plan,
            checkpoint,
            "SURFACE_AND_RECONCILE_IN_FLIGHT_PROVIDER_SENDS",
            trust=self.trust,
        )
        checkpoint = advance_rollback_checkpoint(
            plan,
            checkpoint,
            "STOP_AND_FENCE_FINANCIAL_SENDER",
            trust=self.trust,
        )
        self.assertEqual(
            checkpoint.rollback_completed_steps,
            (
                "QUIESCE_NEW_ADMISSIONS",
                "SURFACE_AND_RECONCILE_IN_FLIGHT_PROVIDER_SENDS",
                "STOP_AND_FENCE_FINANCIAL_SENDER",
            ),
        )

    def test_checkpoint_is_bound_to_exact_plan_digest(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        trust=self.trust,
        )
        other_candidate = frozen_release(
            self.store,
            self.trust_root,
            self.trust_policy,
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
        trust=self.trust,
        )
        checkpoint = start_update_checkpoint(plan , trust=self.trust)
        with self.assertRaisesRegex(WindowsUpdateError, "different update plan"):
            advance_update_checkpoint(
                other_plan,
                checkpoint,
                "STOP_AND_FENCE_FINANCIAL_SENDER",
            trust=self.trust,
            )

    def test_plan_stops_before_separate_authority_reacquisition(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        trust=self.trust,
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
            trust=self.trust,
        )
        checkpoint = start_update_checkpoint(plan, trust=self.trust)
        for step in (
            "VERIFY_CANDIDATE_SIGNATURE_AND_EXACT_HASH",
            "QUIESCE_NEW_ADMISSIONS",
            "SURFACE_AND_RECONCILE_IN_FLIGHT_PROVIDER_SENDS",
        ):
            checkpoint = advance_update_checkpoint(
                plan,
                checkpoint,
                step,
                trust=self.trust,
            )
        serialized = serialize_update_checkpoint(checkpoint)
        restored = restore_update_checkpoint(
            plan,
            serialized,
            trust=self.trust,
        )
        self.assertEqual(restored, checkpoint)
        self.assertEqual(serialize_update_checkpoint(restored), serialized)

    def test_checkpoint_restore_fails_closed_on_tamper_or_wrong_plan(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        trust=self.trust,
        )
        checkpoint = start_update_checkpoint(plan , trust=self.trust)
        serialized = json.loads(serialize_update_checkpoint(checkpoint))
        serialized["update_completed_steps"] = ["VERIFY_PRE_UPDATE_BACKUP"]
        with self.assertRaisesRegex(
            WindowsUpdateError,
            "valid ordered plan prefix",
        ):
            restore_update_checkpoint(
                plan,
                json.dumps(serialized, sort_keys=True, separators=(",", ":")),
            trust=self.trust,
            )

        other_candidate = frozen_release(
            self.store,
            self.trust_root,
            self.trust_policy,
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
        trust=self.trust,
        )
        with self.assertRaisesRegex(WindowsUpdateError, "different update plan"):
            restore_update_checkpoint(
                other_plan,
                serialize_update_checkpoint(checkpoint),
            trust=self.trust,
            )

    def test_checkpoint_restore_rejects_unknown_fields_and_schema(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        trust=self.trust,
        )
        checkpoint = start_update_checkpoint(plan , trust=self.trust)
        body = json.loads(serialize_update_checkpoint(checkpoint))
        body["surprise"] = True
        with self.assertRaisesRegex(WindowsUpdateError, "structure"):
            restore_update_checkpoint(
                plan,
                json.dumps(body, sort_keys=True, separators=(",", ":")),
            trust=self.trust,
            )

        body = json.loads(serialize_update_checkpoint(checkpoint))
        body["schema_version"] = 99
        with self.assertRaisesRegex(WindowsUpdateError, "unsupported"):
            restore_update_checkpoint(
                plan,
                json.dumps(body, sort_keys=True, separators=(",", ":")),
            trust=self.trust,
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
        trust=self.trust,
        )
        document = json.loads(plan.plan_json)
        current_package = document["current_release"]["windows_package_sha256"]
        candidate_package = document["candidate_release"]["windows_package_sha256"]
        checkpoint = start_update_checkpoint(plan , trust=self.trust)

        preinstall = assess_windows_update_restart(
            plan,
            checkpoint,
            observed_windows_package_sha256=current_package,
            observed_journal_schema_version=1,
        trust=self.trust,
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
        trust=self.trust,
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
        trust=self.trust,
        )
        self.assertEqual(mixed.disposition, "ROLLBACK_REQUIRED")

    def test_restart_assessment_blocks_unknown_durable_state(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        trust=self.trust,
        )
        checkpoint = start_update_checkpoint(plan , trust=self.trust)
        unknown_package = assess_windows_update_restart(
            plan,
            checkpoint,
            observed_windows_package_sha256="sha256:" + "9" * 64,
            observed_journal_schema_version=1,
        trust=self.trust,
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
        trust=self.trust,
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
        trust=self.trust,
        )
        document = json.loads(plan.plan_json)
        current_package = document["current_release"]["windows_package_sha256"]
        candidate_package = document["candidate_release"]["windows_package_sha256"]
        checkpoint = start_rollback_checkpoint(
            plan,
            start_update_checkpoint(plan , trust=self.trust),
            trust=self.trust,
        )
        restored = assess_windows_update_restart(
            plan,
            checkpoint,
            observed_windows_package_sha256=current_package,
            observed_journal_schema_version=1,
        trust=self.trust,
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
        trust=self.trust,
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
        with self.assertRaisesRegex(WindowsUpdateError, "lowercase Git object id"):
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
        trust=self.trust,
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
        trust=self.trust,
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
                    start_update_checkpoint(forged , trust=self.trust)

    def test_rehashed_release_identity_or_package_cannot_escape_frozen_manifest(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        trust=self.trust,
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
                    start_update_checkpoint(forged , trust=self.trust)

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
        trust=self.trust,
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
                    "from_schema_version", 3
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
                    start_update_checkpoint(forged , trust=self.trust)

    def test_rehashed_custom_step_sequence_cannot_bypass_canonical_plan(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        trust=self.trust,
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
            start_update_checkpoint(forged , trust=self.trust)

    def test_rehashed_plan_cannot_claim_trading_authority(self):
        plan = build_windows_update_plan(
            current_release=self.current,
            candidate_release=self.candidate,
            current_journal_schema_version=1,
            candidate_journal_schema_version=1,
            backup_evidence=self.backup,
        trust=self.trust,
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
            start_update_checkpoint(forged , trust=self.trust)


if __name__ == "__main__":
    unittest.main()
