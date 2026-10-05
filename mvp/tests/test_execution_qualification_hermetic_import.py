from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ExecutionQualificationHermeticImportTests(unittest.TestCase):
    def test_execution_qualification_imports_without_research_package(self):
        script = r'''
import importlib.abc
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))


class BlockResearch(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "research" or fullname.startswith("research."):
            raise ImportError("research package is unavailable in installed runtime")
        return None


preloaded = sorted(
    name
    for name in sys.modules
    if name == "research" or name.startswith("research.")
)
if preloaded:
    raise AssertionError(f"research package preloaded: {preloaded}")

sys.meta_path.insert(0, BlockResearch())
import autotrade_runtime.artifacts as runtime_artifacts
import mvp.autotrade_mvp.instruments as instruments
import mvp.autotrade_mvp.execution_qualification as execution_qualification

if instruments.ArtifactStore is not runtime_artifacts.ArtifactStore:
    raise AssertionError("instrument registry does not use neutral ArtifactStore")
if execution_qualification.ArtifactStore is not runtime_artifacts.ArtifactStore:
    raise AssertionError("execution qualification does not use neutral ArtifactStore")

leaked = sorted(
    name
    for name in sys.modules
    if name == "research" or name.startswith("research.")
)
if leaked:
    raise AssertionError(f"execution qualification imported research package: {leaked}")
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
