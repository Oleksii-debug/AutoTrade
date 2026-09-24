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
    UnknownSubmission,
    reconcile_account,
)
from mvp.autotrade_mvp.recovery import HostState, RecoveryController


FENCING_EVIDENCE = [
    {
        "artifact_id": "11111111-1111-4111-8111-111111111111",
        "sha256": "sha256:" + "a" * 64,
        "observed_at": "2026-09-24T19:58:00Z",
        "rights_id": "recovery:fencing",
    },
    {
        "artifact_id": "22222222-2222-4222-8222-222222222222",
        "sha256": "sha256:" + "b" * 64,
        "observed_at": "2026-09-24T19:59:00Z",
        "rights_id": "recovery:provider-session",
    },
]


def _restore_instant(restored: Path, seconds: int) -> str:
    marker = json.loads(
        (restored / "RESTORE_RECONCILIATION_REQUIRED.json").read_text(encoding="utf-8")
    )
    base = datetime.fromisoformat(marker["restored_at"].replace("Z", "+00:00"))
    value = base.astimezone(timezone.utc) + timedelta(seconds=seconds)
    return value.isoformat().replace("+00:00", "Z")


def _fencing_evidence(restored: Path | None = None):
    values = [dict(item) for item in FENCING_EVIDENCE]
    if restored is not None:
        for index, item in enumerate(values, start=1):
            item["observed_at"] = _restore_instant(restored, index)
    return values


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


def _reconciliation(*, complete: bool = True):
    return reconcile_account(
        local_cash={"USD": "1000"},
        provider_cash={"USD": "1000"},
        local_positions={},
        provider_positions={},
        local_execution_ids=[],
        provider_fills=[],
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

    def test_completed_restore_reconciliation_can_clear_gate_durably(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            self.assertTrue(restore_requires_reconciliation(restored))

            controller = RecoveryController()
            owner = controller.start("restored-host")
            proof = complete_restore_reconciliation(
                restored,
                controller=controller,
                reconciliation=_reconciliation(),
                fencing_evidence=_fencing_evidence(restored),
                completed_at=_completed_at(restored),
            )

            self.assertEqual(controller.state, HostState.READY)
            self.assertEqual(proof["owner_id"], owner.owner_id)
            self.assertEqual(proof["blocking_resources"], [])
            self.assertFalse(restore_requires_reconciliation(restored))
            self.assertFalse(restore_requires_reconciliation(restored))

    def test_resolved_absence_completion_uses_current_reconciliation_contract(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            controller = RecoveryController()
            controller.start("restored-host")

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
            controller = RecoveryController()
            controller.start("restored-host")

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
            controller = RecoveryController()
            controller.start("restored-host")

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
            controller = RecoveryController()
            controller.start("restored-host")
            with self.assertRaisesRegex(BackupError, "canonical EvidenceRef"):
                complete_restore_reconciliation(
                    restored,
                    controller=controller,
                    reconciliation=_reconciliation(),
                    fencing_evidence=["old-host:fenced"],
                    completed_at=_completed_at(restored),
                )
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_duplicate_or_future_fencing_evidence_never_clears_gate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            controller = RecoveryController()
            controller.start("restored-host")

            duplicate = _fencing_evidence(restored)[:1] * 2
            with self.assertRaisesRegex(BackupError, "unique"):
                complete_restore_reconciliation(
                    restored,
                    controller=controller,
                    reconciliation=_reconciliation(),
                    fencing_evidence=duplicate,
                    completed_at=_completed_at(restored),
                )

            future = _fencing_evidence(restored)[:1]
            future[0]["observed_at"] = _restore_instant(restored, 11)
            with self.assertRaisesRegex(BackupError, "postdate"):
                complete_restore_reconciliation(
                    restored,
                    controller=controller,
                    reconciliation=_reconciliation(),
                    fencing_evidence=future,
                    completed_at=_completed_at(restored),
                )
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_stale_or_semantically_unbound_fencing_evidence_cannot_clear_gate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            controller = RecoveryController()
            controller.start("restored-host")

            stale = _fencing_evidence(restored)[:1]
            stale[0]["observed_at"] = _restore_instant(restored, -1)
            with self.assertRaisesRegex(BackupError, "predate"):
                complete_restore_reconciliation(
                    restored,
                    controller=controller,
                    reconciliation=_reconciliation(),
                    fencing_evidence=stale,
                    completed_at=_completed_at(restored),
                )

            unrelated = _fencing_evidence(restored)[:1]
            unrelated[0]["rights_id"] = "recovery:unrelated"
            with self.assertRaisesRegex(BackupError, "recovery:fencing"):
                complete_restore_reconciliation(
                    restored,
                    controller=controller,
                    reconciliation=_reconciliation(),
                    fencing_evidence=unrelated,
                    completed_at=_completed_at(restored),
                )
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_tampered_restore_completion_proof_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            controller = RecoveryController()
            controller.start("restored-host")
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
            controller = RecoveryController()
            controller.start("restored-host")
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

    def test_boolean_owner_epoch_cannot_clear_restore_gate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state, artifacts = self._build_sources(root)
            backup = create_backup(state, artifacts, root / "backup")
            restored = restore_backup(backup, root / "restored")
            controller = RecoveryController()
            controller.start("restored-host")
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
            object_files = [
                path
                for path in (backup / "artifacts" / "objects" / "sha256").rglob("*")
                if path.is_file()
            ]
            self.assertEqual(len(object_files), 1)
            object_files[0].unlink()
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
