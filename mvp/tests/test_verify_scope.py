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

    def test_recovery_qualification_has_dedicated_exact_head_workflow(self):
        workflow = verify.ROOT / ".github" / "workflows" / "recovery-qualification.yml"
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("os: [ubuntu-latest, windows-latest]", text)
        self.assertIn(
            "AUTOTRADE_SOURCE_SHA: ${{ github.event.pull_request.head.sha || github.sha }}",
            text,
        )
        self.assertIn(
            "AUTOTRADE_PR_HEAD_SHA: ${{ github.event.pull_request.head.sha || '' }}",
            text,
        )
        self.assertIn("ref: ${{ env.AUTOTRADE_SOURCE_SHA }}", text)
        focused_command = (
            "python -m unittest "
            "mvp.tests.test_recovery_qualification "
            "mvp.tests.test_recovery_qualification_authority_ingress "
            "mvp.tests.test_qualification_attestation "
            "mvp.tests.test_qualification_attestation_authority_ingress "
            "mvp.tests.test_verify_scope -v"
        )
        normalized_workflow = " ".join(text.split())
        self.assertIn(focused_command, normalized_workflow)
        self.assertIn("tools/write_ci_evidence.py", text)
        self.assertIn("--suite recovery-qualification-foundation", text)

        critical_trigger_paths = (
            "docs/qualification/recovery/WP59_PROTOCOL_EVIDENCE.md",
            "mvp/autotrade_mvp/recovery_qualification.py",
            "mvp/autotrade_mvp/qualification_attestation.py",
            "mvp/autotrade_mvp/qualification_trust_policy.json",
            "mvp/tests/test_recovery_qualification.py",
            "mvp/tests/test_recovery_qualification_authority_ingress.py",
            "mvp/tests/test_qualification_attestation.py",
            "mvp/tests/test_qualification_attestation_authority_ingress.py",
            "mvp/tests/test_verify_scope.py",
            "autotrade_runtime/artifacts/**",
            "research/autotrade_research/artifacts/**",
            "tools/write_ci_evidence.py",
            ".github/workflows/recovery-qualification.yml",
        )
        for path in critical_trigger_paths:
            self.assertGreaterEqual(
                text.count(f'- "{path}"'),
                2,
                f"recovery qualification must rerun on pull_request and push changes to {path}",
            )


if __name__ == "__main__":
    unittest.main()
