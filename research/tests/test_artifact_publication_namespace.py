from hashlib import sha256
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
from autotrade_research.artifacts import _retained_publication as publication
from autotrade_research.artifacts import (
    _retained_publication_hardening as publication_hardening,
)


@unittest.skipIf(
    os.name == "nt",
    "descriptor-relative publication regressions require POSIX dir-fd semantics",
)
class RetainedPublicationNamespaceTests(unittest.TestCase):
    def _publish(self, store: ArtifactStore, *, payload: bytes = b"bound"):
        return store.publish_bytes(
            artifact_id=str(uuid4()),
            data=payload,
            media_type="application/octet-stream",
            rights={"storage": True, "export": False},
        )

    def test_manifest_child_replacement_never_becomes_publication_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            detached = root / "canonical-manifests"
            os.replace(store.manifests, detached)
            store.manifests.mkdir()
            try:
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "manifests namespace changed",
                ):
                    self._publish(store)
                self.assertEqual(list(store.manifests.iterdir()), [])
            finally:
                store.manifests.rmdir()
                os.replace(detached, store.manifests)

    def test_objects_child_replacement_never_becomes_publication_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            detached = root / "objects" / "canonical-sha256"
            os.replace(store.objects, detached)
            store.objects.mkdir()
            try:
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "objects namespace changed",
                ):
                    self._publish(store)
                self.assertEqual(list(store.objects.iterdir()), [])
            finally:
                store.objects.rmdir()
                os.replace(detached, store.objects)

    def test_staging_child_replacement_never_becomes_publication_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            detached = root / "canonical-staging"
            os.replace(store.staging, detached)
            store.staging.mkdir()
            try:
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "staging namespace changed",
                ):
                    self._publish(store)
                self.assertEqual(list(store.staging.iterdir()), [])
            finally:
                store.staging.rmdir()
                os.replace(detached, store.staging)

    def test_manifest_swap_after_last_check_cannot_redirect_final_commit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            detached = root / "canonical-manifests"
            replacement = root / "replacement-manifests"
            replacement.mkdir()
            real_rename = publication_hardening._rename_noreplace_posix
            swapped = False

            def swap_then_rename(parent_fd, source, target):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    os.replace(store.manifests, detached)
                    os.replace(replacement, store.manifests)
                return real_rename(parent_fd, source, target)

            try:
                with patch.object(
                    publication_hardening,
                    "_rename_noreplace_posix",
                    side_effect=swap_then_rename,
                ):
                    with self.assertRaisesRegex(
                        ArtifactIntegrityError,
                        "manifests namespace changed",
                    ):
                        self._publish(store, payload=b"manifest-race")
                self.assertTrue(swapped)
                self.assertEqual(list(store.manifests.iterdir()), [])
                self.assertEqual(len(list(detached.glob("*.json"))), 1)
            finally:
                for entry in store.manifests.iterdir():
                    entry.unlink()
                store.manifests.rmdir()
                os.replace(detached, store.manifests)

    def test_new_manifest_commit_is_single_link_and_leaves_no_internal_alias(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            manifest = self._publish(store, payload=b"single-link-manifest")
            path = store._manifest_path(manifest["artifact_id"])
            self.assertEqual(os.stat(path, follow_symlinks=False).st_nlink, 1)
            self.assertEqual(
                [entry.name for entry in store.manifests.iterdir() if entry.name.startswith(".manifest-")],
                [],
            )

    def test_object_swap_after_last_check_cannot_redirect_object_commit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            detached = root / "objects" / "canonical-sha256"
            replacement = root / "objects" / "replacement-sha256"
            replacement.mkdir()
            real_link = os.link
            swapped = False

            def swap_then_link(
                src,
                dst,
                *,
                src_dir_fd=None,
                dst_dir_fd=None,
                follow_symlinks=True,
            ):
                nonlocal swapped
                if (
                    not swapped
                    and src_dir_fd == store._retained_staging_fd
                    and dst_dir_fd not in {None, store._retained_manifests_fd}
                ):
                    swapped = True
                    os.replace(store.objects, detached)
                    os.replace(replacement, store.objects)
                return real_link(
                    src,
                    dst,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                    follow_symlinks=follow_symlinks,
                )

            try:
                with patch.object(publication.os, "link", side_effect=swap_then_link):
                    with self.assertRaisesRegex(
                        ArtifactIntegrityError,
                        "objects namespace changed",
                    ):
                        self._publish(store, payload=b"object-race")
                self.assertTrue(swapped)
                self.assertEqual(list(store.objects.iterdir()), [])
                self.assertEqual(len(list(detached.glob("*/*"))), 1)
            finally:
                store.objects.rmdir()
                os.replace(detached, store.objects)

    def test_exact_digest_prefix_swap_cannot_yield_successful_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            payload = b"prefix-race"
            digest = sha256(payload).hexdigest()
            prefix = store.objects / digest[:2]
            prefix.mkdir()
            detached = store.objects / f"detached-{digest[:2]}"
            replacement = Path(directory) / "replacement-prefix"
            replacement.mkdir()
            real_link = os.link
            swapped = False

            def swap_prefix_then_link(
                src,
                dst,
                *,
                src_dir_fd=None,
                dst_dir_fd=None,
                follow_symlinks=True,
            ):
                nonlocal swapped
                if (
                    not swapped
                    and src_dir_fd == store._retained_staging_fd
                    and dst == digest
                ):
                    swapped = True
                    os.replace(prefix, detached)
                    os.replace(replacement, prefix)
                return real_link(
                    src,
                    dst,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                    follow_symlinks=follow_symlinks,
                )

            try:
                with patch.object(
                    publication.os,
                    "link",
                    side_effect=swap_prefix_then_link,
                ):
                    with self.assertRaisesRegex(
                        ArtifactIntegrityError,
                        "object prefix changed during publication",
                    ):
                        self._publish(store, payload=payload)
                self.assertTrue(swapped)
                self.assertEqual(list(prefix.iterdir()), [])
                self.assertTrue((detached / digest).exists())
                self.assertEqual(list(store.manifests.glob("*.json")), [])
            finally:
                for entry in prefix.iterdir():
                    entry.unlink()
                prefix.rmdir()
                os.replace(detached, prefix)

    def test_exact_prefix_replacement_with_same_bytes_is_not_new_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            payload = b"prefix-same-bytes"
            digest = sha256(payload).hexdigest()
            prefix = store.objects / digest[:2]
            prefix.mkdir()
            detached = store.objects / f"detached-{digest[:2]}"
            replacement = Path(directory) / "replacement-prefix"
            replacement.mkdir()
            (replacement / digest).write_bytes(payload)
            real_link = os.link
            swapped = False

            def swap_prefix_then_link(
                src,
                dst,
                *,
                src_dir_fd=None,
                dst_dir_fd=None,
                follow_symlinks=True,
            ):
                nonlocal swapped
                if (
                    not swapped
                    and src_dir_fd == store._retained_staging_fd
                    and dst == digest
                ):
                    swapped = True
                    os.replace(prefix, detached)
                    os.replace(replacement, prefix)
                return real_link(
                    src,
                    dst,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                    follow_symlinks=follow_symlinks,
                )

            try:
                with patch.object(
                    publication.os,
                    "link",
                    side_effect=swap_prefix_then_link,
                ):
                    with self.assertRaisesRegex(
                        ArtifactIntegrityError,
                        "object prefix changed during publication",
                    ):
                        self._publish(store, payload=payload)
                self.assertTrue(swapped)
                self.assertEqual((prefix / digest).read_bytes(), payload)
                self.assertTrue((detached / digest).exists())
                self.assertEqual(list(store.manifests.glob("*.json")), [])
            finally:
                for entry in prefix.iterdir():
                    entry.unlink()
                prefix.rmdir()
                os.replace(detached, prefix)

    def test_staging_swap_during_temp_creation_cannot_redirect_staged_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            detached = root / "canonical-staging"
            replacement = root / "replacement-staging"
            replacement.mkdir()
            real_open = os.open
            swapped = False

            def swap_then_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if (
                    not swapped
                    and dir_fd == store._retained_staging_fd
                    and flags & os.O_CREAT
                ):
                    swapped = True
                    os.replace(store.staging, detached)
                    os.replace(replacement, store.staging)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            try:
                with patch.object(publication.os, "open", side_effect=swap_then_open):
                    with self.assertRaisesRegex(
                        ArtifactIntegrityError,
                        "staging namespace changed",
                    ):
                        self._publish(store, payload=b"staging-race")
                self.assertTrue(swapped)
                self.assertEqual(list(store.staging.iterdir()), [])
            finally:
                for entry in store.staging.iterdir():
                    entry.unlink()
                store.staging.rmdir()
                os.replace(detached, store.staging)


if __name__ == "__main__":
    unittest.main()
