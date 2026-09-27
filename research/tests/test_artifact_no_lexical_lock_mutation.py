from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from autotrade_research.artifacts.store import ArtifactStore


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


if __name__ == "__main__":
    unittest.main()
