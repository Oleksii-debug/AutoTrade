from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from control.tools.reconvergence_integrity import (
    Change,
    INTEGRATION_HARNESS_ROOTS,
    WORKFLOW_AUTHORITY_ROOTS,
    assess_reconvergence,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


class ReconvergenceWorkflowAuthorityTests(unittest.TestCase):
    def test_every_checked_in_workflow_requires_exact_modification_authority(self):
        workflow_dir = REPO_ROOT / ".github" / "workflows"
        workflows = {
            path.relative_to(REPO_ROOT).as_posix()
            for path in workflow_dir.glob("*.y*ml")
            if path.is_file()
        }

        self.assertGreaterEqual(len(workflows), 10)
        self.assertEqual(workflows - WORKFLOW_AUTHORITY_ROOTS, set())

    def test_untrusted_modification_of_integration_workflow_authorities_fails_closed(self):
        for path in (
            ".github/workflows/baseline.yml",
            ".github/workflows/control-plane.yml",
            ".github/workflows/verify.yml",
        ):
            with self.subTest(path=path):
                result = assess_reconvergence(
                    base_paths=[path, "owned/change.py"],
                    changes=[Change(status="M", path=path)],
                )

                self.assertFalse(result.allowed)
                self.assertEqual(
                    result.protected_violations,
                    (f"{path} (unauthorized trust-root modification)",),
                )

    def test_untrusted_modification_of_integration_harness_roots_fails_closed(self):
        expected = {
            "requirements-dev.txt",
            "tools/baseline.py",
            "tools/build_provenance_manifest.py",
            "tools/check_nvda_qualification.py",
            "tools/verify.py",
            "tools/write_ci_evidence.py",
        }
        self.assertEqual(INTEGRATION_HARNESS_ROOTS, expected)

        for path in sorted(expected):
            with self.subTest(path=path):
                result = assess_reconvergence(
                    base_paths=[path, "owned/change.py"],
                    changes=[Change(status="M", path=path)],
                )

                self.assertFalse(result.allowed)
                self.assertEqual(
                    result.protected_violations,
                    (f"{path} (unauthorized trust-root modification)",),
                )

    def test_new_workflow_cannot_spoof_required_check_authority(self):
        path = ".github/workflows/spoof-verify.yml"
        result = assess_reconvergence(
            base_paths=[".github/workflows/verify.yml", "owned/change.py"],
            changes=[Change(status="A", path=path)],
        )

        self.assertFalse(result.allowed)
        self.assertEqual(
            result.protected_violations,
            (f"{path} (unauthorized workflow-authority creation)",),
        )

    def test_copy_or_rename_into_new_workflow_authority_requires_exact_scope(self):
        for change in (
            Change(
                status="C100",
                previous_path="templates/check.yml",
                path=".github/workflows/spoof-copy.yml",
            ),
            Change(
                status="R100",
                previous_path="templates/check.yml",
                path=".github/workflows/spoof-rename.yml",
            ),
        ):
            with self.subTest(status=change.status):
                result = assess_reconvergence(
                    base_paths=["templates/check.yml", ".github/workflows/verify.yml"],
                    changes=[change],
                )
                self.assertFalse(result.allowed)
                self.assertTrue(
                    any(
                        "unauthorized workflow-authority creation" in item
                        for item in result.protected_violations
                    )
                )

    def test_directory_scope_cannot_authorize_workflow_authority_modification(self):
        path = ".github/workflows/verify.yml"
        result = assess_reconvergence(
            base_paths=[path, "owned/change.py"],
            changes=[Change(status="M", path=path)],
            allowed_scopes=(".github/workflows",),
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.scope_violations, ())
        self.assertEqual(
            result.protected_violations,
            (f"{path} (unauthorized trust-root modification)",),
        )

    def test_directory_scope_cannot_authorize_integration_harness_modification(self):
        path = "tools/verify.py"
        result = assess_reconvergence(
            base_paths=[path, "owned/change.py"],
            changes=[Change(status="M", path=path)],
            allowed_scopes=("tools",),
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.scope_violations, ())
        self.assertEqual(
            result.protected_violations,
            (f"{path} (unauthorized trust-root modification)",),
        )

    def test_exact_trusted_scope_can_authorize_workflow_authority_modification(self):
        path = ".github/workflows/verify.yml"
        result = assess_reconvergence(
            base_paths=[path, "owned/change.py"],
            changes=[Change(status="M", path=path)],
            allowed_scopes=(path,),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.protected_violations, ())
        self.assertEqual(result.scope_violations, ())

    def test_exact_trusted_scope_can_authorize_integration_harness_modification(self):
        path = "tools/verify.py"
        result = assess_reconvergence(
            base_paths=[path, "owned/change.py"],
            changes=[Change(status="M", path=path)],
            allowed_scopes=(path,),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.protected_violations, ())
        self.assertEqual(result.scope_violations, ())

    def test_exact_trusted_scope_can_authorize_new_workflow_authority(self):
        path = ".github/workflows/new-authorized.yml"
        result = assess_reconvergence(
            base_paths=[".github/workflows/verify.yml", "owned/change.py"],
            changes=[Change(status="A", path=path)],
            allowed_scopes=(path,),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.protected_violations, ())
        self.assertEqual(result.scope_violations, ())

    def test_unrelated_owned_path_remains_admissible(self):
        path = "mvp/autotrade_mvp/example.py"
        result = assess_reconvergence(
            base_paths=[path, ".github/workflows/verify.yml"],
            changes=[Change(status="M", path=path)],
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.protected_violations, ())

    def test_public_entrypoint_rejects_candidate_rewrite_of_verify_workflow(self):
        with TemporaryDirectory() as directory:
            git_root = Path(directory)
            workflow = git_root / ".github" / "workflows" / "verify.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: Verify AutoTrade\n", encoding="utf-8")
            (git_root / "README.md").write_text("base\n", encoding="utf-8")

            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", *args],
                    cwd=git_root,
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ).stdout.strip()

            git("init")
            git("config", "user.email", "workflow-authority@example.invalid")
            git("config", "user.name", "Workflow Authority Test")
            git("add", ".")
            git("commit", "-m", "base")
            base_sha = git("rev-parse", "HEAD")

            workflow.write_text(
                "name: Verify AutoTrade\njobs: {verify: {runs-on: ubuntu-latest}}\n",
                encoding="utf-8",
            )
            git("add", ".")
            git("commit", "-m", "weaken verify authority")
            head_sha = git("rev-parse", "HEAD")

            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env["GIT_DIR"] = str(git_root / ".git")
            env["GIT_WORK_TREE"] = str(git_root)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "control.tools.reconvergence_integrity",
                    "--base",
                    base_sha,
                    "--head",
                    head_sha,
                ],
                cwd=REPO_ROOT,
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        self.assertIn("unauthorized trust-root modification", completed.stdout)
        self.assertIn(".github/workflows/verify.yml", completed.stdout)

    def test_public_entrypoint_rejects_candidate_rewrite_of_verify_harness(self):
        with TemporaryDirectory() as directory:
            git_root = Path(directory)
            verify = git_root / "tools" / "verify.py"
            verify.parent.mkdir(parents=True)
            verify.write_text("raise SystemExit(1)\n", encoding="utf-8")
            (git_root / "README.md").write_text("base\n", encoding="utf-8")

            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", *args],
                    cwd=git_root,
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ).stdout.strip()

            git("init")
            git("config", "user.email", "harness-authority@example.invalid")
            git("config", "user.name", "Harness Authority Test")
            git("add", ".")
            git("commit", "-m", "base")
            base_sha = git("rev-parse", "HEAD")

            verify.write_text("raise SystemExit(0)\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "weaken verify harness")
            head_sha = git("rev-parse", "HEAD")

            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env["GIT_DIR"] = str(git_root / ".git")
            env["GIT_WORK_TREE"] = str(git_root)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "control.tools.reconvergence_integrity",
                    "--base",
                    base_sha,
                    "--head",
                    head_sha,
                ],
                cwd=REPO_ROOT,
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        self.assertIn("unauthorized trust-root modification", completed.stdout)
        self.assertIn("tools/verify.py", completed.stdout)


if __name__ == "__main__":
    unittest.main()
