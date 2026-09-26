import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from autotrade_research.artifacts.store import (
    ArtifactConflict,
    ArtifactIntegrityError,
    ArtifactStore,
    _manifest_integrity_hash,
    atomic_write_json,
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

    def test_manifest_reparse_attribute_is_rejected_cross_platform(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            fake = SimpleNamespace(
                st_mode=0o100644,
                st_nlink=1,
                st_size=8,
                st_file_attributes=0x400,
            )
            candidate = store._manifest_path(str(uuid4()))
            with patch.object(
                store,
                "_validate_manifest_namespace",
                return_value=None,
            ), patch(
                "autotrade_research.artifacts.store.os.stat",
                return_value=fake,
            ):
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "Windows reparse point",
                ):
                    store._validate_manifest_entry(candidate)

    def test_object_reparse_attribute_fails_before_open_or_read(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            manifest = {
                "sha256": "sha256:" + "a" * 64,
                "bytes": 8,
            }
            fake = SimpleNamespace(
                st_mode=0o100644,
                st_nlink=1,
                st_size=8,
                st_file_attributes=0x400,
            )
            with patch.object(
                store,
                "load_manifest",
                return_value=manifest,
            ), patch.object(
                store,
                "_validate_object_namespace",
                return_value=None,
            ), patch(
                "autotrade_research.artifacts.store.os.stat",
                return_value=fake,
            ), patch(
                "autotrade_research.artifacts.store.os.open"
            ) as open_call, patch(
                "autotrade_research.artifacts.store.os.read"
            ) as read_call:
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "Windows reparse point",
                ):
                    store.read_bytes(artifact_id)
                open_call.assert_not_called()
                read_call.assert_not_called()

    def test_descriptor_reparse_attribute_fails_before_consumption(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            object_path = store._object_path("a" * 64)
            ordinary = SimpleNamespace(
                st_mode=0o100644,
                st_nlink=1,
                st_size=8,
                st_dev=1,
                st_ino=2,
                st_file_attributes=0,
            )
            reparsed = SimpleNamespace(
                st_mode=0o100644,
                st_nlink=1,
                st_size=8,
                st_dev=1,
                st_ino=2,
                st_file_attributes=0x400,
            )
            with patch.object(
                store,
                "_validate_object_entry",
                return_value=ordinary,
            ), patch(
                "autotrade_research.artifacts.store.os.open",
                return_value=123,
            ), patch(
                "autotrade_research.artifacts.store.os.fstat",
                return_value=reparsed,
            ), patch(
                "autotrade_research.artifacts.store.os.close"
            ) as close_call, patch(
                "autotrade_research.artifacts.store.os.read"
            ) as read_call:
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "Windows reparse point",
                ):
                    store._open_object_descriptor(
                        object_path,
                        expected_bytes=8,
                    )
                read_call.assert_not_called()
                close_call.assert_called_once_with(123)

    def test_read_rejects_oversized_swap_before_consuming_replacement(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"verified",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )
            canonical = store._object_path(
                manifest["sha256"].removeprefix("sha256:")
            )
            replacement = Path(directory) / "oversized-replacement.bin"
            replacement.write_bytes(b"x" * 4096)
            original_validate = store._validate_object_entry
            swapped = False

            def validate_then_swap(path):
                nonlocal swapped
                entry = original_validate(path)
                if not swapped:
                    swapped = True
                    os.replace(replacement, canonical)
                return entry

            with patch.object(
                store,
                "_validate_object_entry",
                side_effect=validate_then_swap,
            ), patch(
                "autotrade_research.artifacts.store.os.read"
            ) as read_call:
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "size mismatch",
                ):
                    store.read_bytes(artifact_id)
                read_call.assert_not_called()

    @unittest.skipIf(
        os.name == "nt",
        "open-object replacement semantics differ on Windows",
    )
    def test_read_holds_original_descriptor_across_path_replacement(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"verified",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )
            canonical = store._object_path(
                manifest["sha256"].removeprefix("sha256:")
            )
            replacement = Path(directory) / "same-size-replacement.bin"
            replacement.write_bytes(b"attacker")
            original_open = store._open_object_descriptor

            def open_then_swap(path, *, expected_bytes):
                descriptor, opened = original_open(
                    path,
                    expected_bytes=expected_bytes,
                )
                os.replace(replacement, canonical)
                return descriptor, opened

            with patch.object(
                store,
                "_open_object_descriptor",
                side_effect=open_then_swap,
            ):
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "path changed during descriptor read",
                ):
                    store.read_bytes(artifact_id)

    @unittest.skipIf(
        os.name == "nt",
        "symlink creation is not reliably available on Windows CI",
    )
    def test_symlink_swap_before_open_is_rejected_before_read(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"verified",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )
            canonical = store._object_path(
                manifest["sha256"].removeprefix("sha256:")
            )
            external = Path(directory) / "external.bin"
            external.write_bytes(b"x" * 4096)
            original_validate = store._validate_object_entry
            swapped = False

            def validate_then_swap(path):
                nonlocal swapped
                entry = original_validate(path)
                if not swapped:
                    swapped = True
                    canonical.unlink()
                    canonical.symlink_to(external)
                return entry

            with patch.object(
                store,
                "_validate_object_entry",
                side_effect=validate_then_swap,
            ), patch(
                "autotrade_research.artifacts.store.os.read"
            ) as read_call:
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "opened safely",
                ):
                    store.read_bytes(artifact_id)
                read_call.assert_not_called()

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and os.name != "nt",
        "FIFO replacement requires POSIX mkfifo",
    )
    def test_fifo_swap_before_open_is_rejected_without_blocking_read(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"verified",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )
            canonical = store._object_path(
                manifest["sha256"].removeprefix("sha256:")
            )
            original_validate = store._validate_object_entry
            swapped = False

            def validate_then_swap(path):
                nonlocal swapped
                entry = original_validate(path)
                if not swapped:
                    swapped = True
                    canonical.unlink()
                    os.mkfifo(canonical)
                return entry

            with patch.object(
                store,
                "_validate_object_entry",
                side_effect=validate_then_swap,
            ), patch(
                "autotrade_research.artifacts.store.os.read"
            ) as read_call:
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "descriptor must be a regular file",
                ):
                    store.read_bytes(artifact_id)
                read_call.assert_not_called()


    def test_export_is_rights_aware(self):
        with TemporaryDirectory() as directory:
            authorized: set[tuple[str, str]] = set()
            store = ArtifactStore(
                Path(directory) / "store",
                export_authorizer=lambda artifact_id, digest: (
                    artifact_id,
                    digest,
                )
                in authorized,
            )
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
            allowed_manifest = store.publish_bytes(
                artifact_id=allowed,
                data=b"public",
                media_type="text/plain",
                rights={"storage": True, "export": True},
            )
            authorized.add((allowed, allowed_manifest["sha256"]))
            target = store.export(allowed, Path(directory) / "out" / "allowed.txt")
            self.assertEqual(target.read_bytes(), b"public")

    def test_export_rejects_oversized_swap_before_copy(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(
                Path(directory) / "store",
                export_authorizer=lambda _artifact_id, _digest: True,
            )
            artifact_id = str(uuid4())
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"exported",
                media_type="application/octet-stream",
                rights={"storage": True, "export": True},
            )
            canonical = store._object_path(
                manifest["sha256"].removeprefix("sha256:")
            )
            replacement = Path(directory) / "oversized-export-replacement.bin"
            replacement.write_bytes(b"x" * 4096)
            target = Path(directory) / "out" / "artifact.bin"
            original_validate = store._validate_object_entry
            swapped = False

            def validate_then_swap(path):
                nonlocal swapped
                entry = original_validate(path)
                if not swapped:
                    swapped = True
                    os.replace(replacement, canonical)
                return entry

            with patch.object(
                store,
                "_validate_object_entry",
                side_effect=validate_then_swap,
            ), patch(
                "autotrade_research.artifacts.store.os.read"
            ) as read_call:
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "size mismatch",
                ):
                    store.export(artifact_id, target)
                read_call.assert_not_called()
            self.assertFalse(target.exists())


    def test_export_without_independent_authority_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            store.publish_bytes(
                artifact_id=artifact_id,
                data=b"public",
                media_type="text/plain",
                rights={"storage": True, "export": True},
            )

            with self.assertRaisesRegex(
                PermissionError,
                "independent export authorization is required",
            ):
                store.export(artifact_id, Path(directory) / "out.txt")

    def test_export_reverifies_object_bytes_after_authorization(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            artifact_id = str(uuid4())
            store: ArtifactStore

            def mutate_then_authorize(_artifact_id: str, _digest: str) -> bool:
                manifest = store.load_manifest(artifact_id)
                object_path = store._object_path(
                    manifest["sha256"].removeprefix("sha256:")
                )
                object_path.write_bytes(b"tampered")
                return True

            store = ArtifactStore(root, export_authorizer=mutate_then_authorize)
            store.publish_bytes(
                artifact_id=artifact_id,
                data=b"verified",
                media_type="application/octet-stream",
                rights={"storage": True, "export": True},
            )
            target = Path(directory) / "out.bin"

            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "changed during export copy",
            ):
                store.export(artifact_id, target)
            self.assertFalse(target.exists())

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

    def test_rehashed_manifest_cannot_grant_export_without_independent_authority(self):
        with TemporaryDirectory() as directory:
            authorized: set[tuple[str, str]] = set()
            store = ArtifactStore(
                Path(directory) / "store",
                export_authorizer=lambda artifact_id, digest: (
                    artifact_id,
                    digest,
                )
                in authorized,
            )
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
            tampered["manifest_hash"] = _manifest_integrity_hash(tampered)
            atomic_write_json(path, tampered)

            with self.assertRaisesRegex(
                PermissionError,
                "independent export authority does not permit export",
            ):
                store.export(artifact_id, Path(directory) / "out.txt")
            self.assertEqual(manifest["rights"]["export"], False)

    def test_rehashed_authenticated_manifest_rejects_unknown_top_level_fields(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            store.publish_bytes(
                artifact_id=artifact_id,
                data=b"evidence",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
                source_refs=["source:fixture"],
                metadata={"kind": "strict-contract"},
            )
            path = store._manifest_path(artifact_id)
            tampered = json.loads(path.read_text(encoding="utf-8"))
            tampered["unregistered_claim"] = {"qualified": True}
            tampered["manifest_hash"] = _manifest_integrity_hash(tampered)
            atomic_write_json(path, tampered)

            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "unexpected authenticated fields",
            ):
                store.load_manifest(artifact_id)

    def test_manifest_replacement_during_read_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"manifest-read-boundary",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
                metadata={"kind": "before"},
            )
            manifest_path = store._manifest_path(artifact_id)
            replacement = dict(manifest)
            replacement["metadata"] = {"kind": "after"}
            replacement["manifest_hash"] = _manifest_integrity_hash(replacement)
            replacement_path = Path(directory) / "replacement-manifest.json"
            atomic_write_json(replacement_path, replacement)

            original_validate = store._validate_manifest_entry
            validation_count = 0

            def validate_then_replace(path):
                nonlocal validation_count
                entry = original_validate(path)
                validation_count += 1
                if validation_count == 1:
                    os.replace(replacement_path, manifest_path)
                return entry

            with patch.object(
                store,
                "_validate_manifest_entry",
                side_effect=validate_then_replace,
            ):
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "manifest changed during read",
                ):
                    store.load_manifest(artifact_id)

    def test_manifest_hard_link_alias_is_rejected_and_audited(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            store.publish_bytes(
                artifact_id=artifact_id,
                data=b"manifest-alias",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )
            manifest_path = store._manifest_path(artifact_id)
            external_alias = Path(directory) / "manifest-hard-link.json"
            os.link(manifest_path, external_alias)

            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "manifest must not have hard-link aliases",
            ):
                store.load_manifest(artifact_id)

            audit = store.audit()
            self.assertIn(manifest_path.name, audit.corrupt_objects)

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliably available on Windows CI")
    def test_manifest_symlink_is_rejected_without_following_external_bytes(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            store.publish_bytes(
                artifact_id=artifact_id,
                data=b"manifest-symlink",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )
            manifest_path = store._manifest_path(artifact_id)
            external = Path(directory) / "external-manifest.json"
            external.write_bytes(manifest_path.read_bytes())
            manifest_path.unlink()
            manifest_path.symlink_to(external)

            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "manifest must not be a symlink",
            ):
                store.load_manifest(artifact_id)

            audit = store.audit()
            self.assertIn(manifest_path.name, audit.corrupt_objects)

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliably available on Windows CI")
    def test_manifest_directory_alias_fails_before_object_publication(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            store.manifests.rmdir()
            outside = Path(directory) / "outside-manifests"
            outside.mkdir()
            store.manifests.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "manifest path escapes store namespace",
            ):
                store.publish_bytes(
                    artifact_id=str(uuid4()),
                    data=b"must-not-publish",
                    media_type="application/octet-stream",
                    rights={"storage": True, "export": False},
                )

            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(list(store.objects.glob("*/*")), [])

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliably available on Windows CI")
    def test_staging_directory_alias_cannot_escape_store_namespace(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            store.staging.rmdir()
            outside = Path(directory) / "outside-staging"
            outside.mkdir()
            store.staging.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "staging path escapes store namespace",
            ):
                store.publish_bytes(
                    artifact_id=str(uuid4()),
                    data=b"must-stay-inside-store",
                    media_type="application/octet-stream",
                    rights={"storage": True, "export": False},
                )

            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(list(store.objects.glob("*/*")), [])

    def test_publish_preserves_canonical_rights_identity_and_rejects_extensions(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"evidence",
                media_type="application/octet-stream",
                rights={
                    "storage": True,
                    "export": False,
                    "rights_id": "capability-evidence-test",
                },
            )
            self.assertEqual(
                manifest["rights"],
                {
                    "storage": True,
                    "export": False,
                    "rights_id": "capability-evidence-test",
                },
            )

            invalid_rights = (
                {"storage": True, "export": 1},
                {"storage": True, "export": False, "qualified": True},
                {"storage": True, "export": False, "rights_id": ""},
                {"storage": True, "export": False, "rights_id": " padded "},
            )
            for rights in invalid_rights:
                with self.subTest(rights=rights), self.assertRaises(ValueError):
                    store.publish_bytes(
                        artifact_id=str(uuid4()),
                        data=b"evidence",
                        media_type="application/octet-stream",
                        rights=rights,
                    )

    def test_rehashed_authenticated_manifest_rejects_weakened_core_types(self):
        cases = (
            ("bytes", "8", "byte count"),
            (
                "rights",
                {"storage": True, "export": "yes"},
                "rights contract",
            ),
            (
                "rights",
                {"storage": True, "export": False, "qualified": True},
                "rights contract",
            ),
            ("source_refs", "source:fixture", "source_refs"),
            ("metadata", ["not", "an", "object"], "metadata"),
            ("created_at", "2026-09-26T12:00:00", "must include timezone"),
        )
        for field, replacement, expected_error in cases:
            with self.subTest(field=field):
                with TemporaryDirectory() as directory:
                    store = ArtifactStore(Path(directory) / "store")
                    artifact_id = str(uuid4())
                    store.publish_bytes(
                        artifact_id=artifact_id,
                        data=b"evidence",
                        media_type="application/octet-stream",
                        rights={"storage": True, "export": False},
                        source_refs=["source:fixture"],
                        metadata={"kind": "strict-contract"},
                    )
                    path = store._manifest_path(artifact_id)
                    tampered = json.loads(path.read_text(encoding="utf-8"))
                    tampered[field] = replacement
                    tampered["manifest_hash"] = _manifest_integrity_hash(tampered)
                    atomic_write_json(path, tampered)

                    with self.assertRaisesRegex(
                        ArtifactIntegrityError,
                        expected_error,
                    ):
                        store.load_manifest(artifact_id)

    def test_legacy_manifest_must_be_rebound_before_export(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(
                Path(directory) / "store",
                export_authorizer=lambda _artifact_id, _digest: True,
            )
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

            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "lacks integrity binding",
            ):
                store.read_bytes(artifact_id)

            upgraded = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"evidence",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
                source_refs=["source:fixture"],
                metadata={"kind": "legacy-read-upgrade"},
            )
            self.assertIn("manifest_hash", upgraded)
            self.assertEqual(store.read_bytes(artifact_id), b"evidence")

    def test_legacy_rebind_does_not_seal_untrusted_timestamp_or_extra_fields(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"legacy-evidence",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
                source_refs=["source:fixture"],
                metadata={"kind": "legacy-rebind"},
            )
            path = store._manifest_path(artifact_id)
            legacy = dict(manifest)
            legacy.pop("manifest_hash")
            legacy["created_at"] = "2000-01-01T00:00:00Z"
            legacy["untrusted_extra"] = {"claimed": "historical-proof"}
            path.write_text(json.dumps(legacy), encoding="utf-8")

            rebound = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"legacy-evidence",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
                source_refs=["source:fixture"],
                metadata={"kind": "legacy-rebind"},
            )

            self.assertIn("manifest_hash", rebound)
            self.assertNotEqual(rebound["created_at"], "2000-01-01T00:00:00Z")
            self.assertNotIn("untrusted_extra", rebound)
            self.assertEqual(
                set(rebound),
                {
                    "schema_version",
                    "artifact_id",
                    "sha256",
                    "bytes",
                    "media_type",
                    "rights",
                    "source_refs",
                    "metadata",
                    "created_at",
                    "manifest_hash",
                },
            )
            self.assertEqual(store.read_bytes(artifact_id), b"legacy-evidence")

    def test_recovery_preserves_all_objects_when_manifest_reference_is_unreadable(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"referenced-but-manifest-will-break",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )
            referenced_digest = manifest["sha256"].removeprefix("sha256:")
            referenced_object = store._object_path(referenced_digest)

            orphan_data = b"otherwise-deletable-orphan"
            orphan_digest = hashlib.sha256(orphan_data).hexdigest()
            orphan = store._object_path(orphan_digest)
            orphan.parent.mkdir(parents=True, exist_ok=True)
            orphan.write_bytes(orphan_data)

            manifest_path = store._manifest_path(artifact_id)
            manifest_path.write_text("{not-valid-json", encoding="utf-8")

            before = store.audit()
            self.assertIn(manifest_path.name, before.corrupt_objects)
            self.assertIn(referenced_digest, before.unreferenced_objects)
            self.assertIn(orphan_digest, before.unreferenced_objects)

            after = store.recover_orphans()

            self.assertTrue(referenced_object.exists())
            self.assertTrue(orphan.exists())
            self.assertIn(manifest_path.name, after.corrupt_objects)
            self.assertIn(referenced_digest, after.unreferenced_objects)
            self.assertIn(orphan_digest, after.unreferenced_objects)

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

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliably available on Windows CI")
    def test_recovery_refuses_aliased_staging_directory_and_preserves_external_files(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            store.staging.rmdir()
            outside = Path(directory) / "outside-staging-recovery"
            outside.mkdir()
            external = outside / "must-survive.tmp"
            external.write_bytes(b"external")
            store.staging.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "staging path escapes store namespace",
            ):
                store.recover_orphans()

            self.assertEqual(external.read_bytes(), b"external")

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliably available on Windows CI")
    def test_recovery_revalidates_orphan_namespace_immediately_before_unlink(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            data = b"recoverable-orphan"
            digest = hashlib.sha256(data).hexdigest()
            orphan = store._object_path(digest)
            orphan.parent.mkdir(parents=True, exist_ok=True)
            orphan.write_bytes(data)

            outside = Path(directory) / "outside-orphan-parent"
            outside.mkdir()
            external = outside / digest
            external.write_bytes(b"external-must-survive")

            original_audit = store.audit
            audit_calls = 0

            def audit_then_swap_namespace():
                nonlocal audit_calls
                result = original_audit()
                audit_calls += 1
                if audit_calls == 1:
                    orphan.unlink()
                    orphan.parent.rmdir()
                    orphan.parent.symlink_to(outside, target_is_directory=True)
                return result

            with patch.object(store, "audit", side_effect=audit_then_swap_namespace):
                store.recover_orphans()

            self.assertEqual(external.read_bytes(), b"external-must-survive")

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


    def test_canonical_object_hard_link_alias_is_rejected(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            data = b"hard-linked-evidence"
            digest = hashlib.sha256(data).hexdigest()
            canonical = store._object_path(digest)
            canonical.parent.mkdir(parents=True, exist_ok=True)
            external = Path(directory) / "outside-hard-link.bin"
            external.write_bytes(data)
            os.link(external, canonical)

            object_marker = "object:" + canonical.relative_to(store.root).as_posix()
            audit = store.audit()
            self.assertIn(object_marker, audit.corrupt_objects)
            self.assertNotIn(digest, audit.unreferenced_objects)

            with self.assertRaisesRegex(ArtifactIntegrityError, "hard-link aliases"):
                store.publish_bytes(
                    artifact_id=str(uuid4()),
                    data=data,
                    media_type="application/octet-stream",
                    rights={"storage": True, "export": False},
                )

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliably available on Windows CI")
    def test_digest_directory_symlink_cannot_escape_store_namespace(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            data = b"escaped-evidence"
            digest = hashlib.sha256(data).hexdigest()
            digest_dir = store.objects / digest[:2]
            outside = Path(directory) / "outside-digest-dir"
            outside.mkdir()
            digest_dir.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ArtifactIntegrityError, "escapes store namespace"):
                store.publish_bytes(
                    artifact_id=str(uuid4()),
                    data=data,
                    media_type="application/octet-stream",
                    rights={"storage": True, "export": False},
                )
            self.assertFalse((outside / digest).exists())

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliably available on Windows CI")
    def test_canonical_object_symlink_is_never_accepted_as_artifact_content(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            data = b"external-evidence"
            digest = hashlib.sha256(data).hexdigest()
            canonical = store._object_path(digest)
            canonical.parent.mkdir(parents=True, exist_ok=True)
            external = Path(directory) / "outside-object.bin"
            external.write_bytes(data)
            canonical.symlink_to(external)

            with self.assertRaisesRegex(ArtifactIntegrityError, "symlink"):
                store.publish_bytes(
                    artifact_id=str(uuid4()),
                    data=data,
                    media_type="application/octet-stream",
                    rights={"storage": True, "export": True},
                )

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliably available on Windows CI")
    def test_committed_manifest_fails_closed_if_object_is_replaced_by_symlink(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            data = b"durable-content"
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=data,
                media_type="application/octet-stream",
                rights={"storage": True, "export": True},
            )
            digest = manifest["sha256"].removeprefix("sha256:")
            canonical = store._object_path(digest)
            external = Path(directory) / "outside-object.bin"
            external.write_bytes(data)
            canonical.unlink()
            canonical.symlink_to(external)

            with self.assertRaisesRegex(ArtifactIntegrityError, "symlink"):
                store.read_bytes(artifact_id)
            with self.assertRaisesRegex(ArtifactIntegrityError, "symlink"):
                store.export(artifact_id, Path(directory) / "export.bin")

    def test_publish_and_export_sync_parent_directory_after_replace(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(
                Path(directory) / "store",
                export_authorizer=lambda _artifact_id, _digest: True,
            )
            artifact_id = str(uuid4())
            with patch(
                "autotrade_research.artifacts.store.sync_parent_directory"
            ) as sync:
                store.publish_bytes(
                    artifact_id=artifact_id,
                    data=b"durable",
                    media_type="text/plain",
                    rights={"storage": True, "export": True},
                )
                digest = hashlib.sha256(b"durable").hexdigest()
                sync.assert_any_call(store._object_path(digest))

                target = Path(directory) / "out" / "durable.txt"
                store.export(artifact_id, target)
                sync.assert_any_call(target)
                self.assertEqual(target.read_bytes(), b"durable")

    def test_crash_after_object_publish_before_manifest_leaves_recoverable_orphan(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            data = b"crash-boundary-evidence"
            artifact_id = str(uuid4())
            digest = hashlib.sha256(data).hexdigest()

            with patch(
                "autotrade_research.artifacts.store.atomic_write_json",
                side_effect=RuntimeError("simulated process death before manifest commit"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated process death"):
                    store.publish_bytes(
                        artifact_id=artifact_id,
                        data=data,
                        media_type="application/octet-stream",
                        rights={"storage": True, "export": False},
                    )

            object_path = store._object_path(digest)
            self.assertTrue(object_path.is_file())
            self.assertFalse(store._manifest_path(artifact_id).exists())
            self.assertIn(digest, store.audit().unreferenced_objects)

            recovered = ArtifactStore(directory).recover_orphans()
            self.assertFalse(object_path.exists())
            self.assertEqual(recovered.unreferenced_objects, ())

    def test_failed_object_replace_cleans_staging_without_manifest(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            artifact_id = str(uuid4())
            real_replace = os.replace

            def fail_object_replace(source, destination):
                if Path(destination).is_relative_to(store.objects):
                    raise OSError("simulated object replace failure")
                return real_replace(source, destination)

            with patch(
                "autotrade_research.artifacts.store.os.replace",
                side_effect=fail_object_replace,
            ):
                with self.assertRaisesRegex(OSError, "simulated object replace failure"):
                    store.publish_bytes(
                        artifact_id=artifact_id,
                        data=b"never-published",
                        media_type="application/octet-stream",
                        rights={"storage": True, "export": False},
                    )

            self.assertFalse(store._manifest_path(artifact_id).exists())
            self.assertEqual(store.audit().objects, 0)
            self.assertEqual(list(store.staging.iterdir()), [])

    def test_crash_during_object_directory_sync_leaves_recoverable_orphan(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            data = b"object-published-before-directory-sync"
            artifact_id = str(uuid4())
            digest = hashlib.sha256(data).hexdigest()

            with patch(
                "autotrade_research.artifacts.store.sync_parent_directory",
                side_effect=OSError("simulated object directory sync failure"),
            ):
                with self.assertRaisesRegex(OSError, "directory sync failure"):
                    store.publish_bytes(
                        artifact_id=artifact_id,
                        data=data,
                        media_type="application/octet-stream",
                        rights={"storage": True, "export": False},
                    )

            object_path = store._object_path(digest)
            self.assertTrue(object_path.is_file())
            self.assertFalse(store._manifest_path(artifact_id).exists())
            self.assertIn(digest, store.audit().unreferenced_objects)

            recovered = ArtifactStore(directory).recover_orphans()
            self.assertFalse(object_path.exists())
            self.assertEqual(recovered.unreferenced_objects, ())

    def test_crash_after_manifest_commit_recovers_committed_artifact(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            artifact_id = str(uuid4())

            def commit_then_crash(path, value):
                atomic_write_json(path, value)
                raise RuntimeError("simulated process death after manifest commit")

            with patch(
                "autotrade_research.artifacts.store.atomic_write_json",
                side_effect=commit_then_crash,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated process death after manifest commit",
                ):
                    store.publish_bytes(
                        artifact_id=artifact_id,
                        data=b"committed-before-crash",
                        media_type="application/octet-stream",
                        rights={"storage": True, "export": False},
                    )

            reopened = ArtifactStore(directory)
            self.assertEqual(
                reopened.read_bytes(artifact_id),
                b"committed-before-crash",
            )
            audit = reopened.audit()
            self.assertEqual(audit.manifests, 1)
            self.assertEqual(audit.objects, 1)
            self.assertEqual(audit.unreferenced_objects, ())
            self.assertEqual(audit.missing_objects, ())
            self.assertEqual(audit.corrupt_objects, ())

    def test_restart_after_manifest_commit_exposes_only_verified_complete_artifact(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            artifact_id = str(uuid4())
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=b"complete",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )

            reopened = ArtifactStore(directory)
            self.assertEqual(reopened.read_bytes(artifact_id), b"complete")
            audit = reopened.audit()
            self.assertEqual(audit.manifests, 1)
            self.assertEqual(audit.objects, 1)
            self.assertEqual(audit.unreferenced_objects, ())
            self.assertEqual(audit.missing_objects, ())
            self.assertEqual(audit.corrupt_objects, ())
            self.assertEqual(
                reopened.load_manifest(artifact_id)["manifest_hash"],
                manifest["manifest_hash"],
            )

    def test_recovery_reports_malformed_or_misplaced_objects_without_crashing(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)

            malformed_name = "g" * 64
            malformed = store.objects / "gg" / malformed_name
            malformed.parent.mkdir(parents=True, exist_ok=True)
            malformed.write_bytes(b"malformed")

            misplaced_data = b"misplaced"
            misplaced_digest = hashlib.sha256(misplaced_data).hexdigest()
            wrong_prefix = "00" if misplaced_digest[:2] != "00" else "ff"
            misplaced = store.objects / wrong_prefix / misplaced_digest
            misplaced.parent.mkdir(parents=True, exist_ok=True)
            misplaced.write_bytes(misplaced_data)

            orphan_data = b"valid-orphan"
            orphan_digest = hashlib.sha256(orphan_data).hexdigest()
            orphan = store._object_path(orphan_digest)
            orphan.parent.mkdir(parents=True, exist_ok=True)
            orphan.write_bytes(orphan_data)

            before = store.audit()
            self.assertIn(orphan_digest, before.unreferenced_objects)
            self.assertIn(
                "object:" + malformed.relative_to(store.root).as_posix(),
                before.corrupt_objects,
            )
            self.assertIn(
                "object:" + misplaced.relative_to(store.root).as_posix(),
                before.corrupt_objects,
            )

            after = store.recover_orphans()
            self.assertFalse(orphan.exists())
            self.assertTrue(malformed.exists())
            self.assertTrue(misplaced.exists())
            self.assertEqual(after.unreferenced_objects, ())
            self.assertIn(
                "object:" + malformed.relative_to(store.root).as_posix(),
                after.corrupt_objects,
            )
            self.assertIn(
                "object:" + misplaced.relative_to(store.root).as_posix(),
                after.corrupt_objects,
            )

if __name__ == "__main__":
    unittest.main()
