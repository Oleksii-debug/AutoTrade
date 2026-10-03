from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]


class ReconvergenceEntrypointTests(unittest.TestCase):
    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        current = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            str(REPO_ROOT)
            if not current
            else os.pathsep.join((str(REPO_ROOT), current))
        )
        return env

    def _git(self, root: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()

    def _init_repo(self, root: Path) -> None:
        self._git(root, "init")
        self._git(root, "config", "user.email", "reconvergence-entrypoint@example.invalid")
        self._git(root, "config", "user.name", "Reconvergence Entrypoint Test")

    def _run_guard(
        self,
        root: Path,
        base: str,
        head: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "control.tools.reconvergence_integrity",
                "--base",
                base,
                "--head",
                head,
            ],
            cwd=root,
            env=self._env(),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_public_module_entrypoint_accepts_valid_base_child(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._init_repo(root)
            (root / "README.md").write_text("base\n", encoding="utf-8")
            self._git(root, "add", ".")
            self._git(root, "commit", "-m", "base")
            base_sha = self._git(root, "rev-parse", "HEAD")

            (root / "README.md").write_text("child\n", encoding="utf-8")
            self._git(root, "add", ".")
            self._git(root, "commit", "-m", "child")
            head_sha = self._git(root, "rev-parse", "HEAD")

            completed = self._run_guard(root, base_sha, head_sha)

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("Reconvergence tree guard passed.", completed.stdout)

    def test_public_module_entrypoint_blocks_diverged_candidate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._init_repo(root)
            (root / "README.md").write_text("root\n", encoding="utf-8")
            self._git(root, "add", ".")
            self._git(root, "commit", "-m", "root")
            root_sha = self._git(root, "rev-parse", "HEAD")

            self._git(root, "checkout", "-b", "accepted")
            (root / "accepted.txt").write_text("base\n", encoding="utf-8")
            self._git(root, "add", ".")
            self._git(root, "commit", "-m", "accepted base")
            base_sha = self._git(root, "rev-parse", "HEAD")

            self._git(root, "checkout", "-b", "sibling", root_sha)
            (root / "sibling.txt").write_text("head\n", encoding="utf-8")
            self._git(root, "add", ".")
            self._git(root, "commit", "-m", "sibling head")
            head_sha = self._git(root, "rev-parse", "HEAD")

            completed = self._run_guard(root, base_sha, head_sha)

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("head is not descended from exact base revision", completed.stdout)

    def test_public_module_entrypoint_blocks_mass_deletion(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._init_repo(root)
            for index in range(100):
                (root / f"path-{index:03d}.txt").write_text("base\n", encoding="utf-8")
            self._git(root, "add", ".")
            self._git(root, "commit", "-m", "base")
            base_sha = self._git(root, "rev-parse", "HEAD")

            for index in range(60):
                (root / f"path-{index:03d}.txt").unlink()
            self._git(root, "add", "-A")
            self._git(root, "commit", "-m", "destructive child")
            head_sha = self._git(root, "rev-parse", "HEAD")

            completed = self._run_guard(root, base_sha, head_sha)

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("mass base-tree deletion/rename-away", completed.stdout)

    def test_public_module_entrypoint_blocks_trust_root_rewrite(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._init_repo(root)
            trust_root = root / "tools" / "verify.py"
            trust_root.parent.mkdir(parents=True)
            trust_root.write_text("raise SystemExit(1)\n", encoding="utf-8")
            (root / "README.md").write_text("base\n", encoding="utf-8")
            self._git(root, "add", ".")
            self._git(root, "commit", "-m", "base")
            base_sha = self._git(root, "rev-parse", "HEAD")

            trust_root.write_text("raise SystemExit(0)\n", encoding="utf-8")
            self._git(root, "add", ".")
            self._git(root, "commit", "-m", "weaken verifier")
            head_sha = self._git(root, "rev-parse", "HEAD")

            completed = self._run_guard(root, base_sha, head_sha)

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("unauthorized trust-root modification", completed.stdout)
        self.assertIn("tools/verify.py", completed.stdout)

    def test_public_module_entrypoint_bootstraps_from_repo_root_without_pythonpath(self):
        with TemporaryDirectory() as directory:
            git_root = Path(directory)
            self._init_repo(git_root)
            (git_root / "README.md").write_text("base\n", encoding="utf-8")
            self._git(git_root, "add", ".")
            self._git(git_root, "commit", "-m", "base")
            base_sha = self._git(git_root, "rev-parse", "HEAD")

            (git_root / "README.md").write_text("child\n", encoding="utf-8")
            self._git(git_root, "add", ".")
            self._git(git_root, "commit", "-m", "child")
            head_sha = self._git(git_root, "rev-parse", "HEAD")

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

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("Reconvergence tree guard passed.", completed.stdout)

    def test_public_module_entrypoint_accepts_real_zero_padded_rename_score(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._init_repo(root)
            original = root / "old.txt"
            original.write_text(
                "".join(f"stable line {index:03d}\n" for index in range(100)),
                encoding="utf-8",
            )
            self._git(root, "add", ".")
            self._git(root, "commit", "-m", "base")
            base_sha = self._git(root, "rev-parse", "HEAD")

            self._git(root, "mv", "old.txt", "new.txt")
            renamed = root / "new.txt"
            lines = renamed.read_text(encoding="utf-8").splitlines()
            lines[50] = "changed middle line"
            renamed.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-m", "rename with small edit")
            head_sha = self._git(root, "rev-parse", "HEAD")

            name_status = self._git(
                root,
                "diff",
                "--name-status",
                "--find-renames",
                base_sha,
                head_sha,
            )
            completed = self._run_guard(root, base_sha, head_sha)

        self.assertRegex(name_status, r"^R0\d{2}\told\.txt\tnew\.txt$")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_main_fails_closed_on_unmerged_git_status(self):
        from control.tools import reconvergence_integrity as guard

        def fake_run(command, *args, **kwargs):
            argv = list(command[1:])
            if argv[:2] == ["merge-base", "--is-ancestor"]:
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            if argv[:3] == ["ls-tree", "-r", "--name-only"]:
                return subprocess.CompletedProcess(command, 0, stdout="README.md\n", stderr="")
            if argv[:3] == ["diff", "--name-status", "--find-renames"]:
                return subprocess.CompletedProcess(command, 0, stdout="U\tREADME.md\n", stderr="")
            raise AssertionError(f"unexpected git command: {command!r}")

        with patch.object(guard.subprocess, "run", side_effect=fake_run):
            with self.assertRaisesRegex(ValueError, "unsupported or unmerged"):
                guard.main(["--base", "base", "--head", "head"])


if __name__ == "__main__":
    unittest.main()
