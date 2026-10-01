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

_INVERSE_REQUIRED = (
    "autotrade_foundation/local_filesystem.py",
    "autotrade_runtime/resource_lock.py",
    "autotrade_runtime/strict_json.py",
    "autotrade_runtime/artifacts/store.py",
)


def _write_composition(path: Path, source_sha: str) -> None:
    path.write_text(
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


def _isolated_environment(*, staging: Path, artifact_root: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["AUTOTRADE_STAGING"] = str(staging)
    if artifact_root is not None:
        env["AUTOTRADE_ARTIFACT_ROOT"] = str(artifact_root)
    return env


class NeutralRuntimeInstalledCandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_sha = subprocess.check_output(
            ("git", "rev-parse", "--verify", "HEAD"),
            cwd=ROOT,
            text=True,
        ).strip()
        if not __import__("re").fullmatch(r"[0-9a-f]{40}", cls.source_sha):
            raise AssertionError("test requires one exact Git commit SHA")

    def _stage(self, root: Path) -> tuple[Path, Path]:
        staging = root / "staging"
        staging.mkdir()
        composition = root / "composition.json"
        _write_composition(composition, self.source_sha)
        stage_windows_foundation(
            staging=staging,
            composition_path=composition,
            source_root=ROOT,
        )
        stage_windows_runtime(
            staging=staging,
            composition_path=composition,
            source_root=ROOT,
        )
        return staging, composition

    def test_runtime_descriptor_set_is_module_owned_and_complete(self):
        paths = tuple(item.path for item in _RUNTIME_REQUIRED)
        self.assertEqual(len(paths), 30)
        self.assertEqual(len(paths), len(set(paths)))
        self.assertIn("autotrade_runtime/__init__.py", paths)
        self.assertIn("autotrade_runtime/resource_lock.py", paths)
        self.assertIn("autotrade_runtime/strict_json.py", paths)
        self.assertIn("autotrade_runtime/artifacts/store.py", paths)
        self.assertIn("autotrade_runtime/artifacts/_root_authority.py", paths)
        self.assertNotIn(
            "research/autotrade_research/artifacts/content_store.py",
            paths,
        )
        self.assertFalse(any(path.startswith("research/") for path in paths))

    def test_exact_staged_runtime_supports_sealed_reader_recovery_and_tcb_pinning(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging, _composition = self._stage(root)
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
    if (
        name == "autotrade_foundation"
        or name.startswith("autotrade_foundation.")
        or name == "autotrade_runtime"
        or name.startswith("autotrade_runtime.")
    ):
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
assert not hasattr(reader, "__dict__")
assert getattr(reader, "__closure__", None) is None
trusted_manifest, trusted_payload = reader(artifact_id)
assert trusted_payload == payload
assert trusted_manifest["artifact_id"] == artifact_id

audit = store.audit()
assert audit.manifests == 1
assert audit.missing_objects == ()
assert audit.corrupt_objects == ()
recovered = store.recover_orphans()
assert recovered.manifests == 1
assert recovered.missing_objects == ()
assert recovered.corrupt_objects == ()

# The issued reader must not take later authority from caller-owned store fields.
store.root = root / "caller-selected-root"
store.objects = store.root / "objects" / "sha256"
store.manifests = store.root / "manifests"
store.staging = store.root / "staging"
store.lock_path = store.root / ".artifact-store.lock"

# The selected authenticated implementation and constructor-free execution view
# must remain pinned after issuance.
original_init = ArtifactStore.__init__
original_public_read = ArtifactStore.read_authenticated_snapshot

def poisoned_init(*args, **kwargs):
    raise AssertionError("trusted read called ArtifactStore constructor")

def poisoned_public_read(*args, **kwargs):
    raise AssertionError("trusted read followed mutable public class method")

ArtifactStore.__init__ = poisoned_init
ArtifactStore.read_authenticated_snapshot = poisoned_public_read
try:
    pinned_manifest, pinned_payload = reader(artifact_id)
finally:
    ArtifactStore.__init__ = original_init
    ArtifactStore.read_authenticated_snapshot = original_public_read

assert pinned_payload == payload
assert pinned_manifest["sha256"] == manifest["sha256"]
assert not (root / "caller-selected-root").exists()
assert not any(name == "research" or name.startswith("research.") for name in sys.modules)
assert not any(name == "autotrade_research" or name.startswith("autotrade_research.") for name in sys.modules)
print("NEUTRAL_ARTIFACT_RUNTIME_OK")
"""
            completed = subprocess.run(
                (sys.executable, "-I", "-S", "-c", script),
                cwd=staging,
                env=_isolated_environment(
                    staging=staging,
                    artifact_root=artifact_root,
                ),
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

    def test_inverse_staging_missing_required_runtime_fails_before_artifact_mutation(self):
        script = r"""
import os
from pathlib import Path
import sys

assert sys.flags.isolated == 1 and sys.flags.no_site == 1
staging = Path(os.environ["AUTOTRADE_STAGING"]).resolve(strict=True)
sys.path.insert(0, str(staging))
import autotrade_runtime.artifacts
raise AssertionError("incomplete staged runtime imported successfully")
"""
        for missing in _INVERSE_REQUIRED:
            with self.subTest(missing=missing), TemporaryDirectory() as directory:
                root = Path(directory)
                staging, _composition = self._stage(root)
                target = staging.joinpath(*missing.split("/"))
                self.assertTrue(target.is_file(), missing)
                target.unlink()
                artifact_root = root / "artifact-store"
                completed = subprocess.run(
                    (sys.executable, "-I", "-S", "-c", script),
                    cwd=staging,
                    env=_isolated_environment(
                        staging=staging,
                        artifact_root=artifact_root,
                    ),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertNotEqual(
                    completed.returncode,
                    0,
                    f"{missing} unexpectedly admitted an incomplete runtime",
                )
                self.assertFalse(
                    artifact_root.exists(),
                    f"{missing} failure mutated artifact authority before import closure",
                )


if __name__ == "__main__":
    unittest.main()
