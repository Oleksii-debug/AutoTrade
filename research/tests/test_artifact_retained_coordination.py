from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from uuid import uuid4

from autotrade_research.artifacts.store import ArtifactStore
from autotrade_research.artifacts.resource_lock import ResourceLockBusyError
from autotrade_research.artifacts import _retained_coordination as coordination


class RetainedArtifactStoreCoordinationTests(unittest.TestCase):
    def test_lock_path_replacement_cannot_create_second_publish_or_recovery_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            first = ArtifactStore(root)
            second = ArtifactStore(root)
            first.lock_path.write_bytes(b"\0")

            acquired = threading.Event()
            release = threading.Event()
            worker_errors: list[BaseException] = []

            def hold_canonical_root_lock() -> None:
                try:
                    with coordination.artifact_store_coordination(first):
                        acquired.set()
                        if not release.wait(timeout=10):
                            raise AssertionError(
                                "test did not release retained root coordination lock"
                            )
                except BaseException as error:
                    worker_errors.append(error)
                    acquired.set()

            worker = threading.Thread(target=hold_canonical_root_lock)
            worker.start()
            self.assertTrue(acquired.wait(timeout=10))
            self.assertEqual(worker_errors, [])

            detached_lock = root / ".artifact-store.lock.detached"
            first.lock_path.replace(detached_lock)
            first.lock_path.write_bytes(b"\0")
            artifact_id = str(uuid4())
            try:
                with self.assertRaisesRegex(
                    ResourceLockBusyError,
                    "coordination lock is busy",
                ):
                    second.publish_bytes(
                        artifact_id=artifact_id,
                        data=b"split-lock-must-not-publish",
                        media_type="application/octet-stream",
                        rights={"storage": True, "export": False},
                    )
                with self.assertRaisesRegex(
                    ResourceLockBusyError,
                    "coordination lock is busy",
                ):
                    second.recover_orphans()
                self.assertFalse(second._manifest_path(artifact_id).exists())
            finally:
                release.set()
                worker.join(timeout=10)

            self.assertFalse(worker.is_alive())
            self.assertEqual(worker_errors, [])

            # Once the retained-root owner releases, the replacement legacy lock
            # file cannot split authority: the next mutation is again serialized
            # by the same retained root identity and may safely proceed.
            manifest = second.publish_bytes(
                artifact_id=artifact_id,
                data=b"split-lock-must-not-publish",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )
            self.assertEqual(manifest["artifact_id"], artifact_id)
            self.assertTrue(second._manifest_path(artifact_id).exists())

            detached_lock.unlink()

    def test_same_store_thread_overlap_fails_closed_before_mutation(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            acquired = threading.Event()
            release = threading.Event()
            worker_errors: list[BaseException] = []

            def hold_lock() -> None:
                try:
                    with coordination.artifact_store_coordination(store):
                        acquired.set()
                        if not release.wait(timeout=10):
                            raise AssertionError("test lock holder timed out")
                except BaseException as error:
                    worker_errors.append(error)
                    acquired.set()

            worker = threading.Thread(target=hold_lock)
            worker.start()
            self.assertTrue(acquired.wait(timeout=10))
            self.assertEqual(worker_errors, [])
            try:
                with self.assertRaisesRegex(
                    ResourceLockBusyError,
                    "coordination lock is busy",
                ):
                    store.recover_orphans()
            finally:
                release.set()
                worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(worker_errors, [])


if __name__ == "__main__":
    unittest.main()
