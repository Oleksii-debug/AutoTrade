from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.backup import (
    BACKUP_SCHEMA_VERSION,
    BackupCompatibilityError,
    BackupError,
    BackupIntegrityError,
    complete_restore_reconciliation,
    create_backup,
    restore_backup,
    restore_requires_reconciliation,
    verify_backup,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.pipeline import run_vertical_slice
from mvp.autotrade_mvp.reconciliation import (
    CoverageSurfaceEvidence,
    SnapshotConsistencyEvidence,
    UnknownSubmission,
    reconcile_account,
)
from mvp.autotrade_mvp.recovery import HostState, RecoveryController



def _restore_instant(restored: Path, seconds: int) -> str:
    marker = json.loads(
        (restored / "RESTORE_RECONCILIATION_REQUIRED.json").read_text(encoding="utf-8")
    )
    base = datetime.fromisoformat(marker["restored_at"].replace("Z", "+00:00"))
    value = base.astimezone(timezone.utc) + timedelta(seconds=seconds)
    return value.isoformat().replace("+00:00", "Z")


def _fencing_evidence(restored: Path, **overrides):
    marker = json.loads(
        (restored / "RESTORE_RECONCILIATION_REQUIRED.json").read_text(
            encoding="utf-8"
        )
    )
    payload = {
        "schema_version": 1,
        "kind": "AUTOTRADE_FENCING_ATTESTATION",
        "backup_manifest_sha256": marker["backup_manifest_sha256"],
        "source_sha": marker["source_sha"],
        "old_owner_id": marker["source_owner_id"],
        "old_owner_epoch": marker["source_owner_epoch"],
        "new_owner_id": "restored-host",
        "new_owner_epoch": marker["source_owner_epoch"] + 1,
        "mechanism": "PROVIDER_SESSION_REVOKED",
        "observed_at": _restore_instant(restored, 1),
    }
    payload.update(overrides)
    return [_publish_json_artifact(restored / "artifacts", payload)]


def _restore_controller(restored: Path) -> RecoveryController:
    controller = RecoveryController()
    controller.restore_owner(
        RecoveryController.load_owner_fence(restored / "state" / "owner-fence.json")
    )
    return controller


def _completed_at(restored: Path) -> str:
    return _restore_instant(restored, 10)


def _artifact_store(root: Path) -> str:
    payload = b"immutable-evidence"
    digest = sha256(payload).hexdigest()
    object_path = root / "objects" / "sha256" / digest[:2] / digest
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(payload)
    manifest_path = root / "manifests" / "sha256" / digest[:2] / f"{digest}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "algorithm": "sha256",
                "digest": digest,
                "size_bytes": len(payload),
                "media_type": "application/octet-stream",
                "rights_basis": "first-party-test-evidence",
            }
        ),
        encoding="utf-8",
    )
    return digest


def _publish_json_artifact(root: Path, payload: dict) -> str:
    data = (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    digest = sha256(data).hexdigest()
    object_path = root / "objects" / "sha256" / digest[:2] / digest
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(data)
    manifest_path = root / "manifests" / "sha256" / digest[:2] / f"{digest}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "algorithm": "sha256",
                "digest": digest,
                "size_bytes": len(data),
                "media_type": "application/json",
                "rights_basis": "first-party-test-evidence",
            }
        ),
        encoding="utf-8",
    )
    return "sha256:" + digest


def _build_identity_artifact(
    root: Path,
    *,
    source_sha: str = "a" * 40,
    composition_sha256: str = "sha256:" + "c" * 64,
) -> str:
    return _publish_json_artifact(
        root,
        {
            "schema_version": 1,
            "kind": "AUTOTRADE_BUILD_IDENTITY",
            "source_sha": source_sha,
            "composition_sha256": composition_sha256,
        },
    )


