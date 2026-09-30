from pathlib import Path
import os
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]


def _isolated_import(*, installed: Path, script: str):
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["AUTOTRADE_INSTALLED_SITE"] = str(installed)
    return subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=installed.parent,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


class FoundationDistributionTests(unittest.TestCase):
    def test_research_distribution_installs_shared_foundation_off_checkout(self):
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
                env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                install.returncode,
                0,
                msg=install.stdout + install.stderr,
            )

            script = """
import os
import sys
site = os.environ['AUTOTRADE_INSTALLED_SITE']
sys.path.insert(0, site)
from autotrade_foundation.local_filesystem import require_qualified_local_filesystem_path
from autotrade_research.artifacts.resource_lock import ResourceLock
assert callable(require_qualified_local_filesystem_path)
assert ResourceLock is not None
for name in sys.modules:
    if name.startswith('autotrade_foundation') or name.startswith('autotrade_research'):
        module = sys.modules[name]
        filename = getattr(module, '__file__', None)
        if filename is not None:
            assert str(filename).startswith(site), (name, filename)
print('RESEARCH_DISTRIBUTION_OK')
"""
            completed = _isolated_import(installed=installed, script=script)
            self.assertEqual(
                completed.returncode,
                0,
                msg=completed.stdout + completed.stderr,
            )
            self.assertEqual(completed.stdout.strip(), "RESEARCH_DISTRIBUTION_OK")

            missing = root / "site-without-foundation"
            shutil.copytree(installed, missing)
            shutil.rmtree(missing / "autotrade_foundation")
            inverse = _isolated_import(
                installed=missing,
                script="""
import os
import sys
sys.path.insert(0, os.environ['AUTOTRADE_INSTALLED_SITE'])
from autotrade_research.artifacts.resource_lock import ResourceLock
raise AssertionError(ResourceLock)
""",
            )
            self.assertNotEqual(inverse.returncode, 0)
            self.assertIn("autotrade_foundation", inverse.stderr)


if __name__ == "__main__":
    unittest.main()
