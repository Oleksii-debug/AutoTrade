import gc
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from autotrade_research.artifacts.store import ArtifactIntegrityError, ArtifactStore
from autotrade_research.artifacts.resource_lock import ResourceLockBusyError
from autotrade_research.artifacts import _root_authority as root_authority
from autotrade_research.artifacts import (
    _retained_publication_hardening as posix_publication,
)
from autotrade_research.artifacts import (
    _windows_retained_publication_hardening as windows_publication,
)


class ArtifactRootAuthorityTests(unittest.TestCase):
    def _publish(self, store: ArtifactStore, payload: bytes = b"root-authority"):
        return store.publish_bytes(
            artifact_id=str(uuid4()),
            data=payload,
            media_type="application/octet-stream",
            rights={"storage": True, "export": False},
        )

    def test_whole_root_replacement_invalidates_old_store_generation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            detached = Path(directory) / "store-detached"
            first = ArtifactStore(root)
            os.replace(root, detached)
            second = ArtifactStore(root)
            try:
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "root changed after initialization",
                ):
                    self._publish(first, b"must-not-report-success-from-r1")

                manifest = self._publish(second, b"r2-is-current")
                self.assertTrue(
                    second._manifest_path(manifest["artifact_id"]).exists()
                )
                self.assertEqual(list((detached / "manifests").glob("*.json")), [])
            finally:
                del first
                del second
                gc.collect()

    def test_configured_path_lock_does_not_split_when_root_is_replaced(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            detached = Path(directory) / "store-detached"
            first = ArtifactStore(root)
            acquired = threading.Event()
            release = threading.Event()
            errors: list[BaseException] = []

            def hold_path_authority() -> None:
                try:
                    with root_authority._configured_path_coordination(first):
                        acquired.set()
                        if not release.wait(timeout=10):
                            raise AssertionError("test path lock holder timed out")
                except BaseException as error:
                    errors.append(error)
                    acquired.set()

            worker = threading.Thread(target=hold_path_authority)
            worker.start()
            self.assertTrue(acquired.wait(timeout=10))
            self.assertEqual(errors, [])

            os.replace(root, detached)
            second = ArtifactStore(root)
            try:
                with self.assertRaisesRegex(
                    ResourceLockBusyError,
                    "path authority is busy",
                ):
                    self._publish(second, b"r2-must-not-overlap-r1-authority")
                self.assertEqual(list(second.manifests.glob("*.json")), [])
            finally:
                release.set()
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                del first
                del second
                gc.collect()

    @unittest.skipIf(sys.platform == "win32", "POSIX retained publisher required")
    def test_root_swap_after_posix_manifest_commit_cannot_return_success(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            detached = Path(directory) / "store-detached"
            store = ArtifactStore(root)
            real_publish_manifest = posix_publication._publish_manifest_posix
            swapped = False

            def commit_then_swap(self, *, manifest, replace_existing):
                nonlocal swapped
                result = real_publish_manifest(
                    self,
                    manifest=manifest,
                    replace_existing=replace_existing,
                )
                if not swapped:
                    swapped = True
                    os.replace(root, detached)
                    root.mkdir()
                return result

            try:
                with patch.object(
                    posix_publication,
                    "_publish_manifest_posix",
                    side_effect=commit_then_swap,
                ):
                    with self.assertRaisesRegex(
                        ArtifactIntegrityError,
                        "root changed after initialization",
                    ):
                        self._publish(store, b"posix-root-swap-after-commit")

                self.assertTrue(swapped)
                self.assertEqual(list(root.glob("manifests/*.json")), [])
                self.assertEqual(len(list(detached.glob("manifests/*.json"))), 1)
            finally:
                del store
                gc.collect()

    @unittest.skipUnless(sys.platform == "win32", "real Windows HANDLE semantics required")
    def test_root_swap_after_windows_manifest_commit_cannot_return_success(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            detached = Path(directory) / "store-detached"
            store = ArtifactStore(root)
            real_publish_manifest = windows_publication._publish_manifest_windows
            swapped = False

            def commit_then_swap(self, *, manifest, replace_existing):
                nonlocal swapped
                result = real_publish_manifest(
                    self,
                    manifest=manifest,
                    replace_existing=replace_existing,
                )
                if not swapped:
                    swapped = True
                    os.replace(root, detached)
                    root.mkdir()
                return result

            try:
                with patch.object(
                    windows_publication,
                    "_publish_manifest_windows",
                    side_effect=commit_then_swap,
                ):
                    with self.assertRaisesRegex(
                        ArtifactIntegrityError,
                        "root changed after initialization",
                    ):
                        self._publish(store, b"windows-root-swap-after-commit")

                self.assertTrue(swapped)
                self.assertEqual(list(root.glob("manifests/*.json")), [])
                self.assertEqual(len(list(detached.glob("manifests/*.json"))), 1)
            finally:
                del store
                gc.collect()


if __name__ == "__main__":
    unittest.main()