def _reconciliation(*, complete: bool = True):
    return reconcile_account(
        local_cash={"USD": "1000"},
        provider_cash={"USD": "1000"},
        local_positions={},
        provider_positions={},
        local_execution_ids=[],
        provider_fills=[],
        snapshot_consistency=SnapshotConsistencyEvidence(
            mode="ATOMIC",
            query_started_at="2026-09-24T17:00:00Z",
            query_completed_at="2026-09-24T19:00:00Z",
        ),
        coverage_start="2026-09-24T17:00:00Z",
        coverage_end="2026-09-24T19:00:00Z",
        pagination_complete=complete,
    )


def _resolved_absence_reconciliation():
    surfaces = [
        CoverageSurfaceEvidence(
            surface=surface,
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
            provider_semantics_exclude_execution=True,
        )
        for surface in ("OPEN_ORDERS", "ORDER_HISTORY", "EXECUTIONS", "ACTIVITIES")
    ]
    return reconcile_account(
        local_cash={"USD": "1000"},
        provider_cash={"USD": "1000"},
        local_positions={},
        provider_positions={},
        local_execution_ids=[],
        provider_fills=[],
        snapshot_consistency=SnapshotConsistencyEvidence(
            mode="ATOMIC",
            query_started_at="2026-09-24T17:00:00Z",
            query_completed_at="2026-09-24T19:00:00Z",
        ),
        unknown_submissions=[
            UnknownSubmission.create(
                attempt_id="attempt-absent",
                client_order_id="client-absent",
                started_at="2026-09-24T18:00:00Z",
            )
        ],
        searched_client_order_ids=["client-absent"],
        coverage_start="2026-09-24T17:00:00Z",
        coverage_end="2026-09-24T19:00:00Z",
        pagination_complete=True,
        absence_coverage=surfaces,
    )


