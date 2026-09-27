import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_research.artifacts.store import ArtifactStore
from autotrade_research.artifacts import _retained_recovery_hardening as recovery


@unittest.skipUnless(
    os.name != "nt",
    "retained destructive recovery regressions require POSIX dir-fd semantics",
)
class RetainedRecoveryDetachmentTests(unittest.TestCase):
    def test_sha256_child_swap_at_destructive_unlink_stays_bound_to_retained_generation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            self.assertTrue(store._supports_descriptor_relative_cleanup())

            data = b"retained-sha256-orphan"
            digest = hashlib.sha256(data).hexdigest()
            orphan = store._object_path(digest)
            orphan.parent.mkdir(parents=True, exist_ok=True)
            orphan.write_bytes(data)

            detached = root / "objects" / "detached-sha256"
            outside = Path(directory) / "outside-sha256"
            outside_prefix = outside / digest[:2]
            outside_prefix.mkdir(parents=True)
            external = outside_prefix / digest
            external.write_bytes(b"external-must-survive")

            real_unlink = recovery.os.unlink
            swapped = False

            def swap_then_unlink(name, *, dir_fd=None):
                nonlocal swapped
                if not swapped and name == digest and dir_fd is not None:
                    swapped = True
                    os.replace(store.objects, detached)
                    store.objects.symlink_to(outside, target_is_directory=True)
                return real_unlink(name, dir_fd=dir_fd)

            with patch.object(recovery.os, "unlink", side_effect=swap_then_unlink):
                after = store.recover_orphans()

            self.assertTrue(swapped)
            self.assertEqual(external.read_bytes(), b"external-must-survive")
            self.assertFalse((detached / digest[:2] / digest).exists())
            self.assertNotIn(digest, after.unreferenced_objects)

    def test_staging_child_swap_at_destructive_unlink_returns_retained_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            self.assertTrue(store._supports_descriptor_relative_cleanup())
            staged = store.staging / "left.tmp"
            staged.write_bytes(b"retained-staging")

            detached = root / "detached-staging"
            outside = Path(directory) / "outside-staging"
            outside.mkdir()
            external = outside / staged.name
            external.write_bytes(b"external-must-survive")

            real_unlink = recovery.os.unlink
            swapped = False

            def swap_then_unlink(name, *, dir_fd=None):
                nonlocal swapped
                if not swapped and name == staged.name and dir_fd is not None:
                    swapped = True
                    os.replace(store.staging, detached)
                    store.staging.symlink_to(outside, target_is_directory=True)
                return real_unlink(name, dir_fd=dir_fd)

            with patch.object(recovery.os, "unlink", side_effect=swap_then_unlink):
                after = store.recover_orphans()

            self.assertTrue(swapped)
            self.assertEqual(external.read_bytes(), b"external-must-survive")
            self.assertFalse((detached / staged.name).exists())
            self.assertEqual(after.unreferenced_objects, ())


if __name__ == "__main__":
    unittest.main()
