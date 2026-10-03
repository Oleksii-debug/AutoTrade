from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from control.tools.reconvergence_integrity import (
    Change,
    PROTECTED_SENTINELS,
    SELF_PROTECTING_TRUST_ROOTS,
    assess_git_revisions,
    assess_reconvergence,
    parse_name_status,
)


class ReconvergenceIntegrityTests(unittest.TestCase):
    def test_mass_base_tree_deletion_fails_closed(self):
        base = [f"path-{index}.txt" for index in range(100)]
        changes = [Change(status="D", path=path) for path in base[:60]]

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
        self.assertIn("mass base-tree deletion", result.reasons[0])

    def test_small_scoped_deletion_is_not_misclassified_as_tree_destruction(self):
        base = [f"path-{index}.txt" for index in range(100)]
        result = assess_reconvergence(
            base_paths=base,
            changes=[Change(status="D", path="path-1.txt")],
            protected_sentinels=frozenset(),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.deletion_count, 1)

    def test_full_tree_but_diverged_candidate_fails_closed(self):
        result = assess_reconvergence(
            base_paths=["README.md", "mvp/runtime.py"],
            changes=[],
            protected_sentinels=frozenset(),
            base_is_ancestor=False,
        )

        self.assertFalse(result.allowed)
        self.assertFalse(result.base_is_ancestor)
        self.assertEqual(result.deletion_count, 0)
        self.assertIn(
            "head is not descended from exact base revision",
            result.reasons,
        )

    def test_git_guard_rejects_identical_tree_without_base_ancestry(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.run(
                    ["git", *args],
                    cwd=root,
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ).stdout.strip()

            git("init")
            git("config", "user.email", "reconvergence-test@example.invalid")
            git("config", "user.name", "Reconvergence Test")
            (root / "README.md").write_text("root\n", encoding="utf-8")
            git("add", "README.md")
            git("commit", "-m", "root")
            root_sha = git("rev-parse", "HEAD")

            (root / "mvp").mkdir()
            (root / "mvp" / "runtime.py").write_text(
                "VALUE = 1\n",
                encoding="utf-8",
            )
            git("add", "mvp/runtime.py")
            git("commit", "-m", "accepted base")
            base_sha = git("rev-parse", "HEAD")

            git("checkout", "-b", "stale-rebuild", root_sha)
            (root / "mvp").mkdir(exist_ok=True)
            (root / "mvp" / "runtime.py").write_text(
                "VALUE = 1\n",
                encoding="utf-8",
            )
            git("add", "mvp/runtime.py")
            git("commit", "-m", "same tree without base ancestry")
            head_sha = git("rev-parse", "HEAD")

            self.assertEqual(
                git("rev-parse", f"{base_sha}^{{tree}}"),
                git("rev-parse", f"{head_sha}^{{tree}}"),
            )
            result = assess_git_revisions(base_sha, head_sha, cwd=root)

        self.assertFalse(result.allowed)
        self.assertFalse(result.base_is_ancestor)
        self.assertEqual(result.deletion_count, 0)

    def test_protected_sentinel_deletion_fails_even_when_single_path(self):
        sentinel = "control/INDEX.json"
        self.assertIn(sentinel, PROTECTED_SENTINELS)

        result = assess_reconvergence(
            base_paths=[sentinel, "README.md", "mvp/runtime.py"],
            changes=[Change(status="D", path=sentinel)],
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.protected_deletions, (sentinel,))
        self.assertIn("protected canonical sentinel damage", result.reasons[0])

    def test_rename_source_counts_as_base_path_disappearance(self):
        changes = parse_name_status(["R100\told.py\tnew.py"])

        result = assess_reconvergence(
            base_paths=["old.py", "other.py"],
            changes=changes,
            max_deletions=1,
            max_deleted_fraction=0.1,
            protected_sentinels=frozenset(),
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.deletion_count, 1)
        self.assertEqual(result.deletion_fraction, 0.5)
        self.assertTrue(
            any("mass base-tree deletion/rename-away" in reason for reason in result.reasons)
        )

    def test_parser_rejects_malformed_records(self):
        with self.assertRaises(ValueError):
            parse_name_status(["R100\tonly-old-path"])

    def test_guard_itself_and_canonical_control_authorities_are_protected(self):
        for path in (
            ".github/workflows/reconvergence-integrity.yml",
            "control/tools/reconvergence_integrity.py",
            "control/tools/registry_state.py",
            "AGENTS.md",
            "control/CONSTITUTION.md",
            "control/qualification.json",
        ):
            with self.subTest(path=path):
                self.assertIn(path, PROTECTED_SENTINELS)

    def test_every_checked_in_workflow_is_protected_from_removal(self):
        workflow_dir = Path(".github") / "workflows"
        workflows = sorted(
            path.as_posix()
            for path in workflow_dir.glob("*.y*ml")
            if path.is_file()
        )
        self.assertGreaterEqual(len(workflows), 10)
        self.assertEqual(
            sorted(set(workflows) - PROTECTED_SENTINELS),
            [],
            "every checked-in workflow authority must be a protected sentinel",
        )

    def test_protected_sentinel_rename_away_fails_closed(self):
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
        self.assertEqual(result.protected_deletions, ())
        self.assertEqual(
            result.protected_violations,
            ("control/INDEX.json -> control/INDEX.old.json (rename)",),
        )

    def test_copy_of_protected_sentinel_does_not_mutate_source(self):
        sentinel = "control/INDEX.json"
        result = assess_reconvergence(
            base_paths=[sentinel, "README.md"],
            changes=[
                Change(
                    status="C100",
                    previous_path=sentinel,
                    path="evidence/INDEX-copy.json",
                )
            ],
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.protected_violations, ())

    def test_protected_sentinel_type_change_fails_closed(self):
        sentinel = "control/qualification.json"
        result = assess_reconvergence(
            base_paths=[sentinel, "README.md"],
            changes=[Change(status="T", path=sentinel)],
        )

        self.assertFalse(result.allowed)
        self.assertEqual(
            result.protected_violations,
            ("control/qualification.json (type change)",),
        )

    def test_declared_scope_rejects_small_unrelated_blob_change(self):
        result = assess_reconvergence(
            base_paths=[
                "mvp/autotrade_mvp/recovery.py",
                "mvp/tests/test_recovery.py",
                "README.md",
            ],
            changes=[
                Change(status="M", path="mvp/autotrade_mvp/recovery.py"),
                Change(status="M", path="README.md"),
            ],
            protected_sentinels=frozenset(),
            allowed_scopes=("mvp/autotrade_mvp/recovery.py",),
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.deletion_count, 0)
        self.assertEqual(result.scope_violations, ("README.md",))

    def test_declared_directory_scope_covers_owned_descendants(self):
        result = assess_reconvergence(
            base_paths=["web/src/app.js", "web/src/index.html"],
            changes=[
                Change(status="M", path="web/src/app.js"),
                Change(status="M", path="web/src/index.html"),
            ],
            protected_sentinels=frozenset(),
            allowed_scopes=("web/src",),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.scope_violations, ())

    def test_scope_guard_checks_both_sides_of_rename(self):
        result = assess_reconvergence(
            base_paths=["owned/old.py", "other/file.py"],
            changes=[
                Change(
                    status="R100",
                    previous_path="owned/old.py",
                    path="other/new.py",
                )
            ],
            protected_sentinels=frozenset(),
            allowed_scopes=("owned",),
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.scope_violations, ("other/new.py",))

    def test_scope_guard_checks_only_copy_destination(self):
        result = assess_reconvergence(
            base_paths=["shared/source.py", "owned/existing.py"],
            changes=[
                Change(
                    status="C100",
                    previous_path="shared/source.py",
                    path="owned/copied.py",
                )
            ],
            protected_sentinels=frozenset(),
            allowed_scopes=("owned",),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.scope_violations, ())

    def test_scope_guard_rejects_noncanonical_registry_scope(self):
        with self.assertRaises(ValueError):
            assess_reconvergence(
                base_paths=["web/src/app.js"],
                changes=[Change(status="M", path="web/src/app.js")],
                protected_sentinels=frozenset(),
                allowed_scopes=("web/*",),
            )

    def test_self_protecting_roots_are_exactly_the_executable_guard_roots(self):
        self.assertEqual(
            SELF_PROTECTING_TRUST_ROOTS,
            frozenset(
                {
                    ".github/workflows/reconvergence-integrity.yml",
                    "control/tools/reconvergence_integrity.py",
                }
            ),
        )

    def test_untrusted_plain_modification_of_guard_root_fails_closed(self):
        path = "control/tools/reconvergence_integrity.py"
        result = assess_reconvergence(
            base_paths=[path, "README.md"],
            changes=[Change(status="M", path=path)],
        )
        self.assertFalse(result.allowed)
        self.assertEqual(
            result.protected_violations,
            (f"{path} (unauthorized trust-root modification)",),
        )

    def test_directory_scope_cannot_authorize_guard_root_modification(self):
        path = "control/tools/reconvergence_integrity.py"
        result = assess_reconvergence(
            base_paths=[path, "README.md"],
            changes=[Change(status="M", path=path)],
            allowed_scopes=("control/tools",),
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.scope_violations, ())
        self.assertEqual(
            result.protected_violations,
            (f"{path} (unauthorized trust-root modification)",),
        )

    def test_exact_trusted_scope_can_authorize_guard_root_modification(self):
        path = "control/tools/reconvergence_integrity.py"
        result = assess_reconvergence(
            base_paths=[path, "README.md"],
            changes=[Change(status="M", path=path)],
            allowed_scopes=(path,),
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.protected_violations, ())
        self.assertEqual(result.scope_violations, ())

    def test_trust_root_approval_does_not_constrain_unrelated_changes(self):
        guard_path = "control/tools/reconvergence_integrity.py"
        ordinary_path = "owned/change.py"
        result = assess_reconvergence(
            base_paths=[guard_path, ordinary_path],
            changes=[
                Change(status="M", path=guard_path),
                Change(status="M", path=ordinary_path),
            ],
            trusted_root_approvals=(guard_path,),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.protected_violations, ())
        self.assertEqual(result.scope_violations, ())

    def test_trust_root_approval_is_exact_path_not_directory_authority(self):
        guard_path = "control/tools/reconvergence_integrity.py"
        result = assess_reconvergence(
            base_paths=[guard_path, "owned/change.py"],
            changes=[Change(status="M", path=guard_path)],
            trusted_root_approvals=("control/tools",),
        )

        self.assertFalse(result.allowed)
        self.assertIn(
            f"{guard_path} (unauthorized trust-root modification)",
            result.protected_violations,
        )
        self.assertEqual(result.scope_violations, ())

    def test_parser_rejects_unsupported_and_malformed_git_statuses(self):
        bad = (
            "U\tfile.py",
            "MM\tfile.py",
            "R101\told.py\tnew.py",
            "R00\told.py\tnew.py",
            "C-1\told.py\tnew.py",
        )
        for record in bad:
            with self.subTest(record=record):
                with self.assertRaises(ValueError):
                    parse_name_status([record])

    def test_assessment_revalidates_synthetic_change_objects(self):
        malformed = (
            Change(status="U", path="file.py"),
            Change(status="M", path="../escape.py"),
            Change(status="M", path="bad\x00path.py"),
            Change(status="R100", path="new.py"),
            Change(status="M", path="file.py", previous_path="old.py"),
        )
        for change in malformed:
            with self.subTest(change=change):
                with self.assertRaises((TypeError, ValueError)):
                    assess_reconvergence(
                        base_paths=["file.py"],
                        changes=[change],
                        protected_sentinels=frozenset(),
                    )

    def test_real_git_guard_rejects_plain_guard_root_modification(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "control/tools").mkdir(parents=True)
            path = root / "control/tools/reconvergence_integrity.py"

            def git(*args):
                return subprocess.run(
                    ["git", *args],
                    cwd=root,
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ).stdout.strip()

            git("init")
            git("config", "user.email", "reconvergence-test@example.invalid")
            git("config", "user.name", "Reconvergence Test")
            path.write_text("VALUE = 1\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "base")
            base_sha = git("rev-parse", "HEAD")
            path.write_text("VALUE = 2\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "modify guard")
            head_sha = git("rev-parse", "HEAD")

            result = assess_git_revisions(base_sha, head_sha, cwd=root)

        self.assertFalse(result.allowed)
        self.assertEqual(result.deletion_count, 0)
        self.assertTrue(
            any("unauthorized trust-root modification" in item
                for item in result.protected_violations)
        )

    def test_canonical_workflow_uses_module_entrypoint_and_live_base_tip_check(self):
        workflow = Path(
            ".github/workflows/reconvergence-integrity.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("pull_request_target:", workflow)
        self.assertIn(
            "python -m control.tools.reconvergence_integrity",
            workflow,
        )
        self.assertNotIn(
            "python control/tools/reconvergence_integrity.py",
            workflow,
        )
        self.assertIn("git ls-remote --refs origin", workflow)
        self.assertIn("github.event.pull_request.base.ref", workflow)
        self.assertIn("github.event.pull_request.base.sha", workflow)
        self.assertNotIn("--pull-request-event", workflow)
        self.assertIn("author_association", workflow)
        self.assertIn('"OWNER"', workflow)
        self.assertIn("expected_head_sha=head_sha", workflow)
        self.assertIn("APPROVED_SCOPE_FILE", workflow)
        self.assertIn('args+=(--trusted-root-approval "${scope}")', workflow)
        self.assertNotIn("--allowed-scope", workflow)
        self.assertNotIn("github.event.pull_request.body", workflow)
        self.assertNotIn("github.event.pull_request.title", workflow)
        self.assertNotIn("edited", workflow)

    def test_module_help_bootstraps_from_repository_root(self):
        completed = subprocess.run(
            [sys.executable, "-m", "control.tools.reconvergence_integrity", "--help"],
            cwd=Path.cwd(),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--base", completed.stdout)
        self.assertIn("--head", completed.stdout)

    def test_public_module_entrypoint_accepts_valid_base_to_child_candidate(self):
        repo_root = Path.cwd()
        with TemporaryDirectory() as directory:
            git_root = Path(directory)

            def git(*args):
                return subprocess.run(
                    ["git", *args],
                    cwd=git_root,
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ).stdout.strip()

            git("init")
            git("config", "user.email", "reconvergence-test@example.invalid")
            git("config", "user.name", "Reconvergence Test")
            (git_root / "README.md").write_text("base\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "base")
            base_sha = git("rev-parse", "HEAD")
            (git_root / "README.md").write_text("child\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "child")
            head_sha = git("rev-parse", "HEAD")

            env = dict(__import__("os").environ)
            env["PYTHONPATH"] = str(repo_root)
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
                cwd=git_root,
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Reconvergence tree guard passed.", completed.stdout)


if __name__ == "__main__":
    unittest.main()