class BackupRestoreTests(unittest.TestCase):
    def _build_sources(self, root: Path) -> tuple[Path, Path]:
        state = root / "state"
        artifacts = root / "artifacts"
        run_vertical_slice([100, 101, 102, 103], state)
        _artifact_store(artifacts)
        build_identity = _build_identity_artifact(artifacts)
        (state / "build-identity.sha256").write_text(
            build_identity + "\n",
            encoding="ascii",
        )
        source_controller = RecoveryController()
        source_controller.start("source-host")
        source_controller.persist_owner_fence(state / "owner-fence.json")
        return state, artifacts

    def test_wal_active_backup_verifies_and_restore_is_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            journal = state / "journal.sqlite3"
            connection = sqlite3.connect(journal)
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
            try:
                backup = create_backup(state, artifacts, root / "backup")
            finally:
                connection.close()

            manifest = verify_backup(backup)
            self.assertEqual(manifest["schema_version"], BACKUP_SCHEMA_VERSION)
            self.assertTrue(manifest["reconciliation_required_after_restore"])
            self.assertFalse((backup / "state" / "journal.sqlite3-wal").exists())
            self.assertFalse((backup / "state" / "journal.sqlite3-shm").exists())

            restored = restore_backup(backup, root / "restored")
            self.assertTrue(restore_requires_reconciliation(restored))
            self.assertTrue((restored / "state" / "journal.sqlite3").is_file())
            self.assertTrue((restored / "artifacts" / "objects" / "sha256").is_dir())

    def test_restore_marker_uses_digest_verified_before_copy_to_close_toctou(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            verified_digest = (
                backup / "backup-manifest.sha256"
            ).read_text(encoding="ascii").strip()
            forged_digest = "sha256:" + "f" * 64
            original_copy2 = shutil.copy2
            mutated = False

            def mutate_digest_after_verification(source, destination, *args, **kwargs):
                nonlocal mutated
                if not mutated:
                    mutated = True
                    (backup / "backup-manifest.sha256").write_text(
                        forged_digest + "\n",
                        encoding="ascii",
                    )
                return original_copy2(source, destination, *args, **kwargs)

            with patch(
                "mvp.autotrade_mvp.backup.shutil.copy2",
                side_effect=mutate_digest_after_verification,
            ):
                restored = restore_backup(backup, root / "restored")

            marker = json.loads(
                (restored / "RESTORE_RECONCILIATION_REQUIRED.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(marker["backup_manifest_sha256"], verified_digest)
            self.assertNotEqual(marker["backup_manifest_sha256"], forged_digest)

    def test_immutable_build_identity_binds_source_sha_and_restore(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            build_identity = _build_identity_artifact(artifacts)
            backup = create_backup(
                state,
                artifacts,
                root / "backup",
                build_identity_sha256=build_identity,
            )
            manifest = verify_backup(
                backup,
                expected_source_sha="a" * 40,
                expected_build_identity_sha256=build_identity,
            )
            self.assertEqual(manifest["source_sha"], "a" * 40)
            self.assertTrue(manifest["source_sha_bound"])
            self.assertEqual(manifest["build_identity_sha256"], build_identity)
            self.assertEqual(manifest["composition_sha256"], "sha256:" + "c" * 64)
            self.assertNotIn("SOURCE_SHA_UNBOUND", manifest["unresolved_limits"])

            with self.assertRaisesRegex(BackupCompatibilityError, "source SHA"):
                restore_backup(
                    backup,
                    root / "wrong-build",
                    expected_source_sha="b" * 40,
                )
            self.assertFalse((root / "wrong-build").exists())

            restored = restore_backup(
                backup,
                root / "matched-build",
                expected_source_sha="a" * 40,
                expected_build_identity_sha256=build_identity,
            )
            marker = json.loads(
                (restored / "RESTORE_RECONCILIATION_REQUIRED.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(marker["source_sha"], "a" * 40)
            self.assertEqual(marker["build_identity_sha256"], build_identity)
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_unbound_backup_is_truthfully_marked_non_exact(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            (state / "build-identity.sha256").unlink()
            backup = create_backup(state, artifacts, root / "backup")
            manifest = verify_backup(backup)
            self.assertIsNone(manifest["source_sha"])
            self.assertFalse(manifest["source_sha_bound"])
            self.assertIsNone(manifest["build_identity_sha256"])
            self.assertIn("SOURCE_SHA_UNBOUND", manifest["unresolved_limits"])
            with self.assertRaisesRegex(BackupCompatibilityError, "source SHA"):
                verify_backup(backup, expected_source_sha="a" * 40)

    def test_missing_or_wrong_build_identity_artifact_blocks_backup_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            (state / "build-identity.sha256").unlink()
            missing = "sha256:" + "f" * 64
            with self.assertRaisesRegex(BackupIntegrityError, "missing"):
                create_backup(
                    state,
                    artifacts,
                    root / "missing-build",
                    build_identity_sha256=missing,
                )
            self.assertFalse((root / "missing-build").exists())

            wrong_kind = _publish_json_artifact(
                artifacts,
                {
                    "schema_version": 1,
                    "kind": "NOT_A_BUILD_IDENTITY",
                    "source_sha": "a" * 40,
                    "composition_sha256": "sha256:" + "c" * 64,
                },
            )
            with self.assertRaisesRegex(
                BackupCompatibilityError,
                "AUTOTRADE_BUILD_IDENTITY",
            ):
                create_backup(
                    state,
                    artifacts,
                    root / "wrong-kind",
                    build_identity_sha256=wrong_kind,
                )
            self.assertFalse((root / "wrong-kind").exists())

    def test_completed_restore_reconciliation_can_clear_gate_durably(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            self.assertTrue(restore_requires_reconciliation(restored))

            controller = _restore_controller(restored)
            proof = complete_restore_reconciliation(
                restored,
                controller=controller,
                reconciliation=_reconciliation(),
                fencing_evidence=_fencing_evidence(restored),
                completed_at=_completed_at(restored),
            )

            self.assertEqual(controller.state, HostState.READY)
            self.assertEqual(proof["source_owner_id"], "source-host")
            self.assertEqual(proof["source_owner_epoch"], 1)
            self.assertEqual(proof["owner_id"], "restored-host")
            self.assertEqual(proof["owner_epoch"], 2)
            self.assertEqual(proof["blocking_resources"], [])
            self.assertFalse(restore_requires_reconciliation(restored))
            self.assertFalse(restore_requires_reconciliation(restored))

    def test_resolved_absence_completion_uses_current_reconciliation_contract(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            controller = _restore_controller(restored)

            proof = complete_restore_reconciliation(
                restored,
                controller=controller,
                reconciliation=_resolved_absence_reconciliation(),
                fencing_evidence=_fencing_evidence(restored),
                completed_at=_completed_at(restored),
            )

            resolution = proof["submission_resolutions"][0]
            self.assertEqual(resolution["outcome"], "PROVEN_ABSENT")
            self.assertEqual(resolution["provider_execution_ids"], [])
            self.assertEqual(resolution["provider_order_ids"], [])
            self.assertFalse(restore_requires_reconciliation(restored))

    def test_incomplete_reconciliation_never_clears_restore_gate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            controller = _restore_controller(restored)

            with self.assertRaisesRegex(BackupError, "incomplete"):
                complete_restore_reconciliation(
                    restored,
                    controller=controller,
                    reconciliation=_reconciliation(complete=False),
                    fencing_evidence=_fencing_evidence(restored)[:1],
                    completed_at=_completed_at(restored),
                )
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_missing_fencing_evidence_never_clears_restore_gate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            controller = _restore_controller(restored)

            with self.assertRaisesRegex(BackupError, "fencing"):
                complete_restore_reconciliation(
                    restored,
                    controller=controller,
                    reconciliation=_reconciliation(),
                    fencing_evidence=[],
                    completed_at=_completed_at(restored),
                )
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_arbitrary_string_is_not_fencing_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            controller = _restore_controller(restored)
            with self.assertRaisesRegex(BackupIntegrityError, "canonical SHA-256"):
                complete_restore_reconciliation(
                    restored,
                    controller=controller,
                    reconciliation=_reconciliation(),
                    fencing_evidence=["old-host:fenced"],
                    completed_at=_completed_at(restored),
                )
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_missing_fencing_artifact_cannot_clear_gate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            controller = _restore_controller(restored)
            refs = _fencing_evidence(restored)
            digest = refs[0].removeprefix("sha256:")
            object_path = restored / "artifacts" / "objects" / "sha256" / digest[:2] / digest
            object_path.unlink()
            with self.assertRaisesRegex(BackupIntegrityError, "missing"):
                complete_restore_reconciliation(
                    restored,
                    controller=controller,
                    reconciliation=_reconciliation(),
                    fencing_evidence=refs,
                    completed_at=_completed_at(restored),
                )
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_same_fencing_artifact_cannot_be_counted_twice(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            controller = _restore_controller(restored)
            ref = _fencing_evidence(restored)[0]
            with self.assertRaisesRegex(BackupError, "unique"):
                complete_restore_reconciliation(
                    restored,
                    controller=controller,
                    reconciliation=_reconciliation(),
                    fencing_evidence=[ref, ref],
                    completed_at=_completed_at(restored),
                )
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_future_fencing_attestation_never_clears_gate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            controller = _restore_controller(restored)
            future = _fencing_evidence(
                restored,
                observed_at=_restore_instant(restored, 11),
            )
            with self.assertRaisesRegex(BackupError, "postdate"):
                complete_restore_reconciliation(
                    restored,
                    controller=controller,
                    reconciliation=_reconciliation(),
                    fencing_evidence=future,
                    completed_at=_completed_at(restored),
                )
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_wrong_old_owner_epoch_or_unsupported_fence_cannot_clear_gate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")

            wrong_epoch = _fencing_evidence(restored, old_owner_epoch=999)
            with self.assertRaisesRegex(BackupError, "old owner/epoch"):
                complete_restore_reconciliation(
                    restored,
                    controller=_restore_controller(restored),
                    reconciliation=_reconciliation(),
                    fencing_evidence=wrong_epoch,
                    completed_at=_completed_at(restored),
                )

            unsupported = _fencing_evidence(restored, mechanism="UNVERIFIED_CLAIM")
            with self.assertRaisesRegex(BackupError, "mechanism"):
                complete_restore_reconciliation(
                    restored,
                    controller=_restore_controller(restored),
                    reconciliation=_reconciliation(),
                    fencing_evidence=unsupported,
                    completed_at=_completed_at(restored),
                )
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_tampered_restore_completion_proof_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            controller = _restore_controller(restored)
            complete_restore_reconciliation(
                restored,
                controller=controller,
                reconciliation=_reconciliation(),
                fencing_evidence=_fencing_evidence(restored)[:1],
                completed_at=_completed_at(restored),
            )
            proof_path = restored / "RESTORE_RECONCILIATION_COMPLETE.json"
            payload = json.loads(proof_path.read_text(encoding="utf-8"))
            payload["owner_epoch"] = 999
            proof_path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_self_consistent_but_unknown_restore_proof_keeps_gate_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            controller = _restore_controller(restored)
            complete_restore_reconciliation(
                restored,
                controller=controller,
                reconciliation=_reconciliation(),
                fencing_evidence=_fencing_evidence(restored)[:1],
                completed_at=_completed_at(restored),
            )

            proof_path = restored / "RESTORE_RECONCILIATION_COMPLETE.json"
            marker_path = restored / "RESTORE_RECONCILIATION_REQUIRED.json"
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            proof["submission_resolutions"] = [
                {
                    "attempt_id": "ambiguous-1",
                    "client_order_id": "client-ambiguous",
                    "outcome": "UNKNOWN",
                    "provider_execution_ids": [],
                }
            ]
            proof_bytes = (
                json.dumps(
                    proof,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            proof_path.write_bytes(proof_bytes)
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["completion_proof_sha256"] = "sha256:" + sha256(proof_bytes).hexdigest()
            marker_path.write_text(
                json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_self_consistent_but_unmatched_execution_proof_keeps_gate_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            complete_restore_reconciliation(
                restored,
                controller=_restore_controller(restored),
                reconciliation=_reconciliation(),
                fencing_evidence=_fencing_evidence(restored)[:1],
                completed_at=_completed_at(restored),
            )

            proof_path = restored / "RESTORE_RECONCILIATION_COMPLETE.json"
            marker_path = restored / "RESTORE_RECONCILIATION_REQUIRED.json"
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            proof["submission_resolutions"] = [
                {
                    "attempt_id": "attempt-ghost",
                    "client_order_id": "client-ghost",
                    "outcome": "OBSERVED_EXECUTION",
                    "provider_execution_ids": ["execution-not-matched"],
                    "provider_order_ids": [],
                }
            ]
            proof["matched_execution_ids"] = []
            proof_bytes = (
                json.dumps(
                    proof,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            proof_path.write_bytes(proof_bytes)
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["completion_proof_sha256"] = "sha256:" + sha256(proof_bytes).hexdigest()
            marker_path.write_text(
                json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_duplicate_submission_identity_in_rehashed_proof_keeps_gate_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            complete_restore_reconciliation(
                restored,
                controller=_restore_controller(restored),
                reconciliation=_reconciliation(),
                fencing_evidence=_fencing_evidence(restored)[:1],
                completed_at=_completed_at(restored),
            )
            proof_path = restored / "RESTORE_RECONCILIATION_COMPLETE.json"
            marker_path = restored / "RESTORE_RECONCILIATION_REQUIRED.json"
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            row = {
                "attempt_id": "same-attempt",
                "client_order_id": "same-client",
                "outcome": "PROVEN_ABSENT",
                "provider_execution_ids": [],
                "provider_order_ids": [],
            }
            proof["submission_resolutions"] = [row, dict(row)]
            proof_bytes = (
                json.dumps(proof, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            proof_path.write_bytes(proof_bytes)
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["completion_proof_sha256"] = "sha256:" + sha256(proof_bytes).hexdigest()
            marker_path.write_text(
                json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_boolean_owner_epoch_cannot_clear_restore_gate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            controller = _restore_controller(restored)
            complete_restore_reconciliation(
                restored,
                controller=controller,
                reconciliation=_reconciliation(),
                fencing_evidence=_fencing_evidence(restored)[:1],
                completed_at=_completed_at(restored),
            )
            proof_path = restored / "RESTORE_RECONCILIATION_COMPLETE.json"
            marker_path = restored / "RESTORE_RECONCILIATION_REQUIRED.json"
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            proof["owner_epoch"] = True
            proof_bytes = (
                json.dumps(proof, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            proof_path.write_bytes(proof_bytes)
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["completion_proof_sha256"] = "sha256:" + sha256(proof_bytes).hexdigest()
            marker_path.write_text(
                json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_logically_inconsistent_runtime_snapshot_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            evidence_path = state / "learning-evidence.jsonl"
            row = json.loads(evidence_path.read_text(encoding="utf-8"))
            row["risk_outcome"] = "tampered"
            evidence_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(BackupIntegrityError, "consistent snapshot"):
                create_backup(state, artifacts, root / "backup")
            self.assertFalse((root / "backup").exists())

    def test_missing_artifact_blob_invalidates_backup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            digest = sha256(b"immutable-evidence").hexdigest()
            object_path = (
                backup
                / "artifacts"
                / "objects"
                / "sha256"
                / digest[:2]
                / digest
            )
            self.assertTrue(object_path.is_file())
            object_path.unlink()
            with self.assertRaisesRegex(BackupIntegrityError, "missing"):
                verify_backup(backup)

    def test_incompatible_backup_schema_is_rejected_even_with_matching_manifest_digest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            manifest_path = backup / "backup-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = 99
            payload = (
                json.dumps(
                    manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            manifest_path.write_bytes(payload)
            (backup / "backup-manifest.sha256").write_text(
                f"sha256:{sha256(payload).hexdigest()}\n",
                encoding="ascii",
            )
            with self.assertRaises(BackupCompatibilityError):
                verify_backup(backup)

    def test_incompatible_source_journal_schema_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            with closing(sqlite3.connect(state / "journal.sqlite3")) as connection:
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (99, "2026-09-24T00:00:00Z"),
                )
                connection.commit()
            with self.assertRaises(BackupCompatibilityError):
                create_backup(state, artifacts, root / "backup")

    def test_interrupted_restore_never_publishes_partial_destination(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            destination = root / "restored"
            original_copy = shutil.copy2
            calls = 0

            def interrupted(source, target, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated interruption")
                return original_copy(source, target, *args, **kwargs)

            with patch("mvp.autotrade_mvp.backup.shutil.copy2", side_effect=interrupted):
                with self.assertRaises(OSError):
                    restore_backup(backup, destination)
            self.assertFalse(destination.exists())
            self.assertFalse(any(root.glob(".autotrade-restore-*")))

    def test_source_artifact_manifest_with_missing_blob_fails_before_backup_commit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            for path in (artifacts / "objects" / "sha256").rglob("*"):
                if path.is_file():
                    path.unlink()
            with self.assertRaisesRegex(BackupIntegrityError, "missing"):
                create_backup(state, artifacts, root / "backup")
            self.assertFalse((root / "backup").exists())

    def test_backup_destination_cannot_be_inside_state_or_artifacts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            artifacts = root / "artifacts"
            state.mkdir()
            artifacts.mkdir()
            JournalStore(state / "journal.sqlite3")
            with self.assertRaisesRegex(BackupError, "outside source"):
                create_backup(state, artifacts, state / "order-intents" / "backup")
            with self.assertRaisesRegex(BackupError, "outside source"):
                create_backup(state, artifacts, artifacts / "nested-backup")

    def test_restore_destination_cannot_be_inside_backup_bundle(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            artifacts = root / "artifacts"
            state.mkdir()
            artifacts.mkdir()
            JournalStore(state / "journal.sqlite3")
            backup = create_backup(state, artifacts, root / "backup")
            with self.assertRaisesRegex(BackupError, "outside the backup"):
                restore_backup(backup, backup / "restored")

    def test_missing_or_corrupt_restore_marker_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(restore_requires_reconciliation(root))
            marker = root / "RESTORE_RECONCILIATION_REQUIRED.json"
            marker.write_text("not-json", encoding="utf-8")
            self.assertTrue(restore_requires_reconciliation(root))



if __name__ == "__main__":
    unittest.main()
