from hashlib import sha256
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from autotrade_research.artifacts.store import ArtifactIntegrityError, ArtifactStore
from autotrade_research.artifacts import _windows_retained_publication as windows_publication


@unittest.skipUnless(
    sys.platform == "win32",
    "retained Windows publication regressions require real Windows HANDLE semantics",
)
class WindowsRetainedPublicationNamespaceTests(unittest.TestCase):
    def _publish(self, store: ArtifactStore, payload: bytes):
        return store.publish_bytes(
            artifact_id=str(uuid4()),
            data=payload,
            media_type="application/octet-stream",
            rights={"storage": True, "export": False},
        )

    def _exercise_prefix_swap(self, *, replacement_has_same_bytes: bool) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            payload = (
                b"windows-prefix-same-bytes"
                if replacement_has_same_bytes
                else b"windows-prefix-empty-replacement"
            )
            digest = sha256(payload).hexdigest()
            prefix = store.objects / digest[:2]
            prefix.mkdir()
            detached = store.objects / f"detached-{digest[:2]}"
            replacement = store.objects / f"replacement-{digest[:2]}"
            replacement.mkdir()
            if replacement_has_same_bytes:
                (replacement / digest).write_bytes(payload)

            real_publish_temp = windows_publication._publish_temp_fd
            swapped = False

            def swap_prefix_then_publish(
                descriptor,
                *,
                target_parent,
                target_name,
                replace,
            ):
                nonlocal swapped
                if not swapped and target_name == digest and not replace:
                    swapped = True
                    os.replace(prefix, detached)
                    os.replace(replacement, prefix)
                return real_publish_temp(
                    descriptor,
                    target_parent=target_parent,
                    target_name=target_name,
                    replace=replace,
                )

            try:
                with patch.object(
                    windows_publication,
                    "_publish_temp_fd",
                    side_effect=swap_prefix_then_publish,
                ):
                    with self.assertRaisesRegex(
                        ArtifactIntegrityError,
                        "object prefix changed during publication",
                    ):
                        self._publish(store, payload)

                self.assertTrue(swapped)
                self.assertEqual(list(store.manifests.glob("*.json")), [])
                self.assertTrue((detached / digest).exists())
                if replacement_has_same_bytes:
                    self.assertEqual((prefix / digest).read_bytes(), payload)
                else:
                    self.assertEqual(list(prefix.iterdir()), [])
            finally:
                for entry in list(prefix.iterdir()):
                    entry.unlink()
                prefix.rmdir()
                os.replace(detached, prefix)

    def test_exact_digest_prefix_swap_cannot_yield_successful_manifest(self):
        self._exercise_prefix_swap(replacement_has_same_bytes=False)

    def test_same_bytes_replacement_prefix_is_not_promoted_to_authority(self):
        self._exercise_prefix_swap(replacement_has_same_bytes=True)

    def test_ordinary_manifest_child_replacement_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            detached = root / "canonical-manifests"
            replacement = root / "replacement-manifests"
            replacement.mkdir()
            os.replace(store.manifests, detached)
            os.replace(replacement, store.manifests)
            try:
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "manifests namespace changed",
                ):
                    self._publish(store, b"windows-manifest-child")
                self.assertEqual(list(store.manifests.iterdir()), [])
            finally:
                store.manifests.rmdir()
                os.replace(detached, store.manifests)


if __name__ == "__main__":
    unittest.main()
