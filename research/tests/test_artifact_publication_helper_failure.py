import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from autotrade_research.artifacts.store import ArtifactIntegrityError, ArtifactStore
from autotrade_research.artifacts import _retained_publication_hardening as posix_publication
from autotrade_research.artifacts import _windows_retained_publication_hardening as windows_publication


class ManifestHelperFailureTransactionTests(unittest.TestCase):
    def _assert_retry_clean(self, store: ArtifactStore, artifact_id: str, payload: bytes):
        reopened = ArtifactStore(store.root)
        with self.assertRaises(FileNotFoundError):
            reopened.load_manifest(artifact_id)
        manifest = reopened.publish_bytes(
            artifact_id=artifact_id,
            data=payload,
            media_type="application/octet-stream",
            rights={"storage": True, "export": False},
        )
        self.assertEqual(manifest["artifact_id"], artifact_id)
        self.assertTrue(reopened._manifest_path(artifact_id).exists())

    @unittest.skipIf(os.name == "nt", "POSIX retained manifest helper required")
    def test_posix_helper_failure_after_rename_rolls_back_exact_manifest(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            payload = b"posix-helper-post-commit-failure"
            real_publish = posix_publication._publish_manifest_posix

            def commit_then_fail(*args, **kwargs):
                real_publish(*args, **kwargs)
                raise OSError("injected post-commit helper failure")

            with patch.object(
                posix_publication,
                "_publish_manifest_posix",
                side_effect=commit_then_fail,
            ):
                with self.assertRaises(OSError):
                    store.publish_bytes(
                        artifact_id=artifact_id,
                        data=payload,
                        media_type="application/octet-stream",
                        rights={"storage": True, "export": False},
                    )

            self.assertFalse(store._manifest_path(artifact_id).exists())
            self._assert_retry_clean(store, artifact_id, payload)

    @unittest.skipUnless(os.name == "nt", "real Windows retained manifest helper required")
    def test_windows_helper_failure_after_rename_rolls_back_exact_manifest(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            payload = b"windows-helper-post-commit-failure"
            real_publish = windows_publication._publish_manifest_windows

            def commit_then_fail(*args, **kwargs):
                real_publish(*args, **kwargs)
                raise OSError("injected post-commit helper failure")

            with patch.object(
                windows_publication,
                "_publish_manifest_windows",
                side_effect=commit_then_fail,
            ):
                with self.assertRaises(OSError):
                    store.publish_bytes(
                        artifact_id=artifact_id,
                        data=payload,
                        media_type="application/octet-stream",
                        rights={"storage": True, "export": False},
                    )

            self.assertFalse(store._manifest_path(artifact_id).exists())
            self._assert_retry_clean(store, artifact_id, payload)


if __name__ == "__main__":
    unittest.main()
