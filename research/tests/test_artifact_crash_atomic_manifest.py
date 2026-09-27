from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import textwrap
import unittest
from uuid import uuid4

from autotrade_research.artifacts.store import ArtifactIntegrityError, ArtifactStore


class CrashAtomicManifestTests(unittest.TestCase):
    def test_committed_v2_manifest_is_generation_bound(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            artifact_id = str(uuid4())
            payload = b"generation-bound-committed-object"
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=payload,
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["publication_state"], "COMMITTED")
            self.assertIn("object_generation", manifest)
            self.assertEqual(store.read_bytes(artifact_id), payload)

    def test_hashless_committed_v2_manifest_is_not_metadata_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            artifact_id = str(uuid4())
            store.publish_bytes(
                artifact_id=artifact_id,
                data=b"authenticated-v2-only",
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )

            manifest_path = root / "manifests" / f"{artifact_id}.json"
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(raw["schema_version"], 2)
            self.assertEqual(raw["publication_state"], "COMMITTED")
            raw.pop("manifest_hash")
            manifest_path.write_text(
                json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )

            reopened = ArtifactStore(root)
            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "lacks integrity binding",
            ):
                reopened.load_manifest(artifact_id)
            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "lacks integrity binding",
            ):
                reopened.read_authenticated_snapshot(artifact_id)

    def test_hashless_prepared_v2_manifest_cannot_drive_retry_rollback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ArtifactStore(root)
            artifact_id = str(uuid4())
            payload = b"hashless-prepared-must-not-rollback"
            store.publish_bytes(
                artifact_id=artifact_id,
                data=payload,
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )

            manifest_path = root / "manifests" / f"{artifact_id}.json"
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw["publication_state"] = "PREPARED"
            raw.pop("manifest_hash")
            forged = json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n"
            manifest_path.write_text(forged, encoding="utf-8")
            forged_bytes = manifest_path.read_bytes()

            reopened = ArtifactStore(root)
            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "lacks integrity binding",
            ):
                reopened.publish_bytes(
                    artifact_id=artifact_id,
                    data=payload,
                    media_type="application/octet-stream",
                    rights={"storage": True, "export": False},
                )
            self.assertEqual(manifest_path.read_bytes(), forged_bytes)

    @unittest.skipIf(
        sys.platform == "win32",
        "same-bytes prefix replacement regression uses POSIX directory rename semantics",
    )
    def test_hard_kill_after_prepared_manifest_never_becomes_authority_after_restart(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            artifact_id = str(uuid4())
            payload = b"hard-kill-prepared-generation"
            digest = sha256(payload).hexdigest()
            research_root = Path(__file__).resolve().parents[1]
            script = textwrap.dedent(
                f"""
                import os
                from pathlib import Path
                from unittest.mock import patch
                from autotrade_research.artifacts import _retained_publication_hardening as publication
                from autotrade_research.artifacts.store import ArtifactStore

                root = Path({str(root)!r})
                store = ArtifactStore(root)
                real_publish = publication._publish_manifest_posix

                def durable_prepared_then_die(*args, **kwargs):
                    result = real_publish(*args, **kwargs)
                    manifest = kwargs.get('manifest')
                    if manifest and manifest.get('publication_state') == 'PREPARED':
                        os._exit(73)
                    return result

                with patch.object(
                    publication,
                    '_publish_manifest_posix',
                    side_effect=durable_prepared_then_die,
                ):
                    store.publish_bytes(
                        artifact_id={artifact_id!r},
                        data={payload!r},
                        media_type='application/octet-stream',
                        rights={{'storage': True, 'export': False}},
                    )
                """
            )
            environment = dict(os.environ)
            prior = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = (
                str(research_root)
                if not prior
                else os.pathsep.join((str(research_root), prior))
            )
            completed = subprocess.run(
                [sys.executable, "-c", script],
                env=environment,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(
                completed.returncode,
                73,
                msg=completed.stdout + completed.stderr,
            )

            manifest_path = root / "manifests" / f"{artifact_id}.json"
            self.assertTrue(manifest_path.exists())
            reopened = ArtifactStore(root)
            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "publication is not committed",
            ):
                reopened.load_manifest(artifact_id)
            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "publication is not committed",
            ):
                reopened.read_bytes(artifact_id)

            prefix = root / "objects" / "sha256" / digest[:2]
            detached = root / "objects" / "sha256" / f"detached-{digest[:2]}"
            replacement = Path(directory) / "same-bytes-prefix"
            replacement.mkdir()
            (replacement / digest).write_bytes(payload)
            os.replace(prefix, detached)
            os.replace(replacement, prefix)
            try:
                after_swap = ArtifactStore(root)
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "publication is not committed",
                ):
                    after_swap.read_bytes(artifact_id)
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "object generation changed after publication",
                ):
                    after_swap.publish_bytes(
                        artifact_id=artifact_id,
                        data=payload,
                        media_type="application/octet-stream",
                        rights={"storage": True, "export": False},
                    )
                # The interrupted PREPARED record remains a forensic barrier;
                # retry must not erase/rebind it around a same-bytes replacement.
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "publication is not committed",
                ):
                    after_swap.load_manifest(artifact_id)
            finally:
                for entry in prefix.iterdir():
                    entry.unlink()
                prefix.rmdir()
                os.replace(detached, prefix)


if __name__ == "__main__":
    unittest.main()
