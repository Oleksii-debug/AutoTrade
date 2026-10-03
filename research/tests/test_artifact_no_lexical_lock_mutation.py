import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from autotrade_research.artifacts import store as store_module
from autotrade_research.artifacts.store import ArtifactIntegrityError, ArtifactStore


class NoLexicalCompatibilityLockMutationTests(unittest.TestCase):
    def test_publish_and_recovery_do_not_create_lexical_lock_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            self.assertFalse(store.lock_path.exists())

            artifact_id = str(uuid4())
            store.publish_bytes(
                artifact_id=artifact_id,
                data=b"retained-coordination-only",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )
            self.assertFalse(store.lock_path.exists())

            store.recover_orphans()
            self.assertFalse(store.lock_path.exists())
            self.assertEqual(store.read_bytes(artifact_id), b"retained-coordination-only")

    def test_publish_and_recovery_never_enter_legacy_resource_lock(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            artifact_id = str(uuid4())

            with patch.object(
                store_module,
                "ResourceLock",
                side_effect=AssertionError("legacy lexical ResourceLock was entered"),
            ):
                store.publish_bytes(
                    artifact_id=artifact_id,
                    data=b"retained-lock-authority",
                    media_type="application/octet-stream",
                    rights={"storage": True, "export": False},
                )
                store.recover_orphans()

            self.assertFalse(store.lock_path.exists())
            self.assertEqual(store.read_bytes(artifact_id), b"retained-lock-authority")

    @unittest.skipIf(
        sys.platform == "win32",
        "retained Windows root HANDLE prevents lexical root replacement",
    )
    def test_replacement_root_never_receives_legacy_lock_metadata(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            detached = Path(directory) / "store-detached"
            store = ArtifactStore(root)
            os.replace(root, detached)
            root.mkdir()

            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "root changed after initialization",
            ):
                store.publish_bytes(
                    artifact_id=str(uuid4()),
                    data=b"must-not-touch-replacement-root",
                    media_type="application/octet-stream",
                    rights={"storage": True, "export": False},
                )
            self.assertFalse((root / ".artifact-store.lock").exists())

            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "root changed after initialization",
            ):
                store.recover_orphans()
            self.assertFalse((root / ".artifact-store.lock").exists())


if __name__ == "__main__":
    unittest.main()
