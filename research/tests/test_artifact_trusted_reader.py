import gc
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import autotrade_research.artifacts._root_authority as root_authority

from autotrade_research.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)


ARTIFACT_ID = "11111111-1111-4111-8111-111111111111"
PAYLOAD = b"sealed artifact evidence"


class _ForgedArtifactStore(ArtifactStore):
    def read_authenticated_snapshot(self, artifact_id):
        return (
            {
                "artifact_id": artifact_id,
                "manifest_hash": "sha256:" + "0" * 64,
                "sha256": "sha256:" + "0" * 64,
            },
            b"forged",
        )


def _publish(store: ArtifactStore, payload: bytes = PAYLOAD):
    return store.publish_bytes(
        artifact_id=ARTIFACT_ID,
        data=payload,
        media_type="application/octet-stream",
        rights={"storage": True, "export": False},
    )


def _poison_injected_instance(store: ArtifactStore, directory: str) -> None:
    redirected = Path(directory) / "attacker-store"
    object.__setattr__(store, "root", redirected)
    object.__setattr__(store, "objects", redirected / "objects" / "sha256")
    object.__setattr__(store, "manifests", redirected / "manifests")
    object.__setattr__(store, "staging", redirected / "staging")
    object.__setattr__(store, "lock_path", redirected / ".artifact-store.lock")
    object.__setattr__(
        store,
        "_read_verified_object_bytes",
        lambda _manifest: b"forged",
    )
    object.__setattr__(
        store,
        "_decode_manifest_bytes",
        lambda _raw, **_kwargs: {
            "artifact_id": ARTIFACT_ID,
            "manifest_hash": "sha256:" + "0" * 64,
            "sha256": "sha256:" + "0" * 64,
        },
    )
    object.__setattr__(
        store,
        "_manifest_path",
        lambda _artifact_id: redirected / "forged.json",
    )


