import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from autotrade_research.artifacts.store import (
    ArtifactConflict,
    ArtifactIntegrityError,
    ArtifactStore,
)


class ArtifactStoreTests(unittest.TestCase):
    def test_publish_read_and_idempotent_republish(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            artifact_id = str(uuid4())
            first = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"evidence",
                media_type="application/octet-stream",
                rights={"storage": True, "export": True},
                source_refs=["source:fixture"],
                metadata={"kind": "test"},
            )
            second = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"evidence",
                media_type="application/octet-stream",
                rights={"storage": True, "export": True},
                source_refs=["source:fixture"],
                metadata={"kind": "test"},
            )
            self.assertEqual(first, second)
            self.assertEqual(store.read_bytes(artifact_id), b"evidence")
            audit = store.audit()
            self.assertEqual(audit.manifests, 1)
            self.assertEqual(audit.objects, 1)
            self.assertEqual(audit.unreferenced_objects, ())

    def test_identity_conflict_and_corruption_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            artifact_id = str(uuid4())
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"one",
                media_type="text/plain",
                rights={"storage": True, "export": False},
            )
            with self.assertRaises(ArtifactConflict):
                store.publish_bytes(
                    artifact_id=artifact_id,
                    data=b"two",
                    media_type="text/plain",
                    rights={"storage": True, "export": False},
                )
            digest = manifest["sha256"].removeprefix("sha256:")
            store._object_path(digest).write_bytes(b"tampered")
            with self.assertRaises(ArtifactIntegrityError):
                store.read_bytes(artifact_id)

    def test_export_is_rights_aware(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            denied = str(uuid4())
            store.publish_bytes(
                artifact_id=denied,
                data=b"private",
                media_type="text/plain",
                rights={"storage": True, "export": False},
            )
            with self.assertRaises(PermissionError):
                store.export(denied, Path(directory) / "out" / "denied.txt")

            allowed = str(uuid4())
            store.publish_bytes(
                artifact_id=allowed,
                data=b"public",
                media_type="text/plain",
                rights={"storage": True, "export": True},
            )
            target = store.export(allowed, Path(directory) / "out" / "allowed.txt")
            self.assertEqual(target.read_bytes(), b"public")

    def test_tampered_manifest_cannot_escalate_export_rights(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"private",
                media_type="text/plain",
                rights={"storage": True, "export": False},
            )
            path = store._manifest_path(artifact_id)
            tampered = json.loads(path.read_text(encoding="utf-8"))
            tampered["rights"]["export"] = True
            path.write_text(json.dumps(tampered), encoding="utf-8")

            with self.assertRaisesRegex(ArtifactIntegrityError, "integrity mismatch"):
                store.export(artifact_id, Path(directory) / "out.txt")
            self.assertEqual(manifest["rights"]["export"], False)

    def test_legacy_manifest_must_be_rebound_before_export(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"public",
                media_type="text/plain",
                rights={"storage": True, "export": True},
                source_refs=["source:fixture"],
                metadata={"kind": "legacy-upgrade"},
            )
            path = store._manifest_path(artifact_id)
            legacy = dict(manifest)
            legacy.pop("manifest_hash")
            path.write_text(json.dumps(legacy), encoding="utf-8")

            with self.assertRaisesRegex(ArtifactIntegrityError, "lacks integrity binding"):
                store.export(artifact_id, Path(directory) / "before.txt")

            upgraded = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"public",
                media_type="text/plain",
                rights={"storage": True, "export": True},
                source_refs=["source:fixture"],
                metadata={"kind": "legacy-upgrade"},
            )
            self.assertIn("manifest_hash", upgraded)
            target = store.export(artifact_id, Path(directory) / "after.txt")
            self.assertEqual(target.read_bytes(), b"public")

    def test_recovery_removes_only_unreferenced_objects(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            artifact_id = str(uuid4())
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"kept",
                media_type="text/plain",
                rights={"storage": True, "export": True},
            )
            orphan_data = b"orphan"
            import hashlib
            orphan_digest = hashlib.sha256(orphan_data).hexdigest()
            orphan_path = store._object_path(orphan_digest)
            orphan_path.parent.mkdir(parents=True, exist_ok=True)
            orphan_path.write_bytes(orphan_data)
            (store.staging / "left.tmp").write_bytes(b"partial")

            before = store.audit()
            self.assertIn(orphan_digest, before.unreferenced_objects)
            after = store.recover_orphans()
            self.assertFalse(orphan_path.exists())
            self.assertEqual(after.unreferenced_objects, ())
            kept_digest = manifest["sha256"].removeprefix("sha256:")
            self.assertTrue(store._object_path(kept_digest).exists())
            self.assertEqual(list(store.staging.iterdir()), [])

    def test_storage_without_rights_is_rejected_before_writing(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            with self.assertRaisesRegex(ValueError, "not permitted"):
                store.publish_bytes(
                    artifact_id=str(uuid4()),
                    data=b"x",
                    media_type="text/plain",
                    rights={"storage": False, "export": False},
                )
            self.assertEqual(store.audit().objects, 0)


    def test_legacy_manifest_must_be_rebound_before_read(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"evidence",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
                source_refs=["source:fixture"],
                metadata={"kind": "legacy-read-upgrade"},
            )
            path = store._manifest_path(artifact_id)
            legacy = dict(manifest)
            legacy.pop("manifest_hash")
            path.write_text(json.dumps(legacy), encoding="utf-8")
            with self.assertRaisesRegex(ArtifactIntegrityError, "lacks integrity binding"):
                store.read_bytes(artifact_id)
            rebound = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"evidence",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
                source_refs=["source:fixture"],
                metadata={"kind": "legacy-read-upgrade"},
            )
            self.assertIn("manifest_hash", rebound)
            self.assertEqual(store.read_bytes(artifact_id), b"evidence")

    def test_legacy_rebind_does_not_seal_untrusted_fields(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"legacy",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
                source_refs=["source:fixture"],
                metadata={"kind": "legacy-rebind"},
            )
            path = store._manifest_path(artifact_id)
            legacy = dict(manifest)
            legacy.pop("manifest_hash")
            legacy["created_at"] = "2000-01-01T00:00:00Z"
            legacy["untrusted_extra"] = "claim"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            rebound = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"legacy",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
                source_refs=["source:fixture"],
                metadata={"kind": "legacy-rebind"},
            )
            self.assertNotEqual(rebound["created_at"], "2000-01-01T00:00:00Z")
            self.assertNotIn("untrusted_extra", rebound)
            self.assertEqual(store.read_bytes(artifact_id), b"legacy")

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliable on Windows CI")
    def test_object_symlink_is_never_accepted(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            data = b"external-evidence"
            digest = hashlib.sha256(data).hexdigest()
            canonical = store._object_path(digest)
            canonical.parent.mkdir(parents=True, exist_ok=True)
            external = Path(directory) / "outside.bin"
            external.write_bytes(data)
            canonical.symlink_to(external)
            with self.assertRaisesRegex(ArtifactIntegrityError, "symlink"):
                store.publish_bytes(
                    artifact_id=str(uuid4()),
                    data=data,
                    media_type="application/octet-stream",
                    rights={"storage": True, "export": True},
                )

    def test_publish_and_export_sync_parent_directory(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            with patch("autotrade_research.artifacts.store.sync_parent_directory") as sync:
                store.publish_bytes(
                    artifact_id=artifact_id,
                    data=b"durable",
                    media_type="text/plain",
                    rights={"storage": True, "export": True},
                )
                sync.assert_any_call(store._object_path(hashlib.sha256(b"durable").hexdigest()))
                target = Path(directory) / "out" / "durable.txt"
                store.export(artifact_id, target)
                sync.assert_any_call(target)

    def test_recovery_preserves_malformed_and_misplaced_objects_for_inspection(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            malformed = store.objects / "gg" / ("g" * 64)
            malformed.parent.mkdir(parents=True, exist_ok=True)
            malformed.write_bytes(b"malformed")
            data = b"misplaced"
            digest = hashlib.sha256(data).hexdigest()
            wrong_prefix = "00" if digest[:2] != "00" else "ff"
            misplaced = store.objects / wrong_prefix / digest
            misplaced.parent.mkdir(parents=True, exist_ok=True)
            misplaced.write_bytes(data)
            orphan_data = b"valid-orphan"
            orphan_digest = hashlib.sha256(orphan_data).hexdigest()
            orphan = store._object_path(orphan_digest)
            orphan.parent.mkdir(parents=True, exist_ok=True)
            orphan.write_bytes(orphan_data)
            before = store.audit()
            self.assertIn(orphan_digest, before.unreferenced_objects)
            self.assertTrue(any(item.startswith("object:") for item in before.corrupt_objects))
            after = store.recover_orphans()
            self.assertFalse(orphan.exists())
            self.assertTrue(malformed.exists())
            self.assertTrue(misplaced.exists())
            self.assertEqual(after.unreferenced_objects, ())


if __name__ == "__main__":
    unittest.main()
