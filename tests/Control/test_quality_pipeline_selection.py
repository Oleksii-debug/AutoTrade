from pathlib import Path
import unittest

from tools import verify


class QualityPipelineSelectionTests(unittest.TestCase):
    def test_required_python_suites_include_observability(self):
        self.assertIn("tests/Observability", verify.PYTHON_TEST_SUITES)
        self.assertEqual(len(verify.PYTHON_TEST_SUITES), len(set(verify.PYTHON_TEST_SUITES)))

    def test_selected_suite_directories_exist(self):
        missing = [
            suite
            for suite in verify.PYTHON_TEST_SUITES
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
        self.assertEqual(selected, set(verify.PYTHON_TEST_SUITES))

    def test_non_python_integration_has_dedicated_exact_source_workflow(self):
        workflow = verify.ROOT / ".github" / "workflows" / "lean-adoption.yml"
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("AUTOTRADE_SOURCE_SHA", text)
        self.assertIn("tests/Integration/LeanAdoption", text)
        self.assertIn("ref: ${{ env.AUTOTRADE_SOURCE_SHA }}", text)


if __name__ == "__main__":
    unittest.main()
