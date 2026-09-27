from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from autotrade_research.artifacts.store import ArtifactIntegrityError, ArtifactStore
from autotrade_research.artifacts import _retained_publication_hardening as publication


@unittest.skipIf(os.name == "nt", "POSIX retained-prefix transaction regression")
class PublicationTransactionTests(unittest.TestCase):
    def test_post_last_check_prefix_swap_leaves_prepared_barrier_until_generation_restored(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            artifact_id = str(uuid4())
            payload = b"transaction-prefix-generation"
            digest = sha256(payload).hexdigest()
            prefix = store.objects / digest[:2]
            prefix.mkdir()
            detached = store.objects / f"detached-{digest[:2]}"
            replacement = Path(directory) / "replacement-prefix"
            replacement.mkdir()
            real_publish_manifest = publication._publish_manifest_posix
            swapped = False

            def swap_after_last_prefix_check(*args, **kwargs):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    os.replace(prefix, detached)
                    os.replace(replacement, prefix)
                return real_publish_manifest(*args, **kwargs)

            try:
                with patch.object(
                    publication,
                    "_publish_manifest_posix",
                    side_effect=swap_after_last_prefix_check,
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

                self.assertTrue(swapped)
                manifest_path = store._manifest_path(artifact_id)
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertEqual(raw["schema_version"], 2)
                self.assertEqual(raw["publication_state"], "PREPARED")
                prepared_bytes = manifest_path.read_bytes()
                self.assertTrue((detached / digest).exists())
                self.assertEqual(list(prefix.iterdir()), [])

                reopened = ArtifactStore(root)
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "publication is not committed",
                ):
                    reopened.load_manifest(artifact_id)
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "object generation changed after publication",
                ):
                    reopened.publish_bytes(
                        artifact_id=artifact_id,
                        data=payload,
                        media_type="application/octet-stream",
                        rights={"storage": True, "export": False},
                    )
                self.assertEqual(manifest_path.read_bytes(), prepared_bytes)

                prefix.rmdir()
                os.replace(detached, prefix)
                manifest = reopened.publish_bytes(
                    artifact_id=artifact_id,
                    data=payload,
                    media_type="application/octet-stream",
                    rights={"storage": True, "export": False},
                )
                self.assertEqual(manifest["artifact_id"], artifact_id)
                self.assertEqual(manifest["sha256"], f"sha256:{digest}")
                self.assertEqual(manifest["publication_state"], "COMMITTED")
                self.assertEqual(reopened.read_bytes(artifact_id), payload)
            finally:
                if detached.exists() and not prefix.exists():
                    os.replace(detached, prefix)


if __name__ == "__main__":
    unittest.main()
