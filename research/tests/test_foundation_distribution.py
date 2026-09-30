from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class FoundationDistributionTests(unittest.TestCase):
    def test_editable_research_install_exposes_neutral_foundation_off_checkout(self):
        with TemporaryDirectory() as directory:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    (
                        "from autotrade_foundation.local_filesystem "
                        "import require_qualified_local_filesystem_path; "
                        "from autotrade_research.artifacts.resource_lock "
                        "import ResourceLock; "
                        "assert callable(require_qualified_local_filesystem_path); "
                        "assert ResourceLock is not None"
                    ),
                ],
                cwd=Path(directory),
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
