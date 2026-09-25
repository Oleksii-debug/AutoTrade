from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path, PurePath
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
    _copy_file_durable,
    _fsync_directory,
    _fsync_directory_tree,
    _fsync_file,
    _safe_relative_path,
    complete_restore_reconciliation,
    create_backup,
    restore_backup,
    restore_requires_reconciliation,
    verify_backup,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reconciliation import (
    ReconciliationResult,
    SubmissionResolution,
)
from mvp.autotrade_mvp.recovery import RecoveryController
from mvp.autotrade_mvp.pipeline import run_vertical_slice


def _reseal_backup_manifest(backup: Path) -> None:
    manifest_path = backup / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest["files"]:
        path = backup / Path(*PurePath(item["path"]).parts)
        item["sha256"] = f"sha256:{sha256(path.read_bytes()).hexdigest()}"
        item["size_bytes"] = path.stat().st_size
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


def _reconciliation(
    *,
    complete: bool = True,
    blocking_resources: tuple[str, ...] = (),
    resolutions: tuple[SubmissionResolution, ...] = (),
) -> ReconciliationResult:
    return ReconciliationResult(
        provider_id="SIMULATED",
        account_id="paper-account",
        environment="PAPER",
        complete=complete,
        matched_execution_ids=tuple(
            execution_id
            for item in resolutions
            for execution_id in item.provider_execution_ids
        ),
        unexpected_execution_ids=(),
        missing_local_execution_ids=(),
        matched_working_client_order_ids=tuple(
            item.client_order_id
            for item in resolutions
            if item.outcome == "OBSERVED_WORKING_ORDER"
        ),
        unexpected_working_provider_order_ids=(),
        missing_local_working_client_order_ids=(),
        snapshot_consistent=True,
        provider_cash={},
        provider_positions={},
        snapshot_mode="ATOMIC",
        snapshot_query_started_at="2026-09-25T08:00:00Z",
        snapshot_query_completed_at="2026-09-25T08:00:01Z",
        cash_differences={},
        position_differences={},
        submission_resolutions=resolutions,
        blocking_resources=blocking_resources,
        reasons=(),
        matched_provider_activity_ids=(),
        unexpected_provider_activity_ids=(),
        missing_local_provider_activity_ids=(),
        manual_or_external_activity_ids=(),
        activity_coverage_complete=True,
        resource_availability=None,
        borrow_differences={},
    )


def _publish_sender_fence_evidence(
    restored: Path,
    *,
    backup_manifest_sha256: str,
    old_owner_id: str,
    old_owner_epoch: int,
    new_owner_id: str,
    new_owner_epoch: int,
    fenced_at: str,
) -> str:
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "AUTOTRADE_SENDER_FENCE_EVIDENCE",
                "backup_manifest_sha256": backup_manifest_sha256,
                "old_owner_id": old_owner_id,
                "old_owner_epoch": old_owner_epoch,
                "new_owner_id": new_owner_id,
                "new_owner_epoch": new_owner_epoch,
                "fenced_at": fenced_at,
                "method": "provider-session-revoked-and-host-fenced",
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    digest = sha256(payload).hexdigest()
    object_path = (
        restored
        / "artifacts"
        / "objects"
        / "sha256"
        / digest[:2]
        / digest
    )
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(payload)
    return "sha256:" + digest


def _after_restore(marker: dict[str, object], seconds: int) -> str:
    instant = datetime.fromisoformat(
        str(marker["restored_at"]).replace("Z", "+00:00")
    ).astimezone(timezone.utc) + timedelta(seconds=seconds)
    return instant.isoformat().replace("+00:00", "Z")


