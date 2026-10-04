from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
from uuid import uuid4

from autotrade_research.artifacts import ArtifactIntegrityError, ArtifactStore


class ArtifactGenerationBoundReadTests(unittest.TestCase):
    def _store(self, root: Path) -> ArtifactStore:
        return ArtifactStore(
            root,
            export_authorizer=lambda _artifact_id, _digest: True,
        )

    def _publish(self, store: ArtifactStore, data: bytes):
        return store.publish_bytes(
            artifact_id=str(uuid4()),
            data=data,
            media_type="application/octet-stream",
            rights={"storage": True, "export": True},
            source_refs=["generation-bound-read-regression"],
            metadata={"purpose": "generation-bound-read"},
        )

    def _swap_prefix_on_preopen(self, store: ArtifactStore, manifest, data: bytes):
        digest = manifest["sha256"].removeprefix("sha256:")
        prefix = store.objects / digest[:2]
        detached = prefix.with_name(prefix.name + "-detached")
        original_validate = store._validate_object_entry
        swapped = False

        def inject(_selected_store, path):
            nonlocal swapped
            if not swapped:
                prefix.rename(detached)
                prefix.mkdir()
                (prefix / digest).write_bytes(data)
                swapped = True
            return original_validate(path)

        return digest, prefix, detached, inject

    @staticmethod
    def _restore_prefix(prefix: Path, detached: Path) -> None:
        if prefix.exists():
            shutil.rmtree(prefix)
        if detached.exists():
            detached.rename(prefix)

    def test_authenticated_snapshot_rejects_same_bytes_prefix_swap_at_object_open(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory) / "artifacts")
            data = b"same bytes do not authorize a replacement prefix"
            manifest = self._publish(store, data)
            _digest, prefix, detached, inject = self._swap_prefix_on_preopen(
                store, manifest, data
            )
            try:
                with mock.patch.object(
                    ArtifactStore, "_validate_object_entry", autospec=True, side_effect=inject
                ):
                    with self.assertRaisesRegex(
                        ArtifactIntegrityError,
                        "object generation changed",
                    ):
                        store.read_authenticated_snapshot(manifest["artifact_id"])
            finally:
                self._restore_prefix(prefix, detached)

    def test_verify_manifest_object_rejects_same_bytes_prefix_swap_at_object_open(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory) / "artifacts")
            data = b"verification must retain exact prefix generation"
            manifest = self._publish(store, data)
            committed = store.load_manifest(manifest["artifact_id"])
            _digest, prefix, detached, inject = self._swap_prefix_on_preopen(
                store, committed, data
            )
            try:
                with mock.patch.object(
                    ArtifactStore, "_validate_object_entry", autospec=True, side_effect=inject
                ):
                    with self.assertRaisesRegex(
                        ArtifactIntegrityError,
                        "object generation changed",
                    ):
                        store._verify_manifest_object(committed)
            finally:
                self._restore_prefix(prefix, detached)

    def test_export_rejects_same_bytes_prefix_swap_at_object_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._store(root / "artifacts")
            data = b"export must use the committed prefix generation"
            manifest = self._publish(store, data)
            destination = root / "export.bin"
            _digest, prefix, detached, inject = self._swap_prefix_on_preopen(
                store, manifest, data
            )
            try:
                with mock.patch.object(
                    ArtifactStore, "_validate_object_entry", autospec=True, side_effect=inject
                ):
                    with self.assertRaisesRegex(
                        ArtifactIntegrityError,
                        "object generation changed",
                    ):
                        store.export(manifest["artifact_id"], destination)
                self.assertFalse(destination.exists())
            finally:
                self._restore_prefix(prefix, detached)


if __name__ == "__main__":
    unittest.main()
