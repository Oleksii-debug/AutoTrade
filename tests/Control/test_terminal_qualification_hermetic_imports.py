from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]


class TerminalQualificationHermeticImportTests(unittest.TestCase):
    def test_terminal_qualification_surfaces_import_without_research_package(self):
        script = r'''
import importlib.abc
from pathlib import Path
import sys


root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))


BLOCKED_RESEARCH_PREFIXES = ("research", "autotrade_research")


def is_blocked_research_import(name):
    return any(
        name == prefix or name.startswith(prefix + ".")
        for prefix in BLOCKED_RESEARCH_PREFIXES
    )


class BlockResearch(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if is_blocked_research_import(fullname):
            raise ImportError("research package is unavailable in installed runtime")
        return None


preloaded = sorted(
    name
    for name in sys.modules
    if is_blocked_research_import(name)
)
if preloaded:
    raise AssertionError(f"research package preloaded: {preloaded}")

sys.meta_path.insert(0, BlockResearch())
import autotrade_runtime.artifacts as runtime_artifacts
import mvp.autotrade_mvp.bounded_real
import mvp.autotrade_mvp.execution_qualification
import mvp.autotrade_mvp.instruments as instruments
import mvp.autotrade_mvp.qualification_attestation as qualification_attestation
import mvp.autotrade_mvp.recovery_qualification
import mvp.autotrade_mvp.release_qualification
import mvp.autotrade_mvp.supply_chain_qualification
import tools.check_nvda_qualification
import tools.check_product_completion

if qualification_attestation.ArtifactStore is not runtime_artifacts.ArtifactStore:
    raise AssertionError("qualification verifier does not use neutral ArtifactStore")
if instruments.ArtifactStore is not runtime_artifacts.ArtifactStore:
    raise AssertionError("instrument registry does not use neutral ArtifactStore")
if tools.check_nvda_qualification.ArtifactStore is not runtime_artifacts.ArtifactStore:
    raise AssertionError("NVDA verifier does not use neutral ArtifactStore")
if tools.check_product_completion.ArtifactStore is not runtime_artifacts.ArtifactStore:
    raise AssertionError("completion verifier does not use neutral ArtifactStore")

leaked = sorted(
    name
    for name in sys.modules
    if is_blocked_research_import(name)
)
if leaked:
    raise AssertionError(f"terminal qualification imported research package: {leaked}")
'''
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", script, str(ROOT)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