class TrustedArtifactReaderTests(unittest.TestCase):
    def test_trusted_read_never_calls_artifact_store_constructor(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            manifest = _publish(store)
            read_snapshot = trusted_authenticated_reader(
                root,
                publication_store=store,
            )

            original_init = ArtifactStore.__init__

            def forbidden_init(*_args, **_kwargs):
                raise AssertionError(
                    "trusted read must not construct or initialize ArtifactStore"
                )

            ArtifactStore.__init__ = forbidden_init
            try:
                observed_manifest, observed_data = read_snapshot(ARTIFACT_ID)
            finally:
                ArtifactStore.__init__ = original_init

            self.assertEqual(observed_manifest, manifest)
            self.assertEqual(observed_data, PAYLOAD)

    def test_issued_reader_keeps_installed_authenticated_read_dispatch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            manifest = _publish(store)
            read_snapshot = trusted_authenticated_reader(
                root,
                publication_store=store,
            )

            original_read = ArtifactStore.read_authenticated_snapshot

            def forged_read(_self, artifact_id):
                return (
                    {
                        "artifact_id": artifact_id,
                        "manifest_hash": "sha256:" + "0" * 64,
                        "sha256": "sha256:" + "0" * 64,
                    },
                    b"forged",
                )

            ArtifactStore.read_authenticated_snapshot = forged_read
            try:
                observed_manifest, observed_data = read_snapshot(ARTIFACT_ID)
            finally:
                ArtifactStore.read_authenticated_snapshot = original_read

            self.assertEqual(observed_manifest, manifest)
            self.assertEqual(observed_data, PAYLOAD)

    @unittest.skipIf(os.name == "nt", "deterministic lexical swap injection is POSIX-only")
    def test_swap_after_preflight_fails_before_touching_replacement_root(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            _publish(store)
            read_snapshot = trusted_authenticated_reader(
                root,
                publication_store=store,
            )

            detached = Path(directory) / "detached"
            original_generation = root_authority._immutable_configured_generation
            swapped = False

            def swap_after_first_preflight(root_key):
                nonlocal swapped
                observed = original_generation(root_key)
                if not swapped:
                    root.rename(detached)
                    root.mkdir()
                    swapped = True
                return observed

            root_authority._immutable_configured_generation = (
                swap_after_first_preflight
            )
            try:
                with self.assertRaises(ArtifactIntegrityError):
                    read_snapshot(ARTIFACT_ID)
                self.assertEqual(tuple(root.iterdir()), ())
            finally:
                root_authority._immutable_configured_generation = (
                    original_generation
                )
                if root.exists():
                    root.rmdir()
                if detached.exists():
                    detached.rename(root)

    def test_reader_gc_releases_module_owned_generation_capability(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            _publish(store)
            read_snapshot = trusted_authenticated_reader(
                root,
                publication_store=store,
            )
            reader_id = id(read_snapshot)
            self.assertIn(reader_id, root_authority._READER_CAPABILITIES)

            del read_snapshot
            gc.collect()

            self.assertNotIn(reader_id, root_authority._READER_CAPABILITIES)

    def test_private_reader_preserves_canonical_store_wrapper_contract(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            manifest = _publish(store)

            read_snapshot = trusted_authenticated_reader(
                root,
                publication_store=store,
            )
            observed_manifest, observed_data = read_snapshot(ARTIFACT_ID)

            self.assertEqual(observed_manifest, manifest)
            self.assertEqual(observed_data, PAYLOAD)
            self.assertEqual(store.audit().missing_objects, ())
            self.assertEqual(store.recover_orphans().missing_objects, ())

    def test_reader_exposes_no_mutable_private_store_or_closure_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            manifest = _publish(store)
            read_snapshot = trusted_authenticated_reader(
                root,
                publication_store=store,
            )

            self.assertIsNone(getattr(read_snapshot, "__closure__", None))
            self.assertFalse(hasattr(read_snapshot, "__dict__"))
            with self.assertRaises(AttributeError):
                object.__setattr__(
                    read_snapshot,
                    "root_key",
                    str(Path(directory) / "attacker-store"),
                )

            # Reproduce caller-side instance poisoning after introspection. The
            # only caller-accessible mutable store is the publication object and
            # it has zero authority over the trusted read execution instance.
            _poison_injected_instance(store, directory)
            observed_manifest, observed_data = read_snapshot(ARTIFACT_ID)

            self.assertEqual(observed_manifest, manifest)
            self.assertEqual(observed_data, PAYLOAD)

    def test_mutation_before_reader_construction_cannot_select_root_or_helpers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            manifest = _publish(store)
            _poison_injected_instance(store, directory)

            read_snapshot = trusted_authenticated_reader(
                root,
                publication_store=store,
            )
            observed_manifest, observed_data = read_snapshot(ARTIFACT_ID)

            self.assertEqual(observed_manifest, manifest)
            self.assertEqual(observed_data, PAYLOAD)

    def test_mutation_after_reader_construction_cannot_redirect_later_reads(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            manifest = _publish(store)
            read_snapshot = trusted_authenticated_reader(
                root,
                publication_store=store,
            )

            _poison_injected_instance(store, directory)
            observed_manifest, observed_data = read_snapshot(ARTIFACT_ID)

            self.assertEqual(observed_manifest, manifest)
            self.assertEqual(observed_data, PAYLOAD)

    def test_stable_attacker_selected_publication_root_is_rejected(self):
        with TemporaryDirectory() as directory:
            trusted_root = Path(directory) / "trusted"
            trusted_store = ArtifactStore(trusted_root)
            _publish(trusted_store)

            attacker_root = Path(directory) / "attacker"
            attacker_store = ArtifactStore(attacker_root)
            _publish(attacker_store, b"self-consistent forged bytes")

            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "does not match trusted artifact root",
            ):
                trusted_authenticated_reader(
                    trusted_root,
                    publication_store=attacker_store,
                )

    def test_private_reader_rejects_subclass_publication_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = _ForgedArtifactStore(root)
            with self.assertRaisesRegex(TypeError, "canonical ArtifactStore"):
                trusted_authenticated_reader(
                    root,
                    publication_store=store,
                )

    def test_reader_does_not_depend_on_publication_store_lifetime(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            _publish(store)
            read_snapshot = trusted_authenticated_reader(
                root,
                publication_store=store,
            )
            del store
            gc.collect()

            _manifest, observed_data = read_snapshot(ARTIFACT_ID)
            self.assertEqual(observed_data, PAYLOAD)

    def test_missing_object_still_fails_closed_after_caller_instance_poisoning(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            manifest = _publish(store)
            digest = manifest["sha256"].removeprefix("sha256:")
            object_path = store.objects / digest[:2] / digest
            read_snapshot = trusted_authenticated_reader(
                root,
                publication_store=store,
            )
            object_path.unlink()
            _poison_injected_instance(store, directory)

            with self.assertRaises(ArtifactIntegrityError):
                read_snapshot(ARTIFACT_ID)

    @unittest.skipIf(os.name == "nt", "Windows retained handles intentionally fence rename")
    def test_manifest_namespace_replacement_is_detected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            _publish(store)
            read_snapshot = trusted_authenticated_reader(
                root,
                publication_store=store,
            )

            manifests = root / "manifests"
            replacement = Path(directory) / "replacement-manifests"
            shutil.copytree(manifests, replacement)
            manifests.rename(root / "manifests.genuine")
            replacement.rename(manifests)

            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "namespace generation changed",
            ):
                read_snapshot(ARTIFACT_ID)

    @unittest.skipIf(os.name == "nt", "Windows retained handles intentionally fence rename")
    def test_object_namespace_replacement_is_detected_even_with_identical_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            _publish(store)
            read_snapshot = trusted_authenticated_reader(
                root,
                publication_store=store,
            )

            objects = root / "objects" / "sha256"
            replacement = Path(directory) / "replacement-objects"
            shutil.copytree(objects, replacement)
            objects.rename(root / "objects" / "sha256.genuine")
            replacement.rename(objects)

            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "namespace generation changed",
            ):
                read_snapshot(ARTIFACT_ID)

    @unittest.skipIf(os.name == "nt", "Windows retained handles intentionally fence rename")
    def test_original_manifest_generation_remains_pinned_until_reader_dies(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            manifest = _publish(store)
            read_snapshot = trusted_authenticated_reader(
                root,
                publication_store=store,
            )

            manifests = root / "manifests"
            detached = root / "manifests.detached"
            manifests.rename(detached)
            manifests.mkdir()
            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "namespace generation changed",
            ):
                read_snapshot(ARTIFACT_ID)

            manifests.rmdir()
            detached.rename(manifests)
            observed_manifest, observed_data = read_snapshot(ARTIFACT_ID)
            self.assertEqual(observed_manifest, manifest)
            self.assertEqual(observed_data, PAYLOAD)

    @unittest.skipIf(os.name == "nt", "Windows retained handles intentionally fence rename")
    def test_lexical_root_replacement_is_detected_by_private_root_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            _publish(store)
            read_snapshot = trusted_authenticated_reader(
                root,
                publication_store=store,
            )

            detached = Path(directory) / "detached"
            root.rename(detached)
            root.mkdir()
            try:
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "configured artifact",
                ):
                    read_snapshot(ARTIFACT_ID)
            finally:
                root.rmdir()
                detached.rename(root)


if __name__ == "__main__":
    unittest.main()
