import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from autotrade_research.artifacts.store import ArtifactStore


@unittest.skipUnless(
    sys.platform == "win32",
    "retained relative rename qualification requires real Windows HANDLE semantics",
)
class WindowsRetainedRelativeRenameRealTests(unittest.TestCase):
    def test_real_windows_publish_uses_repaired_retained_relative_rename_end_to_end(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            self.assertTrue(store._windows_retained_rename_fix)
            self.assertTrue(store._windows_retained_publication_hardening)

            artifact_id = str(uuid4())
            payload = b"real-windows-retained-relative-rename"
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=payload,
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )

            digest = manifest["sha256"].removeprefix("sha256:")
            object_path = store._object_path(digest)
            manifest_path = store._manifest_path(artifact_id)
            self.assertTrue(object_path.is_file())
            self.assertTrue(manifest_path.is_file())
            self.assertEqual(os.stat(object_path, follow_symlinks=False).st_nlink, 1)
            self.assertEqual(os.stat(manifest_path, follow_symlinks=False).st_nlink, 1)
            self.assertEqual(store.read_bytes(artifact_id), payload)

            reopened = ArtifactStore(root)
            self.assertEqual(reopened.read_bytes(artifact_id), payload)
            repeated = reopened.publish_bytes(
                artifact_id=artifact_id,
                data=payload,
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )
            self.assertEqual(repeated, manifest)


if __name__ == "__main__":
    unittest.main()
