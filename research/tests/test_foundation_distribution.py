from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]


class FoundationDistributionTests(unittest.TestCase):
    def test_research_distribution_installs_foundation_off_checkout(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            installed = root / "site"
            install = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-deps",
                    "--no-build-isolation",
                    "--target",
                    str(installed),
                    str(ROOT / "research"),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                install.returncode,
                0,
                msg=install.stdout + install.stderr,
            )

            script = (
                "import sys\n"
                f"sys.path.insert(0, {str(installed)!r})\n"
                "from autotrade_foundation.local_filesystem "
                "import require_qualified_local_filesystem_path\n"
                "from autotrade_research.artifacts.resource_lock import ResourceLock\n"
                "assert callable(require_qualified_local_filesystem_path)\n"
                "assert ResourceLock is not None\n"
            )
            completed = subprocess.run(
                [sys.executable, "-I", "-c", script],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=completed.stdout + completed.stderr,
            )


if __name__ == "__main__":
    unittest.main()
