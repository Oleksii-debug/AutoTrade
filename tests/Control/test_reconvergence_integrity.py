from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from control.tools.reconvergence_integrity import (
    Change,
    PROTECTED_SENTINELS,
    assess_git_revisions,
    assess_reconvergence,
    parse_name_status,
    parse_name_status_z,
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
        self.assertIn(
            "head is not descended from exact base revision",
            result.reasons,
        )

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

    def test_rename_is_not_counted_as_deletion(self):
        changes = parse_name_status(["R100\told.py\tnew.py"])

        result = assess_reconvergence(
            base_paths=["old.py", "other.py"],
            changes=changes,
            max_deletions=2,
            max_deleted_fraction=0.6,
            protected_sentinels=frozenset(),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.deletion_count, 0)
        self.assertEqual(result.destructive_change_count, 1)
        self.assertEqual(result.destructive_change_fraction, 0.5)

    def test_mass_rename_away_is_counted_as_destructive_tree_change(self):
        base = [f"path-{index}.txt" for index in range(100)]
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
        self.assertEqual(result.deletion_count, 0)
        self.assertEqual(result.destructive_change_count, 60)
        self.assertEqual(result.destructive_change_fraction, 0.6)
        self.assertIn("mass destructive base-tree change", result.reasons[0])

    def test_mass_type_change_is_counted_as_destructive_tree_change(self):
        base = [f"path-{index}.txt" for index in range(100)]
        changes = [
            Change(status="T", path=path)
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
        self.assertEqual(result.deletion_count, 0)
        self.assertEqual(result.destructive_change_count, 60)
        self.assertIn("mass destructive base-tree change", result.reasons[0])

    def test_mass_copy_is_not_misclassified_as_destructive_tree_change(self):
        base = [f"path-{index}.txt" for index in range(100)]
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
        self.assertEqual(result.destructive_change_count, 0)

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
        self.assertIn("protected canonical sentinel damage", result.reasons[0])

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
        self.assertIn(
            "changed paths outside declared mutation scope",
            result.reasons[0],
        )

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

    def test_canonical_workflow_does_not_treat_pr_body_as_mutation_authority(self):
        workflow = Path(
            ".github/workflows/reconvergence-integrity.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("pull_request_target:", workflow)
        self.assertNotIn("--pull-request-event", workflow)
        self.assertNotIn("--allowed-scope", workflow)
        self.assertNotIn("edited", workflow)
        self.assertIn("github.event.pull_request.base.ref", workflow)
        self.assertIn("__autotrade_live_base", workflow)
        self.assertIn('github.event.pull_request.base.sha', workflow)


    def test_ordinary_protected_sentinel_modification_requires_exact_scope(self):
        sentinel = "control/tools/reconvergence_integrity.py"
        base = [sentinel, "README.md"]

        unauthorized = assess_reconvergence(
            base_paths=base,
            changes=[Change(status="M", path=sentinel)],
        )
        directory_scope = assess_reconvergence(
            base_paths=base,
            changes=[Change(status="M", path=sentinel)],
            allowed_scopes=("control/tools",),
        )
        exact_scope = assess_reconvergence(
            base_paths=base,
            changes=[Change(status="M", path=sentinel)],
            allowed_scopes=(sentinel,),
        )

        self.assertFalse(unauthorized.allowed)
        self.assertFalse(directory_scope.allowed)
        self.assertIn("content change without exact authorization", unauthorized.reasons[0])
        self.assertTrue(exact_scope.allowed)
        self.assertEqual(exact_scope.protected_violations, ())

    def test_rename_destination_cannot_replace_protected_path_without_exact_scope(self):
        sentinel = "control/tools/reconvergence_integrity.py"
        change = Change(
            status="R100",
            previous_path="candidate.py",
            path=sentinel,
        )
        blocked = assess_reconvergence(
            base_paths=["candidate.py", "README.md"],
            changes=[change],
        )
        authorized = assess_reconvergence(
            base_paths=["candidate.py", "README.md"],
            changes=[change],
            allowed_scopes=("candidate.py", sentinel),
        )

        self.assertFalse(blocked.allowed)
        self.assertIn("content change without exact authorization", blocked.reasons[0])
        self.assertTrue(authorized.allowed)

    def test_change_validation_rejects_unsupported_status_and_noncanonical_path(self):
        with self.assertRaisesRegex(ValueError, "Unsupported Git name-status"):
            parse_name_status(["U\tREADME.md"])
        with self.assertRaisesRegex(ValueError, "canonical repository-relative path"):
            assess_reconvergence(
                base_paths=["README.md"],
                changes=[Change(status="M", path="../README.md")],
                protected_sentinels=frozenset(),
            )

    def test_change_validation_rejects_synthetic_missing_base_source(self):
        with self.assertRaisesRegex(ValueError, "absent from the base tree"):
            assess_reconvergence(
                base_paths=["README.md"],
                changes=[Change(status="M", path="not-in-base.py")],
                protected_sentinels=frozenset(),
            )

    def test_candidate_tree_rejects_case_insensitive_add_collision(self):
        with self.assertRaisesRegex(
            ValueError,
            "case-insensitive path collision",
        ):
            assess_reconvergence(
                base_paths=["README.md", "src/runtime.py"],
                changes=[Change(status="A", path="readme.MD")],
                protected_sentinels=frozenset(),
            )

    def test_candidate_tree_rejects_case_insensitive_rename_collision(self):
        with self.assertRaisesRegex(
            ValueError,
            "case-insensitive path collision",
        ):
            assess_reconvergence(
                base_paths=["src/alpha.py", "src/BETA.py"],
                changes=[
                    Change(
                        status="R100",
                        previous_path="src/alpha.py",
                        path="src/beta.py",
                    )
                ],
                protected_sentinels=frozenset(),
            )

    def test_candidate_tree_allows_collision_only_when_conflicting_path_is_removed(self):
        result = assess_reconvergence(
            base_paths=["src/alpha.py", "src/BETA.py", "README.md"],
            changes=[
                Change(status="D", path="src/BETA.py"),
                Change(
                    status="R100",
                    previous_path="src/alpha.py",
                    path="src/beta.py",
                ),
            ],
            max_deletions=50,
            max_deleted_fraction=0.9,
            protected_sentinels=frozenset(),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.deletion_count, 1)
        self.assertEqual(result.destructive_change_count, 2)

    def test_base_tree_case_collision_fails_closed_even_without_changes(self):
        with self.assertRaisesRegex(
            ValueError,
            "case-insensitive path collision",
        ):
            assess_reconvergence(
                base_paths=["src/Module.py", "src/module.py"],
                changes=[],
                protected_sentinels=frozenset(),
            )

    def test_nul_name_status_parser_is_unambiguous_and_fail_closed(self):
        self.assertEqual(
            parse_name_status_z(b"M\x00README.md\x00A\x00new file.txt\x00"),
            (
                Change(status="M", path="README.md"),
                Change(status="A", path="new file.txt"),
            ),
        )
        with self.assertRaisesRegex(ValueError, "Malformed NUL-delimited"):
            parse_name_status_z(b"M\x00README.md")
        with self.assertRaisesRegex(ValueError, "canonical UTF-8"):
            parse_name_status_z(b"A\x00bad-\xff\x00")

    def test_public_module_entrypoint_assesses_real_git_candidate(self):
        repository_root = Path(__file__).resolve().parents[2]
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
            git("config", "user.email", "guard-entry@example.invalid")
            git("config", "user.name", "Guard Entry")
            (root / "README.md").write_text("base\n", encoding="utf-8")
            git("add", "README.md")
            git("commit", "-m", "base")
            base_sha = git("rev-parse", "HEAD")
            (root / "README.md").write_text("child\n", encoding="utf-8")
            git("commit", "-am", "child")
            head_sha = git("rev-parse", "HEAD")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "control.tools.reconvergence_integrity",
                    "--repo",
                    str(root),
                    "--base",
                    base_sha,
                    "--head",
                    head_sha,
                ],
                cwd=repository_root,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Reconvergence tree guard passed.", completed.stdout)

    def test_public_module_entrypoint_blocks_modified_trust_root_without_exact_scope(self):
        repository_root = Path(__file__).resolve().parents[2]
        sentinel = "control/tools/reconvergence_integrity.py"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "control" / "tools").mkdir(parents=True)

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
            git("config", "user.email", "guard-trust@example.invalid")
            git("config", "user.name", "Guard Trust")
            (root / sentinel).write_text("BASE = 1\n", encoding="utf-8")
            (root / "README.md").write_text("base\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "base")
            base_sha = git("rev-parse", "HEAD")
            (root / sentinel).write_text("BASE = 2\n", encoding="utf-8")
            git("commit", "-am", "modify guard")
            head_sha = git("rev-parse", "HEAD")

            common = [
                sys.executable,
                "-m",
                "control.tools.reconvergence_integrity",
                "--repo",
                str(root),
                "--base",
                base_sha,
                "--head",
                head_sha,
            ]
            blocked = subprocess.run(
                common,
                cwd=repository_root,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            authorized = subprocess.run(
                [*common, "--allowed-scope", sentinel],
                cwd=repository_root,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(blocked.returncode, 2)
        self.assertIn("content change without exact authorization", blocked.stdout)
        self.assertEqual(authorized.returncode, 0, authorized.stderr)

    def test_public_module_entrypoint_blocks_diverged_candidate(self):
        repository_root = Path(__file__).resolve().parents[2]
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
            git("config", "user.email", "guard-diverged@example.invalid")
            git("config", "user.name", "Guard Diverged")
            (root / "README.md").write_text("root\n", encoding="utf-8")
            git("add", "README.md")
            git("commit", "-m", "root")
            root_sha = git("rev-parse", "HEAD")
            (root / "accepted.txt").write_text("accepted\n", encoding="utf-8")
            git("add", "accepted.txt")
            git("commit", "-m", "accepted base")
            base_sha = git("rev-parse", "HEAD")

            git("checkout", "-b", "stale", root_sha)
            (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")
            git("add", "candidate.txt")
            git("commit", "-m", "diverged candidate")
            head_sha = git("rev-parse", "HEAD")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "control.tools.reconvergence_integrity",
                    "--repo",
                    str(root),
                    "--base",
                    base_sha,
                    "--head",
                    head_sha,
                ],
                cwd=repository_root,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("head is not descended from exact base revision", completed.stdout)

    def test_public_module_entrypoint_blocks_mass_rename_away(self):
        repository_root = Path(__file__).resolve().parents[2]
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
            git("config", "user.email", "guard-rename@example.invalid")
            git("config", "user.name", "Guard Rename")
            for index in range(100):
                (root / f"path-{index}.txt").write_text(
                    f"{index}\n",
                    encoding="utf-8",
                )
            git("add", ".")
            git("commit", "-m", "full base")
            base_sha = git("rev-parse", "HEAD")
            (root / "moved").mkdir()
            for index in range(60):
                git(
                    "mv",
                    f"path-{index}.txt",
                    f"moved/path-{index}.txt",
                )
            git("commit", "-m", "mass rename candidate")
            head_sha = git("rev-parse", "HEAD")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "control.tools.reconvergence_integrity",
                    "--repo",
                    str(root),
                    "--base",
                    base_sha,
                    "--head",
                    head_sha,
                ],
                cwd=repository_root,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("mass destructive base-tree change", completed.stdout)
        self.assertIn("deletions=0", completed.stdout)
        self.assertIn("destructive_changes=60", completed.stdout)

    def test_public_module_entrypoint_blocks_sparse_mass_deletion(self):
        repository_root = Path(__file__).resolve().parents[2]
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
            git("config", "user.email", "guard-sparse@example.invalid")
            git("config", "user.name", "Guard Sparse")
            for index in range(100):
                (root / f"path-{index}.txt").write_text(
                    f"{index}\n",
                    encoding="utf-8",
                )
            git("add", ".")
            git("commit", "-m", "full base")
            base_sha = git("rev-parse", "HEAD")
            for index in range(60):
                (root / f"path-{index}.txt").unlink()
            git("add", "-A")
            git("commit", "-m", "sparse replacement candidate")
            head_sha = git("rev-parse", "HEAD")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "control.tools.reconvergence_integrity",
                    "--repo",
                    str(root),
                    "--base",
                    base_sha,
                    "--head",
                    head_sha,
                ],
                cwd=repository_root,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("mass base-tree deletion", completed.stdout)

    def test_public_module_help_bootstraps_from_repository_root(self):
        repository_root = Path(__file__).resolve().parents[2]
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "control.tools.reconvergence_integrity",
                "--help",
            ],
            cwd=repository_root,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--allowed-scope", completed.stdout)
        self.assertIn("--repo", completed.stdout)



if __name__ == "__main__":
    unittest.main()
