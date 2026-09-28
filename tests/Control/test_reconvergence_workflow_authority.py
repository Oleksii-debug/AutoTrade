from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from control.tools.reconvergence_integrity import (
    BOOTSTRAP_TRUST_ROOTS,
    Change,
    EXECUTABLE_BOOTSTRAP_ROOTS,
    INTEGRATION_HARNESS_ROOTS,
    TRUSTED_SCOPE_APPROVAL_MARKER,
    WORKFLOW_AUTHORITY_ROOTS,
    assess_reconvergence,
    parse_trusted_scope_approval,
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
            "Directory.Build.props",
            "Directory.Build.targets",
            "global.json",
            "requirements-dev.txt",
            "tools/baseline.py",
            "tools/build_provenance_manifest.py",
            "tools/check_nvda_qualification.py",
            "tools/verify.py",
            "tools/write_ci_evidence.py",
            "tests/Contracts.DotNet/Contracts.DotNet.csproj",
            "tests/Contracts.DotNet/Program.cs",
            "tests/Desktop.Client/Desktop.Client.csproj",
            "tests/Desktop.Client/Program.cs",
            "contracts/fixtures/common-scalars.corpus.json",
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

    def test_bootstrap_trust_roots_are_exact_and_fail_closed(self):
        expected = {
            "control/__init__.py",
            "control/tools/__init__.py",
            "control/tools/reconvergence_integrity.py",
            "control/tools/registry_state.py",
        }
        self.assertEqual(BOOTSTRAP_TRUST_ROOTS, expected)

        for path in (
            "control/tools/__init__.py",
            "control/tools/registry_state.py",
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

        created = assess_reconvergence(
            base_paths=["control/tools/registry_state.py", "owned/change.py"],
            changes=[Change(status="A", path="control/__init__.py")],
        )
        self.assertFalse(created.allowed)
        self.assertEqual(
            created.protected_violations,
            ("control/__init__.py (unauthorized bootstrap trust-root creation)",),
        )

    def test_exact_scope_can_authorize_bootstrap_trust_root_evolution(self):
        path = "control/__init__.py"
        result = assess_reconvergence(
            base_paths=["control/tools/registry_state.py", "owned/change.py"],
            changes=[Change(status="A", path=path)],
            allowed_scopes=(path,),
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.protected_violations, ())
        self.assertEqual(result.scope_violations, ())

    def test_fixed_verifier_authorities_cannot_be_deleted_or_renamed(self):
        for path in (
            "tests/Contracts.DotNet/Program.cs",
            "tests/Desktop.Client/Program.cs",
            "contracts/fixtures/common-scalars.corpus.json",
        ):
            with self.subTest(path=path):
                deleted = assess_reconvergence(
                    base_paths=[path, "owned/change.py"],
                    changes=[Change(status="D", path=path)],
                )
                self.assertFalse(deleted.allowed)
                self.assertIn(path, deleted.protected_deletions)

                renamed = assess_reconvergence(
                    base_paths=[path, "owned/change.py"],
                    changes=[
                        Change(
                            status="R100",
                            previous_path=path,
                            path=f"{path}.old",
                        )
                    ],
                )
                self.assertFalse(renamed.allowed)
                self.assertTrue(
                    any(path in item for item in renamed.protected_violations)
                )

    def test_executable_bootstrap_roots_require_exact_authority(self):
        expected = {
            "control/__init__.py",
            "control/tools/__init__.py",
            "control/tools/reconvergence_integrity.py",
            "control/tools/registry_state.py",
        }
        self.assertEqual(EXECUTABLE_BOOTSTRAP_ROOTS, expected)

        for path in sorted(expected - {"control/__init__.py"}):
            with self.subTest(path=path):
                result = assess_reconvergence(
                    base_paths=[path, "owned/change.py"],
                    changes=[Change(status="M", path=path)],
                )
                self.assertFalse(result.allowed)
                self.assertIn(
                    f"{path} (unauthorized trust-root modification)",
                    result.protected_violations,
                )

        created = assess_reconvergence(
            base_paths=["control/tools/__init__.py", "owned/change.py"],
            changes=[Change(status="A", path="control/__init__.py")],
        )
        self.assertFalse(created.allowed)
        self.assertIn(
            "control/__init__.py (unauthorized trust-root creation)",
            created.protected_violations,
        )

    def test_candidate_cannot_seed_dotnet_build_authority(self):
        created = assess_reconvergence(
            base_paths=["Directory.Build.props", "global.json", "owned/change.py"],
            changes=[Change(status="A", path="Directory.Build.targets")],
        )
        self.assertFalse(created.allowed)
        self.assertIn(
            "Directory.Build.targets (unauthorized trust-root creation)",
            created.protected_violations,
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

    def test_trusted_scope_approval_is_exact_head_bound_and_stale_record_loses_authority(self):
        head = "a" * 40
        body = "\n".join(
            (
                TRUSTED_SCOPE_APPROVAL_MARKER,
                f"head: {head}",
                "path: .github/workflows/verify.yml",
                "path: tools/verify.py",
            )
        )

        self.assertEqual(
            parse_trusted_scope_approval(body, expected_head_sha=head),
            (".github/workflows/verify.yml", "tools/verify.py"),
        )
        self.assertIsNone(
            parse_trusted_scope_approval(body, expected_head_sha="b" * 40)
        )
        self.assertIsNone(
            parse_trusted_scope_approval(
                "ordinary review comment",
                expected_head_sha=head,
            )
        )

    def test_current_head_scope_approval_malformed_records_fail_closed(self):
        head = "a" * 40
        malformed = (
            TRUSTED_SCOPE_APPROVAL_MARKER,
            "\n".join((TRUSTED_SCOPE_APPROVAL_MARKER, f"head: {head}")),
            "\n".join(
                (
                    TRUSTED_SCOPE_APPROVAL_MARKER,
                    f"head: {head}",
                    "path: tools/verify.py",
                    "path: tools/verify.py",
                )
            ),
            "\n".join(
                (
                    TRUSTED_SCOPE_APPROVAL_MARKER,
                    f"head: {head}",
                    "not-path: tools/verify.py",
                )
            ),
        )

        for body in malformed:
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    parse_trusted_scope_approval(body, expected_head_sha=head)

    def test_scope_approval_cannot_silently_cover_unlisted_candidate_change(self):
        protected = ".github/workflows/verify.yml"
        unlisted = "owned/change.py"
        result = assess_reconvergence(
            base_paths=[protected, unlisted],
            changes=[
                Change(status="M", path=protected),
                Change(status="M", path=unlisted),
            ],
            allowed_scopes=(protected,),
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.protected_violations, ())
        self.assertEqual(result.scope_violations, (unlisted,))

    def test_trusted_workflow_resolves_only_owner_exact_head_comment_scopes(self):
        workflow = (
            REPO_ROOT / ".github" / "workflows" / "reconvergence-integrity.yml"
        ).read_text(encoding="utf-8")
        resolver = workflow.split(
            "- name: Resolve trusted exact-path scope approval",
            1,
        )[1].split("- name: Guard pull-request repository tree", 1)[0]
        guard = workflow.split(
            "- name: Guard pull-request repository tree with trusted base code",
            1,
        )[1].split("- name: Run trusted guard regression tests", 1)[0]

        self.assertIn("issues: read", workflow)
        self.assertIn('comment.get("author_association") != "OWNER"', resolver)
        self.assertIn("parse_trusted_scope_approval", resolver)
        self.assertIn("expected_head_sha=head_sha", resolver)
        self.assertIn("/issues/{pr_number}/comments", resolver)
        self.assertIn('args+=(--allowed-scope "${scope}")', guard)
        self.assertNotIn("pull_request.body", resolver)
        self.assertNotIn("pull_request.title", resolver)

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


    def test_public_entrypoint_rejects_candidate_bootstrap_rewrite(self):
        with TemporaryDirectory() as directory:
            git_root = Path(directory)
            registry = git_root / "control" / "tools" / "registry_state.py"
            package_init = git_root / "control" / "tools" / "__init__.py"
            registry.parent.mkdir(parents=True)
            registry.write_text("VALUE = 1\n", encoding="utf-8")
            package_init.write_text("", encoding="utf-8")
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
            git("config", "user.email", "bootstrap-authority@example.invalid")
            git("config", "user.name", "Bootstrap Authority Test")
            git("add", ".")
            git("commit", "-m", "base")
            base_sha = git("rev-parse", "HEAD")

            registry.write_text("raise SystemExit(0)\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "seed executable bootstrap")
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
        self.assertIn("control/tools/registry_state.py", completed.stdout)

    def test_public_entrypoint_rejects_fixed_verifier_rewrite(self):
        with TemporaryDirectory() as directory:
            git_root = Path(directory)
            verifier = git_root / "tests" / "Contracts.DotNet" / "Program.cs"
            verifier.parent.mkdir(parents=True)
            verifier.write_text("return 1;\n", encoding="utf-8")
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
            git("config", "user.email", "verifier-authority@example.invalid")
            git("config", "user.name", "Verifier Authority Test")
            git("add", ".")
            git("commit", "-m", "base")
            base_sha = git("rev-parse", "HEAD")

            verifier.write_text("return 0;\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "weaken fixed verifier")
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
        self.assertIn("tests/Contracts.DotNet/Program.cs", completed.stdout)


    def test_unqualified_nvda_status_check_does_not_import_mutable_product_packages(self):
        source = (REPO_ROOT / "tools" / "check_nvda_qualification.py").read_text(
            encoding="utf-8"
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tool = root / "tools" / "check_nvda_qualification.py"
            tool.parent.mkdir(parents=True)
            tool.write_text(source, encoding="utf-8")

            qualification = root / "qualification" / "nvda"
            qualification.mkdir(parents=True)
            (qualification / "requirements.json").write_text("{}\n", encoding="utf-8")
            (qualification / "status.json").write_text(
                '{"qualified": false, "reason": "NO_REAL_NVDA_RELEASE_EVIDENCE"}\n',
                encoding="utf-8",
            )

            marker = root / "mutable-package-imported.txt"
            for package_init in (
                root / "mvp" / "autotrade_mvp" / "__init__.py",
                root / "research" / "autotrade_research" / "artifacts" / "__init__.py",
            ):
                package_init.parent.mkdir(parents=True, exist_ok=True)
                package_init.write_text(
                    "from pathlib import Path\n"
                    f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n"
                    "raise SystemExit(0)\n",
                    encoding="utf-8",
                )

            completed = subprocess.run(
                [sys.executable, str(tool), "--check-status"],
                cwd=root,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            self.assertFalse(marker.exists())
            self.assertIn('"qualified": false', completed.stdout)


if __name__ == "__main__":
    unittest.main()
