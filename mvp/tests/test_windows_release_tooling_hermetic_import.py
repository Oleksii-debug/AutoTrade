from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]


class WindowsReleaseToolingHermeticImportTests(unittest.TestCase):
    def test_release_tooling_imports_without_research_packages(self):
        script = r'''
import importlib.abc
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))


class BlockResearch(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (
            fullname == "research"
            or fullname.startswith("research.")
            or fullname == "autotrade_research"
            or fullname.startswith("autotrade_research.")
        ):
            raise ImportError("development research package is unavailable")
        return None


preloaded = sorted(
    name
    for name in sys.modules
    if (
        name == "research"
        or name.startswith("research.")
        or name == "autotrade_research"
        or name.startswith("autotrade_research.")
    )
)
if preloaded:
    raise AssertionError(f"research package preloaded: {preloaded}")

sys.meta_path.insert(0, BlockResearch())

import autotrade_runtime.artifacts as runtime_artifacts
import autotrade_runtime.artifacts.durable_publish as runtime_publish
import mvp.autotrade_mvp.qualification_attestation as qualification_attestation
import tools.build_windows_bundle as windows_bundle
import tools.build_windows_install_manifest as install_manifest

if qualification_attestation.ArtifactStore is not runtime_artifacts.ArtifactStore:
    raise AssertionError("qualification attestation does not use neutral ArtifactStore")
if windows_bundle.atomic_write_stream_with_sha256_sidecar is not runtime_publish.atomic_write_stream_with_sha256_sidecar:
    raise AssertionError("Windows bundle does not use neutral durable publication")
if install_manifest.atomic_write_bytes_with_sha256_sidecar is not runtime_publish.atomic_write_bytes_with_sha256_sidecar:
    raise AssertionError("installer manifest does not use neutral durable publication")

leaked = sorted(
    name
    for name in sys.modules
    if (
        name == "research"
        or name.startswith("research.")
        or name == "autotrade_research"
        or name.startswith("autotrade_research.")
    )
)
if leaked:
    raise AssertionError(f"Windows release tooling imported research package: {leaked}")
'''
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", script, str(ROOT)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
