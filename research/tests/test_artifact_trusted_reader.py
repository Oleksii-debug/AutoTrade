import gc
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

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
                    "configured artifact store root",
                ):
                    read_snapshot(ARTIFACT_ID)
            finally:
                root.rmdir()
                detached.rename(root)


if __name__ == "__main__":
    unittest.main()
