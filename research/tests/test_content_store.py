from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_research.artifacts.content_store import (
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactStore,
)


class ArtifactStoreTests(unittest.TestCase):
    def test_put_is_content_addressed_and_idempotent(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            first = store.put_bytes(b"evidence", media_type="application/octet-stream", rights_basis="first-party")
            second = store.put_bytes(b"evidence", media_type="application/octet-stream", rights_basis="first-party")
            self.assertEqual(first, second)
            self.assertEqual(store.verify(first.digest), first)

    def test_conflicting_immutable_rights_metadata_is_rejected(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            first = store.put_bytes(b"same", media_type="text/plain", rights_basis="basis-a")
            with self.assertRaises(ArtifactConflictError):
                store.put_bytes(b"same", media_type="text/plain", rights_basis="basis-b")
            self.assertEqual(store.verify(first.digest).rights_basis, "basis-a")

    def test_tamper_is_detected_and_export_is_refused(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            manifest = store.put_bytes(b"trusted", media_type="text/plain", rights_basis="owned")
            store._object_path(manifest.digest).write_bytes(b"tampered")
            with self.assertRaises(ArtifactIntegrityError):
                store.verify(manifest.digest)
            with self.assertRaises(ArtifactIntegrityError):
                store.export(manifest.digest, Path(directory) / "out.bin")

    def test_export_preserves_exact_digest(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            manifest = store.put_bytes(b"export-me", media_type="text/plain", rights_basis="owned")
            target = store.export(manifest.digest, Path(directory) / "export" / "artifact.bin")
            self.assertEqual(target.read_bytes(), b"export-me")
            self.assertEqual(store.verify(manifest.digest), manifest)

    def test_recovery_removes_temps_but_never_deletes_referenced_or_orphan_objects(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            manifest = store.put_bytes(b"kept", media_type="text/plain", rights_basis="owned")
            referenced = store._object_path(manifest.digest)
            temp_path = referenced.parent / ".interrupted.tmp"
            temp_path.write_bytes(b"partial")
            orphan = store.objects_root / "aa" / ("a" * 64)
            orphan.parent.mkdir(parents=True, exist_ok=True)
            orphan.write_bytes(b"orphan")
            report = store.recover_orphans()
            self.assertFalse(temp_path.exists())
            self.assertTrue(referenced.exists())
            self.assertTrue(orphan.exists())
            self.assertIn("a" * 64, report["orphan_objects"])
            self.assertTrue(report["temp_removed"])

    def test_rights_basis_is_mandatory(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            with self.assertRaises(ValueError):
                store.put_bytes(b"x", media_type="text/plain", rights_basis="")


if __name__ == "__main__":
    unittest.main()
