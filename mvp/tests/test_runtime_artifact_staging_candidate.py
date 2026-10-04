from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from tools.stage_windows_foundation import stage_windows_foundation
from tools.stage_windows_runtime import _RUNTIME_REQUIRED, stage_windows_runtime


ROOT = Path(__file__).resolve().parents[2]


class NeutralRuntimeInstalledCandidateTests(unittest.TestCase):
    def test_runtime_descriptor_set_is_module_owned_and_complete(self):
        paths = tuple(item.path for item in _RUNTIME_REQUIRED)
        self.assertEqual(len(paths), 30)
        self.assertEqual(len(paths), len(set(paths)))
        self.assertIn("autotrade_runtime/__init__.py", paths)
        self.assertIn("autotrade_runtime/resource_lock.py", paths)
        self.assertIn("autotrade_runtime/strict_json.py", paths)
        self.assertIn("autotrade_runtime/artifacts/store.py", paths)
        self.assertIn("autotrade_runtime/artifacts/_root_authority.py", paths)
        self.assertNotIn("research/autotrade_research/artifacts/content_store.py", paths)
        self.assertFalse(any(path.startswith("research/") for path in paths))

    def test_exact_staged_runtime_supports_artifact_publish_and_sealed_read(self):
        source_sha = subprocess.check_output(
            ("git", "rev-parse", "--verify", "HEAD"),
            cwd=ROOT,
            text=True,
        ).strip()
        self.assertRegex(source_sha, r"^[0-9a-f]{40}$")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            composition = root / "composition.json"
            composition.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "product": "AutoTrade",
                        "source_sha": source_sha,
                        "components": [],
                    }
                ),
                encoding="utf-8",
            )
            stage_windows_foundation(
                staging=staging,
                composition_path=composition,
                source_root=ROOT,
            )
            staged_runtime = stage_windows_runtime(
                staging=staging,
                composition_path=composition,
                source_root=ROOT,
            )
            self.assertEqual(
                {item["path"] for item in staged_runtime},
                {item.path for item in _RUNTIME_REQUIRED},
            )

            artifact_root = root / "artifact-store"
            script = r"""
import os
from pathlib import Path
import sys
assert sys.flags.isolated == 1 and sys.flags.no_site == 1
staging = Path(os.environ["AUTOTRADE_STAGING"]).resolve(strict=True)
sys.path.insert(0, str(staging))

import autotrade_foundation
import autotrade_runtime
import autotrade_runtime.artifacts as artifacts
from autotrade_runtime.artifacts import ArtifactStore, trusted_authenticated_reader

for name, module in tuple(sys.modules.items()):
    if name == "autotrade_foundation" or name.startswith("autotrade_foundation.") or name == "autotrade_runtime" or name.startswith("autotrade_runtime."):
        module_file = Path(module.__file__).resolve(strict=True)
        assert module_file.is_relative_to(staging), (name, module_file)
assert not any(name == "research" or name.startswith("research.") for name in sys.modules)
assert not any(name == "autotrade_research" or name.startswith("autotrade_research.") for name in sys.modules)
assert artifacts.CANONICAL_ARTIFACT_STORE_MODULE == "autotrade_runtime.artifacts.store"
assert ArtifactStore.__module__ == "autotrade_runtime.artifacts.store"

root = Path(os.environ["AUTOTRADE_ARTIFACT_ROOT"])
store = ArtifactStore(root)
artifact_id = "00000000-0000-0000-0000-000000000108"
payload = b"neutral-runtime-artifact-payload"
manifest = store.publish_bytes(
    artifact_id=artifact_id,
    data=payload,
    media_type="application/octet-stream",
    rights={"storage": True, "export": False},
    source_refs=["candidate:wp06"],
    metadata={"purpose": "installed-runtime-oracle"},
)
snapshot_manifest, snapshot_payload = store.read_authenticated_snapshot(artifact_id)
assert snapshot_payload == payload
assert snapshot_manifest["sha256"] == manifest["sha256"]

reader = trusted_authenticated_reader(root, publication_store=store)
trusted_manifest, trusted_payload = reader(artifact_id)
assert trusted_payload == payload
assert trusted_manifest["artifact_id"] == artifact_id
assert trusted_manifest["sha256"] == manifest["sha256"]
audit = store.audit()
assert audit.manifests == 1
assert audit.missing_objects == ()
assert audit.corrupt_objects == ()
assert not any(name == "research" or name.startswith("research.") for name in sys.modules)
assert not any(name == "autotrade_research" or name.startswith("autotrade_research.") for name in sys.modules)
print("NEUTRAL_ARTIFACT_RUNTIME_OK")
"""
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            env["AUTOTRADE_STAGING"] = str(staging)
            env["AUTOTRADE_ARTIFACT_ROOT"] = str(artifact_root)
            completed = subprocess.run(
                (sys.executable, "-I", "-S", "-c", script),
                cwd=staging,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            self.assertEqual(
                completed.stdout.strip(),
                "NEUTRAL_ARTIFACT_RUNTIME_OK",
            )


if __name__ == "__main__":
    unittest.main()
