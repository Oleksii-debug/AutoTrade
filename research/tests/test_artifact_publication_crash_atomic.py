import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from autotrade_research.artifacts.store import ArtifactIntegrityError, ArtifactStore


@unittest.skipIf(
    sys.platform == "win32",
    "hard-crash prefix replacement regression requires POSIX rename semantics",
)
class PublicationCrashAtomicityTests(unittest.TestCase):
    def _environment(self) -> dict[str, str]:
        research_root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        current_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            part
            for part in (str(research_root), current_pythonpath)
            if part
        )
        return environment

    def test_process_death_after_durable_prepared_manifest_cannot_promote_replacement_prefix(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            artifact_id = str(uuid4())
            payload = b"hard-crash-prepared-manifest"

            child = r'''
import os
from pathlib import Path
import sys

from autotrade_research.artifacts import _retained_publication_hardening as publication
from autotrade_research.artifacts.store import ArtifactStore

root = Path(sys.argv[1])
artifact_id = sys.argv[2]
payload = b"hard-crash-prepared-manifest"
store = ArtifactStore(root)
real_publish_manifest = publication._publish_manifest_posix


def publish_prepared_then_replace_prefix_and_die(self, *, manifest, replace_existing):
    result = real_publish_manifest(
        self,
        manifest=manifest,
        replace_existing=replace_existing,
    )
    if manifest.get("publication_state") == "PREPARED" and not replace_existing:
        digest = manifest["sha256"].removeprefix("sha256:")
        prefix = self.objects / digest[:2]
        detached = self.objects / ("detached-" + digest[:2])
        replacement = root.parent / ("replacement-" + digest[:2])
        replacement.mkdir()
        (replacement / digest).write_bytes(payload)
        os.replace(prefix, detached)
        os.replace(replacement, prefix)
        os._exit(91)
    return result


publication._publish_manifest_posix = publish_prepared_then_replace_prefix_and_die
store.publish_bytes(
    artifact_id=artifact_id,
    data=payload,
    media_type="application/octet-stream",
    rights={"storage": True, "export": False},
)
raise AssertionError("publication unexpectedly survived hard-crash seam")
'''
            completed = subprocess.run(
                [sys.executable, "-c", child, str(root), artifact_id],
                env=self._environment(),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                91,
                msg=f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
            )

            manifest_path = root / "manifests" / f"{artifact_id}.json"
            self.assertTrue(manifest_path.is_file())
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(raw["schema_version"], 2)
            self.assertEqual(raw["publication_state"], "PREPARED")
            prepared_bytes = manifest_path.read_bytes()

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

            audit = reopened.audit()
            self.assertIn(manifest_path.name, audit.corrupt_objects)
            self.assertEqual(audit.manifests, 1)

            # Equal bytes in a replacement prefix are not enough to recover an
            # interrupted publication. Retry must honor the generation recorded
            # by PREPARED and leave that durable forensic barrier untouched.
            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "object generation changed after publication",
            ):
                reopened.publish_bytes(
                    artifact_id=artifact_id,
                    data=payload,
                    media_type="application/octet-stream",
                    rights={"storage": True, "export": False},
                )
            self.assertEqual(manifest_path.read_bytes(), prepared_bytes)
            still_prepared = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(still_prepared["publication_state"], "PREPARED")

    def test_process_death_after_durable_committed_manifest_rejects_replacement_prefix_on_reopen(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            artifact_id = str(uuid4())
            payload = b"hard-crash-committed-manifest"

            child = r'''
import os
from pathlib import Path
import sys

from autotrade_research.artifacts import _retained_publication_hardening as publication
from autotrade_research.artifacts.store import ArtifactStore

root = Path(sys.argv[1])
artifact_id = sys.argv[2]
payload = b"hard-crash-committed-manifest"
store = ArtifactStore(root)
real_publish_manifest = publication._publish_manifest_posix


def publish_committed_then_replace_prefix_and_die(self, *, manifest, replace_existing):
    result = real_publish_manifest(
        self,
        manifest=manifest,
        replace_existing=replace_existing,
    )
    if manifest.get("publication_state") == "COMMITTED" and replace_existing:
        digest = manifest["sha256"].removeprefix("sha256:")
        prefix = self.objects / digest[:2]
        detached = self.objects / ("detached-" + digest[:2])
        replacement = root.parent / ("replacement-" + digest[:2])
        replacement.mkdir()
        (replacement / digest).write_bytes(payload)
        os.replace(prefix, detached)
        os.replace(replacement, prefix)
        os._exit(92)
    return result


publication._publish_manifest_posix = publish_committed_then_replace_prefix_and_die
store.publish_bytes(
    artifact_id=artifact_id,
    data=payload,
    media_type="application/octet-stream",
    rights={"storage": True, "export": False},
)
raise AssertionError("publication unexpectedly survived hard-crash seam")
'''
            completed = subprocess.run(
                [sys.executable, "-c", child, str(root), artifact_id],
                env=self._environment(),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                92,
                msg=f"stdout={completed.stdout!r} stderr={completed.stderr!r}",
            )

            manifest_path = root / "manifests" / f"{artifact_id}.json"
            self.assertTrue(manifest_path.is_file())
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(raw["schema_version"], 2)
            self.assertEqual(raw["publication_state"], "COMMITTED")

            reopened = ArtifactStore(root)
            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "object generation changed after publication",
            ):
                reopened.read_bytes(artifact_id)
            with self.assertRaisesRegex(
                ArtifactIntegrityError,
                "object generation changed after publication",
            ):
                reopened.read_authenticated_snapshot(artifact_id)

            audit = reopened.audit()
            self.assertIn(manifest_path.name, audit.corrupt_objects)
            self.assertEqual(audit.manifests, 1)


if __name__ == "__main__":
    unittest.main()
