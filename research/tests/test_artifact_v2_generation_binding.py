import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from autotrade_research.artifacts import _namespace_guard as guard
from autotrade_research.artifacts.store import ArtifactIntegrityError, ArtifactStore


class ArtifactV2GenerationBindingTests(unittest.TestCase):
    def _published(self, directory, *, export_authorizer=None):
        store = ArtifactStore(
            Path(directory) / "store",
            export_authorizer=export_authorizer,
        )
        artifact_id = str(uuid4())
        payload = b"generation-bound-evidence"
        manifest = store.publish_bytes(
            artifact_id=artifact_id,
            data=payload,
            media_type="application/octet-stream",
            rights={"storage": True, "export": True},
            source_refs=["source:generation-binding"],
            metadata={"kind": "generation-binding-regression"},
        )
        digest = manifest["sha256"].removeprefix("sha256:")
        object_path = store._object_path(digest)
        return store, artifact_id, payload, object_path

    @staticmethod
    def _replace_prefix_with_same_bytes(object_path, payload):
        prefix = object_path.parent
        detached = prefix.with_name(prefix.name + ".detached")
        os.replace(prefix, detached)
        prefix.mkdir()
        (prefix / object_path.name).write_bytes(payload)
        return detached

    def test_read_rejects_same_bytes_prefix_swap_after_generation_open(self):
        with TemporaryDirectory() as directory:
            store, artifact_id, payload, object_path = self._published(directory)
            detached = None
            swapped = False

            if sys.platform == "win32":
                original = guard._nt_open_relative_handle

                def open_then_swap(parent, name, *, directory, subject):
                    nonlocal detached, swapped
                    handle = original(
                        parent,
                        name,
                        directory=directory,
                        subject=subject,
                    )
                    if (
                        not swapped
                        and directory
                        and name == object_path.parent.name
                        and subject in {
                            "artifact object generation",
                            "artifact object prefix",
                        }
                    ):
                        swapped = True
                        detached = self._replace_prefix_with_same_bytes(
                            object_path,
                            payload,
                        )
                    return handle

                patcher = patch.object(
                    guard,
                    "_nt_open_relative_handle",
                    side_effect=open_then_swap,
                )
            else:
                original = guard._open_posix_directory_component

                def open_then_swap(owner, parent_fd, name, *, subject):
                    nonlocal detached, swapped
                    descriptor = original(
                        owner,
                        parent_fd,
                        name,
                        subject=subject,
                    )
                    if (
                        not swapped
                        and name == object_path.parent.name
                        and subject in {
                            "artifact object generation",
                            "artifact object prefix",
                        }
                    ):
                        swapped = True
                        detached = self._replace_prefix_with_same_bytes(
                            object_path,
                            payload,
                        )
                    return descriptor

                patcher = patch.object(
                    guard,
                    "_open_posix_directory_component",
                    side_effect=open_then_swap,
                )

            try:
                with patcher:
                    with self.assertRaisesRegex(
                        ArtifactIntegrityError,
                        "generation|changed",
                    ):
                        store.read_authenticated_snapshot(artifact_id)
                self.assertTrue(swapped)
            finally:
                if detached is not None:
                    replacement = object_path.parent
                    shutil.rmtree(replacement, ignore_errors=True)
                    os.replace(detached, replacement)

    def test_export_rejects_same_bytes_prefix_swap_after_generation_open(self):
        with TemporaryDirectory() as directory:
            store, artifact_id, payload, object_path = self._published(
                directory,
                export_authorizer=lambda *_args: True,
            )
            detached = None
            swapped = False
            target = Path(directory) / "export.bin"

            if sys.platform == "win32":
                original = guard._nt_open_relative_handle

                def open_then_swap(parent, name, *, directory, subject):
                    nonlocal detached, swapped
                    handle = original(
                        parent,
                        name,
                        directory=directory,
                        subject=subject,
                    )
                    if (
                        not swapped
                        and directory
                        and name == object_path.parent.name
                        and subject in {
                            "artifact object generation",
                            "artifact object prefix",
                        }
                    ):
                        swapped = True
                        detached = self._replace_prefix_with_same_bytes(
                            object_path,
                            payload,
                        )
                    return handle

                patcher = patch.object(
                    guard,
                    "_nt_open_relative_handle",
                    side_effect=open_then_swap,
                )
            else:
                original = guard._open_posix_directory_component

                def open_then_swap(owner, parent_fd, name, *, subject):
                    nonlocal detached, swapped
                    descriptor = original(
                        owner,
                        parent_fd,
                        name,
                        subject=subject,
                    )
                    if (
                        not swapped
                        and name == object_path.parent.name
                        and subject in {
                            "artifact object generation",
                            "artifact object prefix",
                        }
                    ):
                        swapped = True
                        detached = self._replace_prefix_with_same_bytes(
                            object_path,
                            payload,
                        )
                    return descriptor

                patcher = patch.object(
                    guard,
                    "_open_posix_directory_component",
                    side_effect=open_then_swap,
                )

            try:
                with patcher:
                    with self.assertRaisesRegex(
                        ArtifactIntegrityError,
                        "generation|changed",
                    ):
                        store.export(artifact_id, target)
                self.assertTrue(swapped)
                self.assertFalse(target.exists())
            finally:
                if detached is not None:
                    replacement = object_path.parent
                    shutil.rmtree(replacement, ignore_errors=True)
                    os.replace(detached, replacement)


if __name__ == "__main__":
    unittest.main()
