from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_research.artifacts.store import (
    ArtifactStore,
    read_trusted_authenticated_snapshot,
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


class TrustedArtifactReaderTests(unittest.TestCase):
    def test_canonical_store_has_no_instance_dictionary_or_shadowable_helpers(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            self.assertFalse(hasattr(store, "__dict__"))
            with self.assertRaisesRegex(
                AttributeError,
                "immutable after construction",
            ):
                store._read_verified_object_bytes = lambda manifest: b"forged"
            with self.assertRaisesRegex(
                AttributeError,
                "immutable after construction",
            ):
                store.root = Path(directory) / "redirected"

    def test_trusted_reader_rejects_subclass_even_when_it_delegates_or_overrides(self):
        with TemporaryDirectory() as directory:
            store = _ForgedArtifactStore(directory)
            with self.assertRaisesRegex(
                TypeError,
                "canonical ArtifactStore",
            ):
                read_trusted_authenticated_snapshot(store, ARTIFACT_ID)

    def test_trusted_reader_returns_one_canonical_authenticated_snapshot(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            manifest = store.publish_bytes(
                artifact_id=ARTIFACT_ID,
                data=PAYLOAD,
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )
            observed_manifest, observed_data = read_trusted_authenticated_snapshot(
                store,
                ARTIFACT_ID,
            )
            self.assertEqual(observed_manifest, manifest)
            self.assertEqual(observed_data, PAYLOAD)

    def test_failed_instance_shadow_does_not_bypass_missing_object_detection(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            manifest = store.publish_bytes(
                artifact_id=ARTIFACT_ID,
                data=PAYLOAD,
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )
            digest = manifest["sha256"].removeprefix("sha256:")
            object_path = store.objects / digest[:2] / digest
            object_path.unlink()

            with self.assertRaises(AttributeError):
                store._read_verified_object_bytes = lambda ignored: PAYLOAD

            with self.assertRaisesRegex(
                ValueError,
                "artifact object is missing",
            ):
                read_trusted_authenticated_snapshot(store, ARTIFACT_ID)


if __name__ == "__main__":
    unittest.main()
