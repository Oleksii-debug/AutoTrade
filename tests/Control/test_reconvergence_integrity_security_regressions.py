from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from control.tools.reconvergence_integrity import Change, assess_reconvergence


REPO_ROOT = Path(__file__).resolve().parents[2]


class ReconvergenceSecurityRegressionTests(unittest.TestCase):
    def test_mass_rename_away_counts_as_base_tree_disappearance(self):
        base = [f"path-{index:03d}.txt" for index in range(100)]
        changes = [
            Change(
                status="R100",
                previous_path=path,
                path=f"moved/{path}",
            )
            for path in base[:60]
        ]

        result = assess_reconvergence(
            base_paths=base,
            changes=changes,
            max_deletions=50,
            max_deleted_fraction=0.35,
            protected_sentinels=frozenset(),
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.deletion_count, 60)
        self.assertEqual(result.deletion_fraction, 0.6)
        self.assertTrue(
            any("mass base-tree deletion/rename-away" in reason for reason in result.reasons)
        )

    def test_mass_copy_does_not_count_as_source_disappearance(self):
        base = [f"path-{index:03d}.txt" for index in range(100)]
        changes = [
            Change(
                status="C100",
                previous_path=path,
                path=f"copies/{path}",
            )
            for path in base[:60]
        ]

        result = assess_reconvergence(
            base_paths=base,
            changes=changes,
            max_deletions=50,
            max_deleted_fraction=0.35,
            protected_sentinels=frozenset(),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.deletion_count, 0)
        self.assertEqual(result.deletion_fraction, 0.0)

    def test_public_module_entrypoint_blocks_large_real_git_rename_away(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", *args],
                    cwd=root,
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ).stdout.strip()

            git("init")
            git("config", "user.email", "reconvergence-rename@example.invalid")
            git("config", "user.name", "Reconvergence Rename Test")
            for index in range(100):
                (root / f"path-{index:03d}.txt").write_text(
                    f"unique-{index:03d}\n",
                    encoding="utf-8",
                )
            git("add", ".")
            git("commit", "-m", "base")
            base_sha = git("rev-parse", "HEAD")

            moved = root / "moved"
            moved.mkdir()
            for index in range(60):
                source = root / f"path-{index:03d}.txt"
                source.rename(moved / source.name)
            git("add", "-A")
            git("commit", "-m", "rename away most base paths")
            head_sha = git("rev-parse", "HEAD")

            env = dict(os.environ)
            current_pythonpath = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                str(REPO_ROOT)
                if not current_pythonpath
                else os.pathsep.join((str(REPO_ROOT), current_pythonpath))
            )
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
                cwd=root,
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("mass base-tree deletion/rename-away", completed.stdout)
        self.assertIn("deletions=60", completed.stdout)

    def test_public_module_entrypoint_fails_closed_on_malformed_git_status(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = root / "run_guard_with_git_shim.py"
            bootstrap.write_text(
                "import runpy\n"
                "import subprocess\n"
                "_real_run = subprocess.run\n"
                "def _run(command, *args, **kwargs):\n"
                "    if isinstance(command, (list, tuple)) and command and command[0] == 'git':\n"
                "        argv = list(command[1:])\n"
                "        if argv[:2] == ['merge-base', '--is-ancestor']:\n"
                "            return subprocess.CompletedProcess(command, 0, stdout='', stderr='')\n"
                "        if argv[:3] == ['ls-tree', '-r', '--name-only']:\n"
                "            return subprocess.CompletedProcess(command, 0, stdout='README.md\\n', stderr='')\n"
                "        if argv[:3] == ['diff', '--name-status', '--find-renames']:\n"
                "            return subprocess.CompletedProcess(command, 0, stdout='U\\tREADME.md\\n', stderr='')\n"
                "        raise AssertionError(f'unexpected git command: {command!r}')\n"
                "    return _real_run(command, *args, **kwargs)\n"
                "subprocess.run = _run\n"
                "runpy.run_module('control.tools.reconvergence_integrity', run_name='__main__')\n",
                encoding="utf-8",
            )
            env = dict(os.environ)
            current_pythonpath = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                str(REPO_ROOT)
                if not current_pythonpath
                else os.pathsep.join((str(REPO_ROOT), current_pythonpath))
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(bootstrap),
                    "--base",
                    "base",
                    "--head",
                    "head",
                ],
                cwd=root,
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        combined = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Unsupported or malformed Git name-status", combined)

    def test_diverged_candidate_retains_diagnostic_reason(self):
        result = assess_reconvergence(
            base_paths=["README.md"],
            changes=[],
            protected_sentinels=frozenset(),
            base_is_ancestor=False,
        )

        self.assertFalse(result.allowed)
        self.assertIn("head is not descended from exact base revision", result.reasons)

    def test_protected_rename_retains_diagnostic_reason(self):
        sentinel = "control/INDEX.json"
        result = assess_reconvergence(
            base_paths=[sentinel, "README.md"],
            changes=[
                Change(
                    status="R100",
                    previous_path=sentinel,
                    path="control/INDEX.old.json",
                )
            ],
        )

        self.assertFalse(result.allowed)
        self.assertTrue(
            any("protected canonical sentinel damage" in reason for reason in result.reasons)
        )

    def test_scope_violation_retains_diagnostic_reason(self):
        result = assess_reconvergence(
            base_paths=["owned/a.py", "README.md"],
            changes=[
                Change(status="M", path="owned/a.py"),
                Change(status="M", path="README.md"),
            ],
            protected_sentinels=frozenset(),
            allowed_scopes=("owned",),
        )

        self.assertFalse(result.allowed)
        self.assertTrue(
            any("changed paths outside declared mutation scope" in reason for reason in result.reasons)
        )

    def test_live_base_event_values_enter_shell_only_through_env(self):
        workflow = (
            REPO_ROOT / ".github" / "workflows" / "reconvergence-integrity.yml"
        ).read_text(encoding="utf-8")
        step = workflow.split(
            "- name: Verify event base is still live target tip",
            1,
        )[1].split("- name: Fetch exact candidate revision", 1)[0]
        shell_body = step.split("run: |", 1)[1]

        self.assertIn(
            "BASE_REF: ${{ github.event.pull_request.base.ref }}",
            step,
        )
        self.assertIn(
            "BASE_SHA: ${{ github.event.pull_request.base.sha }}",
            step,
        )
        self.assertIn('"refs/heads/${BASE_REF}"', shell_body)
        self.assertIn('test "${live_base}" = "${BASE_SHA}"', shell_body)
        self.assertNotIn("${{ github.event.pull_request.base.ref }}", shell_body)
        self.assertNotIn("${{ github.event.pull_request.base.sha }}", shell_body)


if __name__ == "__main__":
    unittest.main()
