from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools import verify


ROOT = Path(__file__).resolve().parents[2]


class VerifyScopeTests(unittest.TestCase):
    def test_every_repository_python_test_file_parent_is_selected(self):
        discovered = {
            path.parent.relative_to(ROOT).as_posix()
            for path in (ROOT / "tests").rglob("test_*.py")
            if path.is_file()
        }
        configured = set(verify.PYTHON_TEST_DIRS)
        self.assertLessEqual(
            discovered,
            configured,
            "tools/verify.py omitted a repository Python test directory",
        )
        self.assertIn("research/tests", configured)
        self.assertIn("mvp/tests", configured)
        self.assertIn("tests/Observability", configured)

    def test_dynamic_discovery_covers_root_and_nested_test_files(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            root_test = root / "tests" / "test_root.py"
            root_test.parent.mkdir(parents=True)
            root_test.write_text("import unittest\n", encoding="utf-8")
            nested = root / "tests" / "deep" / "nested" / "test_nested.py"
            nested.parent.mkdir(parents=True)
            nested.write_text("import unittest\n", encoding="utf-8")
            self.assertEqual(
                verify.repository_python_test_dirs(root),
                ("tests", "tests/deep/nested"),
            )

    def test_selected_suite_directories_exist_and_are_unique(self):
        configured = verify.PYTHON_TEST_DIRS
        self.assertEqual(len(configured), len(set(configured)))
        missing = [
            suite
            for suite in configured
            if not (verify.ROOT / suite).is_dir()
        ]
        self.assertEqual(missing, [])

    def test_verification_commands_run_every_selected_suite(self):
        commands = verify.verification_commands()
        selected = {
            command[command.index("-s") + 1]
            for command in commands
            if "-s" in command
        }
        self.assertEqual(selected, set(verify.PYTHON_TEST_DIRS))

    def test_non_python_integration_has_dedicated_exact_source_workflow(self):
        workflow = verify.ROOT / ".github" / "workflows" / "lean-adoption.yml"
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("AUTOTRADE_SOURCE_SHA", text)
        self.assertIn("tests/Integration/LeanAdoption", text)
        self.assertIn("ref: ${{ env.AUTOTRADE_SOURCE_SHA }}", text)


if __name__ == "__main__":
    unittest.main()
