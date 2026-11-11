from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research.autotrade_research.artifacts.resource_lock import ResourceLock
from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp.qualification_attestation import QualificationTrustPolicy
from mvp.autotrade_mvp.windows_update_bridge import (
    WindowsUpdateBridgeError,
    WindowsUpdateBridgeRequest,
    _assert_prepare_receipt,
    _runtime_locks,
    _safe_extract,
    run_windows_update,
)
from mvp.tests.test_windows_update import (
    CANDIDATE_SOURCE,
    CURRENT_SOURCE,
    _trust_root,
    frozen_release,
    release_artifact_bytes,
)


class _Controller:
    def __init__(self, *, fail_first_resume=False):
        self.actions = []
        self.fail_first_resume = fail_first_resume

    @staticmethod
    def stopped(action, request_id):
        return {
            "schema_version": "1.0.0",
            "request_id": request_id,
            "action": action,
            "status": "STOPPED_CHECKPOINTED",
            "financial_process_running": False,
            "financial_sender_fenced": True,
            "checkpoint_confirmed": True,
            "learning_checkpoint_confirmed": True,
            "trading_authority": False,
        }

    @staticmethod
    def resumed(action, request_id):
        return {
            "schema_version": "1.0.0",
            "request_id": request_id,
            "action": action,
            "status": "DEGRADED_RESEARCH_ONLY",
            "financial_sender_fenced": True,
            "checkpoint_confirmed": True,
            "learning_checkpoint_confirmed": True,
            "learning_resumed": True,
            "identity_validated": True,
            "provider_sessions_reestablished": True,
            "reconciliation_completed": True,
            "host_ui_compatible": True,
            "trading_authority": False,
        }

    def request(self, action, payload, *, directory):
        del directory
        self.actions.append(action)
        request_id = payload["request_id"]
        if action in {"prepare-update", "prepare-rollback"}:
            return self.stopped(action, request_id)
        if action == "restore-backup":
            return {
                "schema_version": "1.0.0",
                "request_id": request_id,
                "action": action,
                "status": "BACKUP_RESTORED",
                "reconciliation_required": True,
                "trading_authority": False,
            }
        if action == "resume-degraded" and self.fail_first_resume:
            self.fail_first_resume = False
            result = self.resumed(action, request_id)
            result["learning_resumed"] = False
            return result
        if action in {"resume-degraded", "resume-rollback-degraded"}:
            return self.resumed(action, request_id)
        raise AssertionError(action)


class WindowsUpdateBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared_temp = TemporaryDirectory()
        cls.shared_root = Path(cls.shared_temp.name).resolve()
        cls.evidence = cls.shared_root / "evidence"
        cls.store = ArtifactStore(cls.evidence)
        cls.trust_root = _trust_root()
        cls.policy = QualificationTrustPolicy(
            policy_version="2026.09", roots=(cls.trust_root,)
        )
        cls.policy_patch = patch(
            "mvp.autotrade_mvp.qualification_attestation."
            "load_canonical_qualification_trust_policy",
            return_value=cls.policy,
        )
        cls.policy_patch.start()
        cls.current = frozen_release(
            cls.store,
            cls.evidence,
            cls.trust_root,
            cls.policy,
            "autotrade-1.0.0",
            CURRENT_SOURCE,
            3,
        )
        cls.candidate = frozen_release(
            cls.store,
            cls.evidence,
            cls.trust_root,
            cls.policy,
            "autotrade-1.1.0",
            CANDIDATE_SOURCE,
            5,
        )

    @classmethod
    def tearDownClass(cls):
        cls.policy_patch.stop()
        cls.shared_temp.cleanup()

    def setUp(self):
        self.run_temp = TemporaryDirectory(dir=self.shared_root)
        self.root = Path(self.run_temp.name).resolve()
        self.current_manifest = self.root / "current-release.json"
        self.candidate_manifest = self.root / "candidate-release.json"
        self.current_manifest.write_text(self.current.manifest_json, encoding="utf-8")
        self.candidate_manifest.write_text(self.candidate.manifest_json, encoding="utf-8")
        self.package = self.root / "candidate.zip"
        self.package.write_bytes(
            release_artifact_bytes(
                "autotrade-1.1.0", CANDIDATE_SOURCE, 5, "WINDOWS_PACKAGE"
            )
        )
        self.descriptor = {
            "schema_version": "1.0.0",
            "current_release_id": "autotrade-1.0.0",
            "candidate_release_id": "autotrade-1.1.0",
            "candidate_manifest_url": "https://updates.invalid/candidate.json",
            "candidate_manifest_sha256": self.candidate.manifest_sha256,
            "candidate_package_url": "https://updates.invalid/candidate.zip",
            "candidate_package_sha256": "sha256:" + sha256(self.package.read_bytes()).hexdigest(),
            "current_journal_schema_version": 1,
            "candidate_journal_schema_version": 1,
            "migration_evidence": None,
        }
        self.descriptor_path = self.root / "descriptor.json"
        self.descriptor_path.write_text(json.dumps(self.descriptor), encoding="utf-8")
        self.request = WindowsUpdateBridgeRequest(
            descriptor_url="https://updates.invalid/descriptor.json",
            descriptor_sha256="sha256:" + sha256(self.descriptor_path.read_bytes()).hexdigest(),
            current_release_manifest=self.current_manifest,
            evidence_root=self.evidence,
            state_dir=self.root / "data" / "state",
            artifact_root=self.root / "data" / "artifacts",
            data_root=self.root / "data",
            install_root=self.root / "program",
            backup_root=self.root / "backups",
            work_root=self.root / "updater-state",
            controller_executable=self.root / "controller.exe",
        )

    def tearDown(self):
        self.run_temp.cleanup()

    def _download(self, url, destination, *, expected_sha256, maximum):
        del maximum
        source = {
            self.request.descriptor_url: self.descriptor_path,
            self.descriptor["candidate_manifest_url"]: self.candidate_manifest,
            self.descriptor["candidate_package_url"]: self.package,
        }[url]
        payload = source.read_bytes()
        self.assertEqual("sha256:" + sha256(payload).hexdigest(), expected_sha256)
        destination.write_bytes(payload)
        return destination

    @staticmethod
    def _backup(state, artifacts, destination):
        del state, artifacts
        destination.mkdir()
        (destination / "backup-manifest.sha256").write_text(
            "sha256:" + "c" * 64 + "\n", encoding="ascii"
        )
        return destination

    @staticmethod
    def _verify_backup(destination):
        del destination
        return {
            "journal_schema_version": 1,
            "reconciliation_required_after_restore": True,
        }

    @staticmethod
    def _extract(package, install_root, release_id, source_sha):
        del package
        destination = install_root / "releases" / f"{release_id}-{source_sha[:12]}"
        destination.mkdir(parents=True)
        (destination / "AutoTrade.exe").write_bytes(b"qualified")
        return destination

    def _run(self, controller):
        with (
            patch("mvp.autotrade_mvp.windows_update_bridge._download", side_effect=self._download),
            patch("mvp.autotrade_mvp.windows_update_bridge.create_backup", side_effect=self._backup),
            patch("mvp.autotrade_mvp.windows_update_bridge.verify_backup", side_effect=self._verify_backup),
            patch("mvp.autotrade_mvp.windows_update_bridge._safe_extract", side_effect=self._extract),
        ):
            return run_windows_update(self.request, controller=controller)

    def test_success_requires_stop_backup_and_resumes_learning_without_authority(self):
        controller = _Controller()
        result = self._run(controller)
        self.assertEqual(result["status"], "UPDATED_DEGRADED_RESEARCH_ONLY")
        self.assertTrue(result["learning_resumed"])
        self.assertFalse(result["trading_authority"])
        self.assertEqual(controller.actions, ["prepare-update", "resume-degraded"])
        active = json.loads((self.request.install_root / "active-release.json").read_text())
        self.assertEqual(active["release_id"], "autotrade-1.1.0")
        checkpoint_doc = json.loads(
            (self.request.work_root / "update-checkpoint.json").read_text()
        )
        checkpoint_json = json.loads(checkpoint_doc["checkpoint_json"])
        self.assertEqual(active["plan_sha256"], checkpoint_json["plan_sha256"])
        self.assertEqual(len(checkpoint_json["update_completed_steps"]), 13)

    def test_failed_resume_rolls_back_pointer_and_external_backup(self):
        previous_directory = self.request.install_root / "releases" / "old"
        previous_directory.mkdir(parents=True)
        previous = {
            "schema_version": "1.0.0",
            "release_id": "autotrade-1.0.0",
            "source_sha": CURRENT_SOURCE,
            "package_sha256": "sha256:" + "a" * 64,
            "path": str(previous_directory),
            "plan_sha256": "sha256:" + "b" * 64,
        }
        (self.request.install_root / "active-release.json").write_text(
            json.dumps(previous), encoding="utf-8"
        )
        controller = _Controller(fail_first_resume=True)
        with self.assertRaisesRegex(
            WindowsUpdateBridgeError, "runtime did not resume safely"
        ):
            self._run(controller)
        restored = json.loads(
            (self.request.install_root / "active-release.json").read_text()
        )
        self.assertEqual(restored, previous)
        self.assertEqual(
            controller.actions,
            [
                "prepare-update",
                "resume-degraded",
                "prepare-rollback",
                "restore-backup",
                "resume-rollback-degraded",
            ],
        )

    def test_runtime_lock_blocks_update_even_after_claimed_stop(self):
        lock_path = self.request.state_dir / "journal.sqlite3.host.lock"
        lock_path.parent.mkdir(parents=True)
        with ResourceLock(lock_path):
            with self.assertRaisesRegex(
                WindowsUpdateBridgeError, "still running"
            ):
                _runtime_locks(self.request)

    def test_stop_receipt_cannot_omit_learning_checkpoint(self):
        receipt = _Controller.stopped("prepare-update", "request")
        receipt["learning_checkpoint_confirmed"] = False
        with self.assertRaisesRegex(
            WindowsUpdateBridgeError, "checkpointed STOP"
        ):
            _assert_prepare_receipt(
                receipt, request_id="request", action="prepare-update"
            )

    def test_safe_extract_rejects_parent_traversal(self):
        archive = self.root / "unsafe.zip"
        import zipfile

        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("../AutoTrade.exe", b"unsafe")
        with self.assertRaisesRegex(WindowsUpdateBridgeError, "unsafe paths"):
            _safe_extract(
                archive,
                self.request.install_root,
                "autotrade-1.1.0",
                CANDIDATE_SOURCE,
            )

    def test_request_rejects_data_inside_replaceable_program_tree(self):
        with self.assertRaisesRegex(WindowsUpdateBridgeError, "must be separate"):
            WindowsUpdateBridgeRequest(
                descriptor_url=self.request.descriptor_url,
                descriptor_sha256=self.request.descriptor_sha256,
                current_release_manifest=self.current_manifest,
                evidence_root=self.evidence,
                state_dir=self.request.install_root / "data" / "state",
                artifact_root=self.request.install_root / "data" / "artifacts",
                data_root=self.request.install_root / "data",
                install_root=self.request.install_root,
                backup_root=self.request.backup_root,
                work_root=self.request.work_root,
                controller_executable=self.request.controller_executable,
            )


if __name__ == "__main__":
    unittest.main()
