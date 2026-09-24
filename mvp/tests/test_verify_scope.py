from pathlib import Path
import unittest

from tools.verify import PYTHON_TEST_DIRS


ROOT = Path(__file__).resolve().parents[2]


class VerifyScopeTests(unittest.TestCase):
    def test_every_top_level_python_test_directory_is_selected(self):
        discovered = {
            str(path.parent.relative_to(ROOT)).replace("\\", "/")
            for path in (ROOT / "tests").glob("*/test_*.py")
        }
        configured = set(PYTHON_TEST_DIRS)
        self.assertLessEqual(
            discovered,
            configured,
            "tools/verify.py omitted a top-level Python test directory",
        )
        self.assertIn("research/tests", configured)
        self.assertIn("mvp/tests", configured)
        self.assertIn("tests/Observability", configured)


if __name__ == "__main__":
    unittest.main()
