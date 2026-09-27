import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from autotrade_research.artifacts.store import ArtifactIntegrityError, ArtifactStore
from autotrade_research.artifacts import _windows_retained_publication_hardening as publication


@unittest.skipUnless(os.name == "nt", "Windows retained-HANDLE publication regression")
class WindowsPublicationTransactionTests(unittest.TestCase):
    def test_post_manifest_prefix_rejection_leaves_resumable_prepared_state(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            payload = b"windows-transaction-prefix-generation"
            real_publish_manifest = publication._publish_manifest_windows
            real_assert_prefix = publication._assert_windows_prefix_identity
            manifest_committed = False

            def publish_then_mark(*args, **kwargs):
                nonlocal manifest_committed
                result = real_publish_manifest(*args, **kwargs)
                manifest_committed = True
                return result

            def reject_after_manifest(*args, **kwargs):
                if manifest_committed:
                    raise ArtifactIntegrityError(
                        "artifact object prefix changed during publication"
                    )
                return real_assert_prefix(*args, **kwargs)

            with patch.object(
                publication,
                "_publish_manifest_windows",
                side_effect=publish_then_mark,
            ), patch.object(
                publication,
                "_assert_windows_prefix_identity",
                side_effect=reject_after_manifest,
            ):
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "object prefix changed during publication",
                ):
                    store.publish_bytes(
                        artifact_id=artifact_id,
                        data=payload,
                        media_type="application/octet-stream",
                        rights={"storage": True, "export": False},
                    )

            manifest_path = store._manifest_path(artifact_id)
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(raw["schema_version"], 2)
            self.assertEqual(raw["publication_state"], "PREPARED")

            reopened = ArtifactStore(store.root)
            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "publication is not committed",
            ):
                reopened.load_manifest(artifact_id)
            manifest = reopened.publish_bytes(
                artifact_id=artifact_id,
                data=payload,
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )
            self.assertEqual(manifest["artifact_id"], artifact_id)
            self.assertEqual(manifest["publication_state"], "COMMITTED")
            self.assertEqual(reopened.read_bytes(artifact_id), payload)


if __name__ == "__main__":
    unittest.main()
