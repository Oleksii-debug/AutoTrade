from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]


class InstalledRuntimeLocalFilesystemClosureTests(unittest.TestCase):
    def test_persistence_import_does_not_require_research_source_tree(self):
        with TemporaryDirectory() as directory:
            staging = Path(directory)
            shutil.copytree(
                ROOT / "autotrade_foundation",
                staging / "autotrade_foundation",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            (staging / "mvp").mkdir()
            shutil.copy2(ROOT / "mvp" / "__init__.py", staging / "mvp" / "__init__.py")
            shutil.copytree(
                ROOT / "mvp" / "autotrade_mvp",
                staging / "mvp" / "autotrade_mvp",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

            script = (
                "import sys\n"
                f"sys.path.insert(0, {str(staging)!r})\n"
                "from mvp.autotrade_mvp.persistence import JournalStore\n"
                "import autotrade_foundation.local_filesystem\n"
                "assert JournalStore is not None\n"
                "assert not any(name == 'research' or name.startswith('research.') "
                "or name == 'autotrade_research' or name.startswith('autotrade_research.') "
                "for name in sys.modules)\n"
            )
            completed = subprocess.run(
                [sys.executable, "-I", "-c", script],
                cwd=staging,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=completed.stdout + completed.stderr,
            )

    def test_research_lock_import_uses_packaged_foundation_without_repo_root(self):
        with TemporaryDirectory() as directory:
            staging = Path(directory)
            shutil.copytree(
                ROOT / "autotrade_foundation",
                staging / "autotrade_foundation",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            shutil.copytree(
                ROOT / "research" / "autotrade_research",
                staging / "autotrade_research",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            script = (
                "import sys\n"
                f"sys.path.insert(0, {str(staging)!r})\n"
                "from autotrade_research.artifacts.resource_lock import ResourceLock\n"
                "import autotrade_foundation.local_filesystem\n"
                "assert ResourceLock is not None\n"
                "assert not any(name == 'mvp' or name.startswith('mvp.') "
                "for name in sys.modules)\n"
            )
            completed = subprocess.run(
                [sys.executable, "-I", "-c", script],
                cwd=staging,
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
