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

from mvp.autotrade_mvp import backup as backup_module
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
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.autotrade_mvp.pipeline import run_vertical_slice
from mvp.autotrade_mvp.simulation_session import run_autonomous_simulation


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

    def test_fsync_file_flushes_regular_payload_without_mutating_bytes(self):
        with TemporaryDirectory() as directory:
            payload = Path(directory) / "payload.bin"
            payload.write_bytes(b"durable-payload")
            _fsync_file(payload)
            self.assertEqual(payload.read_bytes(), b"durable-payload")

    def test_fsync_file_rejects_symlink_or_non_file(self):
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.bin"
            with self.assertRaises(BackupIntegrityError):
                _fsync_file(missing)

class BackupRestoreTests(unittest.TestCase):
    def _record_durable_ready(
        self,
        controller: RecoveryController,
        store: JournalStore,
        *,
        reconciliation_id: str,
    ) -> str:
        owner = controller.owner
        self.assertIsNotNone(owner)
        result = _reconciliation()
        checkpoint = record_reconciliation_checkpoint(
            store,
            reconciliation_id=reconciliation_id,
            result=result,
            observed_at="2026-09-25T08:00:02Z",
            host_id=owner.owner_id,
            owner_epoch=str(owner.epoch),
        )
        accepted = controller.record_reconciliation_checkpoint(
            reconciliation_id=reconciliation_id,
            provider_id=result.provider_id,
            account_id=result.account_id,
            environment=result.environment,
        )
        self.assertEqual(accepted["event_id"], checkpoint["event_id"])
        return str(checkpoint["event_id"])

    def _build_sources(self, root: Path) -> tuple[Path, Path]:
        state = root / "state"
        artifacts = root / "artifacts"
        run_vertical_slice([100, 101, 102, 103], state)
        _artifact_store(artifacts)
        return state, artifacts

    def _build_autonomous_sources(
        self,
        root: Path,
        *,
        stop_after_episodes: int = 2,
    ) -> tuple[Path, Path]:
        state = root / "state"
        artifacts = root / "artifacts"
        run_autonomous_simulation(
            ["100", "101", "103", "102", "100"],
            state,
            run_id="backup-runtime-checkpoint",
            now="2026-10-03T00:00:00Z",
            stop_after_episodes=stop_after_episodes,
        )
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

    def test_autonomous_runtime_checkpoint_is_quarantined_not_reauthorized(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_autonomous_sources(root)
            checkpoint = state / "autonomous-runtime-checkpoint.json"
            authority_key = state / ".autonomous-runtime-authority.key"
            checkpoint_bytes = checkpoint.read_bytes()
            self.assertEqual(len(authority_key.read_bytes()), 32)

            backup = create_backup(state, artifacts, root / "backup")
            manifest = verify_backup(backup)
            self.assertEqual(manifest["schema_version"], BACKUP_SCHEMA_VERSION)
            self.assertEqual(
                manifest["runtime_checkpoint_evidence"],
                "QUARANTINED",
            )
            entries = {
                item["path"]: item
                for item in manifest["files"]
            }
            evidence_path = (
                "restore-evidence/autonomous-runtime-checkpoint.json"
            )
            self.assertEqual(
                entries[evidence_path]["kind"],
                "runtime-checkpoint-evidence",
            )
            self.assertEqual(
                (backup / evidence_path).read_bytes(),
                checkpoint_bytes,
            )
            self.assertFalse(
                (backup / "state" / "autonomous-runtime-checkpoint.json").exists()
            )
            self.assertFalse(
                any(
                    path.name == ".autonomous-runtime-authority.key"
                    for path in backup.rglob("*")
                )
            )

            restored = restore_backup(backup, root / "restored")
            marker = json.loads(
                (
                    restored / "RESTORE_RECONCILIATION_REQUIRED.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                marker["runtime_checkpoint_evidence"],
                "QUARANTINED",
            )
            self.assertTrue(
                marker["runtime_checkpoint_reconstitution_required"]
            )
            self.assertEqual(
                marker["runtime_checkpoint_evidence_sha256"],
                entries[evidence_path]["sha256"],
            )
            self.assertEqual(
                (restored / evidence_path).read_bytes(),
                checkpoint_bytes,
            )
            self.assertFalse(
                (
                    restored
                    / "state"
                    / "autonomous-runtime-checkpoint.json"
                ).exists()
            )
            self.assertFalse(
                any(
                    path.name == ".autonomous-runtime-authority.key"
                    for path in restored.rglob("*")
                )
            )
            self.assertTrue(restore_requires_reconciliation(restored))


    def test_quarantined_runtime_checkpoint_keeps_restore_gate_closed_until_reconstitution(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_autonomous_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            marker = json.loads(
                (
                    restored / "RESTORE_RECONCILIATION_REQUIRED.json"
                ).read_text(encoding="utf-8")
            )

            self.assertTrue(marker["runtime_checkpoint_reconstitution_required"])
            self.assertTrue(restore_requires_reconciliation(restored))
            with self.assertRaisesRegex(
                BackupError,
                "fresh runtime checkpoint authority reconstitution",
            ):
                complete_restore_reconciliation(
                    restored,
                    controller=None,
                    reconciliation_checkpoint_event_id="unused",
                    fencing_evidence=(),
                    completed_at=marker["restored_at"],
                )

            self.assertTrue(restore_requires_reconciliation(restored))
            self.assertFalse(
                (restored / "RESTORE_RECONCILIATION_COMPLETE.json").exists()
            )

    def test_runtime_checkpoint_created_during_sqlite_snapshot_aborts_backup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            target = root / "backup"
            checkpoint = state / "autonomous-runtime-checkpoint.json"
            self.assertFalse(checkpoint.exists())
            original_backup_sqlite = backup_module._backup_sqlite
            injected = False

            def backup_then_create_checkpoint(source, destination):
                nonlocal injected
                result = original_backup_sqlite(source, destination)
                if not injected:
                    injected = True
                    checkpoint.write_bytes(
                        b'{"sealed":"post-journal-generation"}\n'
                    )
                return result

            with patch.object(
                backup_module,
                "_backup_sqlite",
                side_effect=backup_then_create_checkpoint,
            ):
                with self.assertRaisesRegex(
                    BackupError,
                    "runtime checkpoint inventory changed across journal snapshot",
                ):
                    create_backup(state, artifacts, target)

            self.assertTrue(injected)
            self.assertFalse(target.exists())
            self.assertFalse(any(root.glob(".autotrade-backup-*")))

    def test_runtime_checkpoint_journal_backing_replacement_aborts_backup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_autonomous_sources(root)
            target = root / "backup"
            journal = state / "journal.sqlite3"
            replacement = root / "replacement-journal.sqlite3"
            with closing(sqlite3.connect(journal)) as source_db:
                with closing(sqlite3.connect(replacement)) as replacement_db:
                    source_db.backup(replacement_db)
                    replacement_db.commit()

            original_backup_sqlite = backup_module._backup_sqlite
            injected = False

            def replace_backing_then_snapshot(source, destination):
                nonlocal injected
                if not injected:
                    injected = True
                    replacement.replace(source)
                    for suffix in ("-wal", "-shm"):
                        try:
                            Path(str(source) + suffix).unlink()
                        except FileNotFoundError:
                            pass
                return original_backup_sqlite(source, destination)

            with patch.object(
                backup_module,
                "_backup_sqlite",
                side_effect=replace_backing_then_snapshot,
            ):
                with self.assertRaisesRegex(
                    BackupError,
                    "journal generation changed across snapshot",
                ):
                    create_backup(state, artifacts, target)

            self.assertTrue(injected)
            self.assertFalse(target.exists())
            self.assertFalse(any(root.glob(".autotrade-backup-*")))

    def test_runtime_checkpoint_advance_during_sqlite_snapshot_aborts_backup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            target = root / "backup"
            checkpoint = state / "autonomous-runtime-checkpoint.json"
            checkpoint.write_bytes(b'{"sealed":"generation-a"}\n')
            original_backup_sqlite = backup_module._backup_sqlite
            injected = False

            def backup_then_advance_checkpoint(source, destination):
                nonlocal injected
                result = original_backup_sqlite(source, destination)
                if not injected:
                    injected = True
                    checkpoint.write_bytes(b'{"sealed":"generation-b"}\n')
                return result

            with patch.object(
                backup_module,
                "_backup_sqlite",
                side_effect=backup_then_advance_checkpoint,
            ):
                with self.assertRaisesRegex(
                    BackupError,
                    "runtime checkpoint changed across journal snapshot",
                ):
                    create_backup(state, artifacts, target)

            self.assertTrue(injected)
            self.assertFalse(target.exists())
            self.assertFalse(any(root.glob(".autotrade-backup-*")))

    def test_runtime_checkpoint_aba_cannot_mix_with_newer_journal_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_autonomous_sources(
                root,
                stop_after_episodes=2,
            )
            target = root / "backup"
            checkpoint = state / "autonomous-runtime-checkpoint.json"
            checkpoint_a = checkpoint.read_bytes()
            original_backup_sqlite = backup_module._backup_sqlite
            injected = False

            def advance_journal_then_restore_checkpoint_a(source, destination):
                nonlocal injected
                if not injected:
                    injected = True
                    run_autonomous_simulation(
                        ["100", "101", "103", "102", "100"],
                        state,
                        run_id="backup-runtime-checkpoint",
                        now="2026-10-03T00:00:00Z",
                        stop_after_episodes=3,
                    )
                    self.assertNotEqual(checkpoint.read_bytes(), checkpoint_a)
                    checkpoint.write_bytes(checkpoint_a)
                return original_backup_sqlite(source, destination)

            with patch.object(
                backup_module,
                "_backup_sqlite",
                side_effect=advance_journal_then_restore_checkpoint_a,
            ):
                with self.assertRaisesRegex(
                    BackupError,
                    "does not match staged journal snapshot",
                ):
                    create_backup(state, artifacts, target)

            self.assertTrue(injected)
            self.assertEqual(checkpoint.read_bytes(), checkpoint_a)
            self.assertFalse(target.exists())
            self.assertFalse(any(root.glob(".autotrade-backup-*")))

    def test_runtime_checkpoint_journal_replacement_before_publish_aborts_backup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_autonomous_sources(root)
            target = root / "backup"
            journal = state / "journal.sqlite3"
            replacement = root / "late-replacement-journal.sqlite3"
            with closing(sqlite3.connect(journal)) as source_db:
                with closing(sqlite3.connect(replacement)) as replacement_db:
                    source_db.backup(replacement_db)
                    replacement_db.commit()

            original_fsync_tree = backup_module._fsync_directory_tree
            injected = False

            def fsync_then_replace_journal(stage_root):
                nonlocal injected
                result = original_fsync_tree(stage_root)
                if (
                    not injected
                    and Path(stage_root).name.startswith(".autotrade-backup-")
                ):
                    injected = True
                    replacement.replace(journal)
                    for suffix in ("-wal", "-shm"):
                        try:
                            Path(str(journal) + suffix).unlink()
                        except FileNotFoundError:
                            pass
                return result

            with patch.object(
                backup_module,
                "_fsync_directory_tree",
                side_effect=fsync_then_replace_journal,
            ):
                with self.assertRaisesRegex(
                    BackupError,
                    "journal generation changed before backup commit",
                ):
                    create_backup(state, artifacts, target)

            self.assertTrue(injected)
            self.assertFalse(target.exists())
            self.assertFalse(any(root.glob(".autotrade-backup-*")))

    def test_runtime_checkpoint_created_after_staging_fsync_aborts_backup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            target = root / "backup"
            checkpoint = state / "autonomous-runtime-checkpoint.json"
            self.assertFalse(checkpoint.exists())
            original_fsync_tree = backup_module._fsync_directory_tree
            injected = False

            def fsync_then_create_checkpoint(stage_root):
                nonlocal injected
                result = original_fsync_tree(stage_root)
                if (
                    not injected
                    and Path(stage_root).name.startswith(".autotrade-backup-")
                ):
                    injected = True
                    checkpoint.write_bytes(
                        b'{"sealed":"late-prepublication-generation"}\n'
                    )
                return result

            with patch.object(
                backup_module,
                "_fsync_directory_tree",
                side_effect=fsync_then_create_checkpoint,
            ):
                with self.assertRaisesRegex(
                    BackupError,
                    "runtime checkpoint inventory changed before backup commit",
                ):
                    create_backup(state, artifacts, target)

            self.assertTrue(injected)
            self.assertFalse(target.exists())
            self.assertFalse(any(root.glob(".autotrade-backup-*")))

    def test_runtime_checkpoint_manifest_claim_must_match_inventory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            manifest_path = backup / "backup-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["runtime_checkpoint_evidence"], "ABSENT")
            manifest["runtime_checkpoint_evidence"] = "QUARANTINED"
            manifest_path.write_text(
                json.dumps(
                    manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n",
                encoding="utf-8",
            )
            _reseal_backup_manifest(backup)
            with self.assertRaisesRegex(
                BackupIntegrityError,
                "claim does not match",
            ):
                verify_backup(backup)

    def test_restored_runtime_checkpoint_evidence_tamper_fails_marker_validation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_autonomous_sources(root)
            checkpoint = state / "autonomous-runtime-checkpoint.json"
            self.assertTrue(checkpoint.is_file())
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            evidence = (
                restored
                / "restore-evidence"
                / "autonomous-runtime-checkpoint.json"
            )
            evidence.write_bytes(b'{"sealed":"tampered"}\n')

            with self.assertRaisesRegex(
                BackupIntegrityError,
                "runtime checkpoint evidence digest mismatch",
            ):
                complete_restore_reconciliation(
                    restored,
                    controller=None,
                    reconciliation_checkpoint_event_id="unused",
                    fencing_evidence=(),
                    completed_at="2026-09-25T08:00:03Z",
                )

    def test_schema_v1_backup_remains_readable_without_runtime_checkpoint_claim(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            manifest_path = backup / "backup-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["runtime_checkpoint_evidence"], "ABSENT")
            manifest["schema_version"] = 1
            manifest.pop("runtime_checkpoint_evidence")
            manifest_path.write_text(
                json.dumps(
                    manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n",
                encoding="utf-8",
            )
            _reseal_backup_manifest(backup)

            verified = verify_backup(backup)
            self.assertEqual(verified["schema_version"], 1)
            restored = restore_backup(backup, root / "restored-v1")
            marker = json.loads(
                (
                    restored / "RESTORE_RECONCILIATION_REQUIRED.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                marker["runtime_checkpoint_evidence"],
                "UNAVAILABLE_LEGACY_BACKUP",
            )
            self.assertFalse(
                marker["runtime_checkpoint_reconstitution_required"]
            )
            self.assertIsNone(marker["runtime_checkpoint_evidence_sha256"])
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_restore_validation_never_recreates_missing_journal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            journal = restored / "state" / "journal.sqlite3"
            self.assertTrue(journal.is_file())
            journal.unlink()

            self.assertTrue(restore_requires_reconciliation(restored))
            self.assertFalse(journal.exists())
            with self.assertRaisesRegex(
                BackupIntegrityError,
                "journal is missing or unsafe",
            ):
                complete_restore_reconciliation(
                    restored,
                    controller=None,
                    reconciliation_checkpoint_event_id="unused",
                    fencing_evidence=(),
                    completed_at="2026-09-25T08:00:03Z",
                )
            self.assertFalse(journal.exists())
            self.assertFalse(
                (restored / "RESTORE_RECONCILIATION_COMPLETE.json").exists()
            )

    def test_restore_persists_journal_bound_provenance_before_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            marker = json.loads(
                (
                    restored / "RESTORE_RECONCILIATION_REQUIRED.json"
                ).read_text(encoding="utf-8")
            )
            store = JournalStore(restored / "state" / "journal.sqlite3")
            events = store.load_events("backup_restore", "restore-authority")

            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["event_type"], "BackupRestoreStaged")
            self.assertEqual(event["committed_at"], marker["restored_at"])
            self.assertEqual(
                event["payload"]["backup_manifest_sha256"],
                marker["backup_manifest_sha256"],
            )
            self.assertEqual(
                event["payload"]["runtime_checkpoint_evidence"],
                marker["runtime_checkpoint_evidence"],
            )
            self.assertEqual(
                event["payload"]["runtime_checkpoint_reconstitution_required"],
                marker["runtime_checkpoint_reconstitution_required"],
            )
            self.assertEqual(
                event["payload"]["runtime_checkpoint_evidence_sha256"],
                marker["runtime_checkpoint_evidence_sha256"],
            )
            self.assertEqual(
                event["payload"]["source_owner_scope"],
                marker["source_owner_scope"],
            )

    def test_restore_marker_cannot_override_journal_bound_manifest_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            marker_path = restored / "RESTORE_RECONCILIATION_REQUIRED.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker["backup_manifest_sha256"] = "sha256:" + ("0" * 64)
            marker_path.write_text(
                json.dumps(
                    marker,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertTrue(restore_requires_reconciliation(restored))
            with self.assertRaisesRegex(
                BackupIntegrityError,
                "conflicts with durable journal provenance",
            ):
                complete_restore_reconciliation(
                    restored,
                    controller=None,
                    reconciliation_checkpoint_event_id="unused",
                    fencing_evidence=(),
                    completed_at=marker["restored_at"],
                )
            self.assertFalse(
                (restored / "RESTORE_RECONCILIATION_COMPLETE.json").exists()
            )

    def test_journal_bound_restore_marker_cannot_downgrade_to_legacy(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            restored, controller, marker, checkpoint_id = self._restored_with_owner(root)
            marker_path = restored / "RESTORE_RECONCILIATION_REQUIRED.json"
            downgraded = json.loads(marker_path.read_text(encoding="utf-8"))
            downgraded.pop("runtime_checkpoint_evidence")
            downgraded.pop("runtime_checkpoint_reconstitution_required")
            downgraded.pop("runtime_checkpoint_evidence_sha256")
            marker_path.write_text(
                json.dumps(
                    downgraded,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertTrue(restore_requires_reconciliation(restored))
            with self.assertRaisesRegex(
                BackupIntegrityError,
                "cannot downgrade journal-bound restore provenance",
            ):
                complete_restore_reconciliation(
                    restored,
                    controller=controller,
                    reconciliation_checkpoint_event_id=checkpoint_id,
                    fencing_evidence=(),
                    completed_at=_after_restore(marker, 2),
                )
            self.assertFalse(
                (restored / "RESTORE_RECONCILIATION_COMPLETE.json").exists()
            )

    def test_legacy_marker_downgrade_cannot_hide_quarantined_checkpoint_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_autonomous_sources(root)
            self.assertTrue(
                (state / "autonomous-runtime-checkpoint.json").is_file()
            )
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            marker_path = restored / "RESTORE_RECONCILIATION_REQUIRED.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker.pop("runtime_checkpoint_evidence")
            marker.pop("runtime_checkpoint_reconstitution_required")
            marker.pop("runtime_checkpoint_evidence_sha256")
            marker_path.write_text(
                json.dumps(
                    marker,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertTrue(restore_requires_reconciliation(restored))
            with self.assertRaisesRegex(
                BackupIntegrityError,
                "cannot coexist with runtime checkpoint evidence",
            ):
                complete_restore_reconciliation(
                    restored,
                    controller=None,
                    reconciliation_checkpoint_event_id="unused",
                    fencing_evidence=(),
                    completed_at="2026-09-25T08:00:03Z",
                )

    def test_checkpoint_evidence_deletion_cannot_downgrade_journal_bound_restore(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_autonomous_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            marker_path = restored / "RESTORE_RECONCILIATION_REQUIRED.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker.pop("runtime_checkpoint_evidence")
            marker.pop("runtime_checkpoint_reconstitution_required")
            marker.pop("runtime_checkpoint_evidence_sha256")
            (
                restored
                / "restore-evidence"
                / "autonomous-runtime-checkpoint.json"
            ).unlink()
            marker_path.write_text(
                json.dumps(
                    marker,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertTrue(restore_requires_reconciliation(restored))
            with self.assertRaisesRegex(
                BackupIntegrityError,
                "cannot downgrade journal-bound restore provenance",
            ):
                complete_restore_reconciliation(
                    restored,
                    controller=None,
                    reconciliation_checkpoint_event_id="unused",
                    fencing_evidence=(),
                    completed_at="2026-09-25T08:00:03Z",
                )
            self.assertFalse(
                (restored / "RESTORE_RECONCILIATION_COMPLETE.json").exists()
            )

    def test_schema_v1_cannot_smuggle_runtime_checkpoint_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            evidence = (
                backup
                / "restore-evidence"
                / "autonomous-runtime-checkpoint.json"
            )
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_bytes(b'{"sealed":"smuggled"}\n')

            manifest_path = backup / "backup-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = 1
            manifest.pop("runtime_checkpoint_evidence")
            manifest["files"].append(
                {
                    "path": "restore-evidence/autonomous-runtime-checkpoint.json",
                    "sha256": "sha256:" + sha256(evidence.read_bytes()).hexdigest(),
                    "size_bytes": evidence.stat().st_size,
                    "kind": "runtime-checkpoint-evidence",
                }
            )
            manifest_path.write_text(
                json.dumps(
                    manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n",
                encoding="utf-8",
            )
            _reseal_backup_manifest(backup)

            with self.assertRaisesRegex(
                BackupIntegrityError,
                "schema v1 cannot carry",
            ):
                verify_backup(backup)

    def test_partial_runtime_checkpoint_marker_binding_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            marker_path = restored / "RESTORE_RECONCILIATION_REQUIRED.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker.pop("runtime_checkpoint_evidence_sha256")
            marker_path.write_text(
                json.dumps(
                    marker,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertTrue(restore_requires_reconciliation(restored))
            with self.assertRaisesRegex(
                BackupIntegrityError,
                "binding is partial",
            ):
                complete_restore_reconciliation(
                    restored,
                    controller=None,
                    reconciliation_checkpoint_event_id="unused",
                    fencing_evidence=(),
                    completed_at="2026-09-25T08:00:03Z",
                )

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

    def test_restore_discovers_non_default_recovery_owner_scope(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            source_store = JournalStore(state / "journal.sqlite3")
            source = RecoveryController(
                owner_store=source_store,
                owner_scope="PAPER:acct-non-default",
            )
            source.start("source-owner")

            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            marker = json.loads(
                (restored / "RESTORE_RECONCILIATION_REQUIRED.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(
                marker["source_owner_scope"],
                "PAPER:acct-non-default",
            )
            self.assertEqual(marker["source_owner_id"], "source-owner")
            self.assertEqual(marker["source_owner_epoch"], 1)

    def test_restore_fails_closed_when_multiple_owner_scopes_are_ambiguous(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            store = JournalStore(state / "journal.sqlite3")
            first = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct-a",
            )
            second = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct-b",
            )
            first.start("source-a")
            second.start("source-b")

            backup = create_backup(state, artifacts, root / "backup")
            with self.assertRaisesRegex(
                BackupIntegrityError,
                "multiple recovery owner scopes",
            ):
                restore_backup(backup, root / "restored")
            self.assertFalse((root / "restored").exists())

    def _restored_with_owner(
        self,
        root: Path,
    ) -> tuple[Path, RecoveryController, dict[str, object], str]:
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
        checkpoint_id = self._record_durable_ready(
            controller, restored_store, reconciliation_id="restore-readiness"
        )
        return restored, controller, marker, checkpoint_id

    def test_restore_completion_requires_reconciliation_and_immutable_fence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            restored, controller, marker, checkpoint_id = self._restored_with_owner(root)
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
                reconciliation_checkpoint_event_id=checkpoint_id,
                fencing_evidence=(fence,),
                completed_at=_after_restore(marker, 2),
            )

            self.assertEqual(proof["current_owner_id"], "restored-owner")
            self.assertEqual(proof["current_owner_epoch"], 2)
            self.assertFalse(restore_requires_reconciliation(restored))

    def test_restore_completion_rejects_unknown_reconciliation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            restored, controller, marker, checkpoint_id = self._restored_with_owner(root)
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
            store = JournalStore(restored / "state" / "journal.sqlite3")
            owner = controller.owner
            self.assertIsNotNone(owner)
            incomplete = record_reconciliation_checkpoint(
                store,
                reconciliation_id="restore-incomplete",
                result=_reconciliation(
                    complete=False,
                    blocking_resources=("ACCOUNT",),
                    resolutions=(unknown,),
                ),
                observed_at="2026-09-25T08:00:03Z",
                host_id=owner.owner_id,
                owner_epoch=str(owner.epoch),
            )
            with self.assertRaisesRegex(
                BackupError,
                "complete non-blocking reconciliation",
            ):
                complete_restore_reconciliation(
                    restored,
                    controller=controller,
                    reconciliation_checkpoint_event_id=str(incomplete["event_id"]),
                    fencing_evidence=(fence,),
                    completed_at=_after_restore(marker, 2),
                )
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_restore_completion_rejects_controller_bound_to_other_journal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            restored, _, marker, _checkpoint_id = self._restored_with_owner(root)
            other_store = JournalStore(root / "other" / "journal.sqlite3")
            other = RecoveryController(owner_store=other_store)
            other.start("other-owner")
            other_checkpoint_id = self._record_durable_ready(
                other, other_store, reconciliation_id="other-readiness"
            )
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
                    reconciliation_checkpoint_event_id=other_checkpoint_id,
                    fencing_evidence=(fence,),
                    completed_at=_after_restore(marker, 2),
                )

    def test_restore_completion_requires_fence_for_every_owner_epoch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            restored, controller, marker, checkpoint_id = self._restored_with_owner(root)
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
            checkpoint_id = self._record_durable_ready(
                controller,
                JournalStore(restored / "state" / "journal.sqlite3"),
                reconciliation_id="replacement-readiness",
            )
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
                    reconciliation_checkpoint_event_id=checkpoint_id,
                    fencing_evidence=(first,),
                    completed_at=_after_restore(marker, 3),
                )

            complete_restore_reconciliation(
                restored,
                controller=controller,
                reconciliation_checkpoint_event_id=checkpoint_id,
                fencing_evidence=(first, second),
                completed_at=_after_restore(marker, 3),
            )
            self.assertFalse(restore_requires_reconciliation(restored))

    def test_restore_completion_semantic_tamper_fails_closed_even_if_rehashed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            restored, controller, marker, checkpoint_id = self._restored_with_owner(root)
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
                reconciliation_checkpoint_event_id=checkpoint_id,
                fencing_evidence=(fence,),
                completed_at=_after_restore(marker, 2),
            )
            proof_path = restored / "RESTORE_RECONCILIATION_COMPLETE.json"
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            proof["reconciliation"]["summary"]["complete"] = False
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

    def test_newer_reconciliation_checkpoint_reopens_restore_gate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            restored, controller, marker, checkpoint_id = self._restored_with_owner(root)
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
                reconciliation_checkpoint_event_id=checkpoint_id,
                fencing_evidence=(fence,),
                completed_at=_after_restore(marker, 2),
            )
            self.assertFalse(restore_requires_reconciliation(restored))

            store = JournalStore(restored / "state" / "journal.sqlite3")
            owner = controller.owner
            self.assertIsNotNone(owner)
            newer = record_reconciliation_checkpoint(
                store,
                reconciliation_id="restore-readiness-superseding",
                result=_reconciliation(),
                observed_at="2026-09-25T08:00:04Z",
                host_id=owner.owner_id,
                owner_epoch=str(owner.epoch),
            )
            self.assertNotEqual(newer["event_id"], checkpoint_id)
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_new_sender_epoch_after_completion_reopens_restore_gate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            restored, controller, marker, checkpoint_id = self._restored_with_owner(root)
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
                reconciliation_checkpoint_event_id=checkpoint_id,
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