class BackupDurabilityTests(unittest.TestCase):
    def test_fsync_directory_flushes_directory_descriptor(self):
        with patch("mvp.autotrade_mvp.backup.os.open", return_value=73) as open_mock, patch(
            "mvp.autotrade_mvp.backup.os.fsync"
        ) as fsync_mock, patch("mvp.autotrade_mvp.backup.os.close") as close_mock:
            _fsync_directory(Path("/durable-parent"))

        self.assertEqual(open_mock.call_args.args[0], Path("/durable-parent"))
        fsync_mock.assert_called_once_with(73)
        close_mock.assert_called_once_with(73)


    def test_directory_tree_flushes_children_before_staging_root(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "stage"
            nested = root / "state" / "nested"
            nested.mkdir(parents=True)
            (nested / "payload.bin").write_bytes(b"x")

            flushed = []
            with patch(
                "mvp.autotrade_mvp.backup._fsync_directory",
                side_effect=lambda path: flushed.append(path),
            ):
                _fsync_directory_tree(root)

            self.assertEqual(flushed[-1], root)
            self.assertIn(root / "state", flushed)
            self.assertIn(nested, flushed)
            self.assertLess(flushed.index(nested), flushed.index(root / "state"))
            self.assertLess(flushed.index(root / "state"), flushed.index(root))

    def test_durable_copy_flushes_payload_and_parent_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.bin"
            target = root / "nested" / "target.bin"
            source.write_bytes(b"durable-payload")

            with patch("mvp.autotrade_mvp.backup.os.fsync") as fsync_mock, patch(
                "mvp.autotrade_mvp.backup._fsync_directory"
            ) as directory_sync:
                _copy_file_durable(source, target)

            self.assertEqual(target.read_bytes(), b"durable-payload")
            self.assertGreaterEqual(fsync_mock.call_count, 1)
            directory_sync.assert_called_once_with(target.parent)

    def test_fsync_file_rejects_symlink_or_non_file(self):
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.bin"
            with self.assertRaises(BackupIntegrityError):
                _fsync_file(missing)

class BackupRestoreTests(unittest.TestCase):
    def _build_sources(self, root: Path) -> tuple[Path, Path]:
        state = root / "state"
        artifacts = root / "artifacts"
        run_vertical_slice([100, 101, 102, 103], state)
        _artifact_store(artifacts)
        return state, artifacts

    def test_manifest_paths_reject_windows_and_noncanonical_forms(self):
        self.assertEqual(
            _safe_relative_path("state/journal.sqlite3").as_posix(),
            "state/journal.sqlite3",
        )
        invalid_paths = (
            "state\\\\journal.sqlite3",
            "C:/state/journal.sqlite3",
            "C:state/journal.sqlite3",
            "state//journal.sqlite3",
            "state/journal.sqlite3/",
        )
        for raw in invalid_paths:
            with self.subTest(raw=raw):
                with self.assertRaises(BackupIntegrityError):
                    _safe_relative_path(raw)

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

    def test_journal_only_backup_declares_journal_only_consistency(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            (state / "checkpoint.json").unlink()
            (state / "learning-evidence.jsonl").unlink()
            backup = create_backup(state, artifacts, root / "backup")
            manifest = verify_backup(backup)
            self.assertEqual(
                manifest["runtime_consistency_check"],
                "JOURNAL_ONLY",
            )

    def test_partial_runtime_consistency_evidence_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            (state / "learning-evidence.jsonl").unlink()
            with self.assertRaisesRegex(
                BackupIntegrityError,
                "must be captured together",
            ):
                create_backup(state, artifacts, root / "backup")

    def test_resealed_manifest_cannot_lie_about_runtime_consistency_mode(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            manifest_path = backup / "backup-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["runtime_consistency_check"] = "JOURNAL_ONLY"
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
            with self.assertRaisesRegex(
                BackupIntegrityError,
                "claim does not match",
            ):
                verify_backup(backup)

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

    def test_resealed_backup_cannot_hide_logically_inconsistent_runtime_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            evidence_path = backup / "state" / "learning-evidence.jsonl"
            row = json.loads(evidence_path.read_text(encoding="utf-8"))
            row["risk_outcome"] = "tampered-after-backup"
            evidence_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            _reseal_backup_manifest(backup)

            with self.assertRaisesRegex(
                BackupIntegrityError,
                "not one consistent snapshot",
            ):
                verify_backup(backup)
            with self.assertRaises(BackupIntegrityError):
                restore_backup(backup, root / "must-not-restore")
            self.assertFalse((root / "must-not-restore").exists())

    def test_missing_artifact_blob_invalidates_backup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            object_files = [
                path
                for path in (backup / "artifacts" / "objects" / "sha256").rglob("*")
                if path.is_file()
            ]
            self.assertEqual(len(object_files), 1)
            object_files[0].unlink()
            with self.assertRaisesRegex(BackupIntegrityError, "missing"):
                verify_backup(backup)

    def test_nested_manifest_named_file_cannot_hide_outside_inventory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")

            hidden = backup / "state" / "backup-manifest.json"
            hidden.write_text('{"not":"inventory metadata"}\n', encoding="utf-8")

            with self.assertRaisesRegex(
                BackupIntegrityError,
                "untracked or missing payload files",
            ):
                verify_backup(backup)

    def test_nested_manifest_digest_named_file_cannot_hide_outside_inventory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")

            hidden = backup / "artifacts" / "backup-manifest.sha256"
            hidden.write_text("sha256:" + "0" * 64 + "\n", encoding="ascii")

            with self.assertRaisesRegex(
                BackupIntegrityError,
                "untracked or missing payload files",
            ):
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
            original_copy = _copy_file_durable
            calls = 0

            def interrupted(source, target):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated interruption")
                return original_copy(source, target)

            verified_manifest = verify_backup(backup)
            with (
                patch(
                    "mvp.autotrade_mvp.backup.verify_backup",
                    return_value=verified_manifest,
                ),
                patch(
                    "mvp.autotrade_mvp.backup._copy_file_durable",
                    side_effect=interrupted,
                ),
            ):
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

    def _restored_with_owner(
        self,
        root: Path,
    ) -> tuple[Path, RecoveryController, dict[str, object]]:
        state, artifacts = self._build_sources(root)
        (state / "checkpoint.json").unlink()
        (state / "learning-evidence.jsonl").unlink()
        source_store = JournalStore(state / "journal.sqlite3")
        source = RecoveryController(owner_store=source_store)
        source.start("source-owner")
        backup = create_backup(state, artifacts, root / "backup")
        restored = restore_backup(backup, root / "restored")
        marker = json.loads(
            (restored / "RESTORE_RECONCILIATION_REQUIRED.json").read_text(
                encoding="utf-8"
            )
        )
        restored_store = JournalStore(restored / "state" / "journal.sqlite3")
        controller = RecoveryController(owner_store=restored_store)
        controller.start("restored-owner")
        controller.record_reconciliation(consistent=True)
        return restored, controller, marker

    def test_restore_completion_requires_reconciliation_and_immutable_fence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            restored, controller, marker = self._restored_with_owner(root)
            fence = _publish_sender_fence_evidence(
                restored,
                backup_manifest_sha256=marker["backup_manifest_sha256"],
                old_owner_id="source-owner",
                old_owner_epoch=1,
                new_owner_id="restored-owner",
                new_owner_epoch=2,
                fenced_at=_after_restore(marker, 1),
            )

            proof = complete_restore_reconciliation(
                restored,
                controller=controller,
                reconciliation=_reconciliation(),
                fencing_evidence=(fence,),
                completed_at=_after_restore(marker, 2),
            )

            self.assertEqual(proof["current_owner_id"], "restored-owner")
            self.assertEqual(proof["current_owner_epoch"], 2)
            self.assertFalse(restore_requires_reconciliation(restored))

    def test_restore_completion_rejects_unknown_reconciliation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            restored, controller, marker = self._restored_with_owner(root)
            fence = _publish_sender_fence_evidence(
                restored,
                backup_manifest_sha256=marker["backup_manifest_sha256"],
                old_owner_id="source-owner",
                old_owner_epoch=1,
                new_owner_id="restored-owner",
                new_owner_epoch=2,
                fenced_at=_after_restore(marker, 1),
            )
            unknown = SubmissionResolution(
                attempt_id="attempt-1",
                intent_id="intent-1",
                client_order_id="client-1",
                outcome="UNKNOWN",
                evidence_reason="provider coverage incomplete",
            )
            with self.assertRaisesRegex(
                BackupError,
                "complete non-blocking reconciliation",
            ):
                complete_restore_reconciliation(
                    restored,
                    controller=controller,
                    reconciliation=_reconciliation(
                        complete=False,
                        blocking_resources=("ACCOUNT",),
                        resolutions=(unknown,),
                    ),
                    fencing_evidence=(fence,),
                    completed_at=_after_restore(marker, 2),
                )
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_restore_completion_rejects_controller_bound_to_other_journal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            restored, _, marker = self._restored_with_owner(root)
            other_store = JournalStore(root / "other" / "journal.sqlite3")
            other = RecoveryController(owner_store=other_store)
            other.start("other-owner")
            other.record_reconciliation(consistent=True)
            fence = _publish_sender_fence_evidence(
                restored,
                backup_manifest_sha256=marker["backup_manifest_sha256"],
                old_owner_id="source-owner",
                old_owner_epoch=1,
                new_owner_id="restored-owner",
                new_owner_epoch=2,
                fenced_at=_after_restore(marker, 1),
            )
            with self.assertRaisesRegex(
                BackupError,
                "restored journal",
            ):
                complete_restore_reconciliation(
                    restored,
                    controller=other,
                    reconciliation=_reconciliation(),
                    fencing_evidence=(fence,),
                    completed_at=_after_restore(marker, 2),
                )

    def test_restore_completion_requires_fence_for_every_owner_epoch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            restored, controller, marker = self._restored_with_owner(root)
            first = _publish_sender_fence_evidence(
                restored,
                backup_manifest_sha256=marker["backup_manifest_sha256"],
                old_owner_id="source-owner",
                old_owner_epoch=1,
                new_owner_id="restored-owner",
                new_owner_epoch=2,
                fenced_at=_after_restore(marker, 1),
            )
            controller.transfer_owner(
                new_owner_id="replacement-owner",
                old_sender_fenced=True,
                reconciled=True,
            )
            controller.record_reconciliation(consistent=True)
            second = _publish_sender_fence_evidence(
                restored,
                backup_manifest_sha256=marker["backup_manifest_sha256"],
                old_owner_id="restored-owner",
                old_owner_epoch=2,
                new_owner_id="replacement-owner",
                new_owner_epoch=3,
                fenced_at=_after_restore(marker, 2),
            )

            with self.assertRaisesRegex(
                BackupError,
                "every post-restore owner transition",
            ):
                complete_restore_reconciliation(
                    restored,
                    controller=controller,
                    reconciliation=_reconciliation(),
                    fencing_evidence=(first,),
                    completed_at=_after_restore(marker, 3),
                )

            complete_restore_reconciliation(
                restored,
                controller=controller,
                reconciliation=_reconciliation(),
                fencing_evidence=(first, second),
                completed_at=_after_restore(marker, 3),
            )
            self.assertFalse(restore_requires_reconciliation(restored))

    def test_restore_completion_semantic_tamper_fails_closed_even_if_rehashed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            restored, controller, marker = self._restored_with_owner(root)
            fence = _publish_sender_fence_evidence(
                restored,
                backup_manifest_sha256=marker["backup_manifest_sha256"],
                old_owner_id="source-owner",
                old_owner_epoch=1,
                new_owner_id="restored-owner",
                new_owner_epoch=2,
                fenced_at=_after_restore(marker, 1),
            )
            complete_restore_reconciliation(
                restored,
                controller=controller,
                reconciliation=_reconciliation(),
                fencing_evidence=(fence,),
                completed_at=_after_restore(marker, 2),
            )
            proof_path = restored / "RESTORE_RECONCILIATION_COMPLETE.json"
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            proof["reconciliation"]["complete"] = False
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
            marker_path = restored / "RESTORE_RECONCILIATION_REQUIRED.json"
            completed_marker = json.loads(
                marker_path.read_text(encoding="utf-8")
            )
            completed_marker["completion_proof_sha256"] = (
                "sha256:" + sha256(proof_bytes).hexdigest()
            )
            marker_path.write_text(
                json.dumps(
                    completed_marker,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_new_sender_epoch_after_completion_reopens_restore_gate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            restored, controller, marker = self._restored_with_owner(root)
            fence = _publish_sender_fence_evidence(
                restored,
                backup_manifest_sha256=marker["backup_manifest_sha256"],
                old_owner_id="source-owner",
                old_owner_epoch=1,
                new_owner_id="restored-owner",
                new_owner_epoch=2,
                fenced_at=_after_restore(marker, 1),
            )
            complete_restore_reconciliation(
                restored,
                controller=controller,
                reconciliation=_reconciliation(),
                fencing_evidence=(fence,),
                completed_at=_after_restore(marker, 2),
            )
            self.assertFalse(restore_requires_reconciliation(restored))

            controller.transfer_owner(
                new_owner_id="later-owner",
                old_sender_fenced=True,
                reconciled=True,
            )
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_missing_or_corrupt_restore_marker_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(restore_requires_reconciliation(root))
            marker = root / "RESTORE_RECONCILIATION_REQUIRED.json"
            marker.write_text("not-json", encoding="utf-8")
            self.assertTrue(restore_requires_reconciliation(root))


    def test_resealed_artifact_manifest_with_wrong_declared_size_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            manifest_path = next(
                (backup / "artifacts" / "manifests" / "sha256").rglob("*.json")
            )
            artifact_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            artifact_manifest["size_bytes"] += 1
            manifest_path.write_text(json.dumps(artifact_manifest), encoding="utf-8")
            _reseal_backup_manifest(backup)

            with self.assertRaisesRegex(
                BackupIntegrityError, "does not match its object"
            ):
                verify_backup(backup)

    def test_resealed_artifact_manifest_with_noncanonical_path_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            old_path = next(
                (backup / "artifacts" / "manifests" / "sha256").rglob("*.json")
            )
            wrong_dir = old_path.parent.parent / "ff"
            wrong_dir.mkdir(parents=True, exist_ok=True)
            new_path = wrong_dir / old_path.name
            old_path.replace(new_path)

            outer = json.loads((backup / "backup-manifest.json").read_text(encoding="utf-8"))
            old_relative = old_path.relative_to(backup).as_posix()
            new_relative = new_path.relative_to(backup).as_posix()
            for item in outer["files"]:
                if item["path"] == old_relative:
                    item["path"] = new_relative
                    break
            payload = (
                json.dumps(
                    outer,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            (backup / "backup-manifest.json").write_bytes(payload)
            (backup / "backup-manifest.sha256").write_text(
                f"sha256:{sha256(payload).hexdigest()}\n",
                encoding="ascii",
            )

            with self.assertRaisesRegex(
                BackupIntegrityError, "outside the canonical inventory"
            ):
                verify_backup(backup)

    def test_resealed_inventory_kind_spoof_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            outer = json.loads((backup / "backup-manifest.json").read_text(encoding="utf-8"))
            object_item = next(
                item for item in outer["files"] if item["kind"] == "artifact-object"
            )
            object_item["kind"] = "runtime-state"
            payload = (
                json.dumps(
                    outer,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            (backup / "backup-manifest.json").write_bytes(payload)
            (backup / "backup-manifest.sha256").write_text(
                f"sha256:{sha256(payload).hexdigest()}\n",
                encoding="ascii",
            )

            with self.assertRaisesRegex(
                BackupIntegrityError, "kind does not match canonical path"
            ):
                verify_backup(backup)


    def test_resealed_backup_rejects_gapped_journal_migration_history(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            journal = backup / "state" / "journal.sqlite3"

            connection = sqlite3.connect(journal)
            try:
                current = int(
                    connection.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0]
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (current + 2, "2026-09-25T00:00:00Z"),
                )
                connection.commit()
            finally:
                connection.close()

            _reseal_backup_manifest(backup)
            with self.assertRaisesRegex(
                BackupIntegrityError, "migration history is not contiguous"
            ):
                verify_backup(backup)


if __name__ == "__main__":
    unittest.main()
