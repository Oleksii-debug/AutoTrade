import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from autotrade_research.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactStore,
)


@unittest.skipIf(
    os.name == "nt",
    "adversarial directory-symlink swaps require POSIX symlink semantics",
)
class ArtifactNamespaceGuardTests(unittest.TestCase):
    def test_guard_is_installed_for_direct_store_import(self):
        self.assertTrue(ArtifactStore._root_anchored_namespace_guard)

    def test_manifest_intermediate_swap_cannot_escape_held_store_root(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            artifact_id = str(uuid4())
            store.publish_bytes(
                artifact_id=artifact_id,
                data=b"canonical-manifest-boundary",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
                metadata={"origin": "canonical"},
            )
            manifest_path = store._manifest_path(artifact_id)
            canonical_manifest = manifest_path.read_bytes()

            outside = Path(directory) / "outside-manifests"
            outside.mkdir()
            external = outside / manifest_path.name
            external.write_bytes(canonical_manifest)
            detached = root / "detached-manifests"
            original_validate = store._validate_manifest_entry
            swapped = False

            def validate_then_swap(path):
                nonlocal swapped
                entry = original_validate(path)
                if not swapped:
                    swapped = True
                    os.replace(store.manifests, detached)
                    store.manifests.symlink_to(outside, target_is_directory=True)
                return entry

            try:
                with patch.object(
                    store,
                    "_validate_manifest_entry",
                    side_effect=validate_then_swap,
                ):
                    with self.assertRaisesRegex(
                        ArtifactIntegrityError,
                        "opened safely|namespace|changed",
                    ):
                        store.load_manifest(artifact_id)
            finally:
                if store.manifests.is_symlink():
                    store.manifests.unlink()
                if detached.exists():
                    os.replace(detached, store.manifests)

            self.assertTrue(swapped)
            self.assertEqual(external.read_bytes(), canonical_manifest)
            self.assertEqual(
                store.load_manifest(artifact_id)["metadata"],
                {"origin": "canonical"},
            )

    def test_object_intermediate_swap_cannot_open_outside_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            artifact_id = str(uuid4())
            data = b"canonical-object-boundary"
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=data,
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )
            digest = manifest["sha256"].removeprefix("sha256:")
            canonical = store._object_path(digest)

            outside_sha256 = Path(directory) / "outside-objects" / "sha256"
            external = outside_sha256 / digest[:2] / digest
            external.parent.mkdir(parents=True)
            external.write_bytes(b"outside-object-is-not-authority")
            detached = root / "objects" / "detached-sha256"
            original_validate = store._validate_object_entry
            swapped = False

            def validate_then_swap(path):
                nonlocal swapped
                entry = original_validate(path)
                if not swapped:
                    swapped = True
                    os.replace(store.objects, detached)
                    store.objects.symlink_to(
                        outside_sha256,
                        target_is_directory=True,
                    )
                return entry

            descriptor = None
            try:
                with patch.object(
                    store,
                    "_validate_object_entry",
                    side_effect=validate_then_swap,
                ):
                    with self.assertRaisesRegex(
                        ArtifactIntegrityError,
                        "opened safely|namespace|changed",
                    ):
                        descriptor, _opened = store._open_object_descriptor(
                            canonical,
                            expected_bytes=len(data),
                        )
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                if store.objects.is_symlink():
                    store.objects.unlink()
                if detached.exists():
                    os.replace(detached, store.objects)

            self.assertTrue(swapped)
            self.assertEqual(
                external.read_bytes(),
                b"outside-object-is-not-authority",
            )
            self.assertEqual(store.read_bytes(artifact_id), data)

    def test_recovery_intermediate_sha256_swap_never_deletes_outside_object(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            self.assertTrue(store._supports_descriptor_relative_cleanup())

            data = b"canonical-recoverable-orphan"
            digest = hashlib.sha256(data).hexdigest()
            orphan = store._object_path(digest)
            orphan.parent.mkdir(parents=True, exist_ok=True)
            orphan.write_bytes(data)

            outside_sha256 = Path(directory) / "outside-recovery" / "sha256"
            external = outside_sha256 / digest[:2] / digest
            external.parent.mkdir(parents=True)
            external.write_bytes(b"outside-must-survive")
            detached = root / "objects" / "detached-sha256"
            original_unlink = store._unlink_verified_regular_entry
            swapped = False

            def swap_intermediate_then_unlink(
                cleanup_directory,
                name,
                *,
                subject,
            ):
                nonlocal swapped
                if not swapped and name == digest:
                    swapped = True
                    os.replace(store.objects, detached)
                    store.objects.symlink_to(
                        outside_sha256,
                        target_is_directory=True,
                    )
                return original_unlink(
                    cleanup_directory,
                    name,
                    subject=subject,
                )

            try:
                with patch.object(
                    store,
                    "_unlink_verified_regular_entry",
                    side_effect=swap_intermediate_then_unlink,
                ):
                    store.recover_orphans()
            finally:
                if store.objects.is_symlink():
                    store.objects.unlink()
                if detached.exists():
                    os.replace(detached, store.objects)

            self.assertTrue(swapped)
            self.assertEqual(external.read_bytes(), b"outside-must-survive")
            self.assertTrue(orphan.exists())
            self.assertIn(digest, store.audit().unreferenced_objects)


if __name__ == "__main__":
    unittest.main()
