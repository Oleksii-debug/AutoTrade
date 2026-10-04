from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]


class WindowsReleaseToolingHermeticImportTests(unittest.TestCase):
    def test_release_and_qualification_tooling_import_without_research_packages(self):
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


def is_research_name(name):
    return (
        name == "research"
        or name.startswith("research.")
        or name == "autotrade_research"
        or name.startswith("autotrade_research.")
    )


preloaded = sorted(name for name in sys.modules if is_research_name(name))
if preloaded:
    raise AssertionError(f"research package preloaded: {preloaded}")

sys.meta_path.insert(0, BlockResearch())

import autotrade_runtime.artifacts as runtime_artifacts
import autotrade_runtime.artifacts.durable_publish as runtime_publish
import mvp.autotrade_mvp.qualification_attestation as qualification_attestation
import mvp.autotrade_mvp.recovery_qualification as recovery_qualification
import mvp.autotrade_mvp.release_candidate as release_candidate
import mvp.autotrade_mvp.science_qualification as science_qualification
import mvp.autotrade_mvp.supply_chain_qualification as supply_chain_qualification
import mvp.autotrade_mvp.windows_update as windows_update
import tools.build_windows_bundle as windows_bundle
import tools.build_windows_install_manifest as install_manifest
import tools.check_nvda_qualification as nvda_qualification

artifact_users = (
    qualification_attestation,
    recovery_qualification,
    release_candidate,
    science_qualification,
    supply_chain_qualification,
    windows_update,
    nvda_qualification,
)
for module in artifact_users:
    store = getattr(module, "ArtifactStore", None)
    if store is not runtime_artifacts.ArtifactStore:
        raise AssertionError(
            f"{module.__name__} does not use neutral ArtifactStore"
        )

if windows_bundle.atomic_write_stream_with_sha256_sidecar is not runtime_publish.atomic_write_stream_with_sha256_sidecar:
    raise AssertionError("Windows bundle does not use neutral durable publication")
if install_manifest.atomic_write_bytes_with_sha256_sidecar is not runtime_publish.atomic_write_bytes_with_sha256_sidecar:
    raise AssertionError("installer manifest does not use neutral durable publication")

leaked = sorted(name for name in sys.modules if is_research_name(name))
if leaked:
    raise AssertionError(
        f"release/qualification tooling imported research package: {leaked}"
    )
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
