from __future__ import annotations

import os
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
            max_deleted_fraction=0.1,
            protected_sentinels=frozenset(),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.deletion_count, 0)

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

    def test_protected_sentinel_modification_requires_exact_path_authorization(self):
        sentinel = "control/tools/reconvergence_integrity.py"
        base = [sentinel, "README.md"]

        blocked = assess_reconvergence(
            base_paths=base,
            changes=[Change(status="M", path=sentinel)],
        )
        self.assertFalse(blocked.allowed)
        self.assertEqual(
            blocked.protected_violations,
            (f"{sentinel} (modified without exact-path authorization)",),
        )

        directory_scope = assess_reconvergence(
            base_paths=base,
            changes=[Change(status="M", path=sentinel)],
            allowed_scopes=("control/tools",),
        )
        self.assertFalse(directory_scope.allowed)
        self.assertIn("modified without exact-path authorization", directory_scope.reasons[0])

        exact_scope = assess_reconvergence(
            base_paths=base,
            changes=[Change(status="M", path=sentinel)],
            allowed_scopes=(sentinel,),
        )
        self.assertTrue(exact_scope.allowed)
        self.assertEqual(exact_scope.protected_violations, ())

    def test_exact_scope_never_authorizes_protected_sentinel_removal(self):
        sentinel = ".github/workflows/reconvergence-integrity.yml"
        result = assess_reconvergence(
            base_paths=[sentinel, "README.md"],
            changes=[Change(status="D", path=sentinel)],
            allowed_scopes=(sentinel,),
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.protected_deletions, (sentinel,))

    def test_change_rejects_unmerged_unknown_and_noncanonical_paths(self):
        for status in ("U", "X", "B", "MM", "R", "R101", "C999"):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    Change(status=status, path="owned/file.py")

        for path in (
            "",
            "/absolute.py",
            "../escape.py",
            "owned/../escape.py",
            "owned//file.py",
            "owned\\file.py",
            "owned/line\nfeed.py",
            "owned/tab\tfile.py",
            "owned/control\x1fchar.py",
            "owned/delete\x7fchar.py",
        ):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    Change(status="M", path=path)

    def test_assessment_rejects_change_subclass_bypass(self):
        class ForgedChange(Change):
            pass

        with self.assertRaises(TypeError):
            assess_reconvergence(
                base_paths=["README.md"],
                changes=[ForgedChange(status="M", path="README.md")],
                protected_sentinels=frozenset(),
            )

    def test_module_entrypoint_boots_from_repository_root(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "control.tools.reconvergence_integrity",
                "--help",
            ],
            cwd=Path.cwd(),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--base", completed.stdout)
        self.assertIn("--head", completed.stdout)

    def test_module_entrypoint_assesses_real_git_history_and_exact_trust_scope(self):
        repository_root = Path.cwd().resolve()
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
            git("config", "user.email", "reconvergence-cli@example.invalid")
            git("config", "user.name", "Reconvergence CLI Test")
            sentinel = root / "control" / "tools" / "reconvergence_integrity.py"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("TRUST = 1\n", encoding="utf-8")
            (root / "README.md").write_text("base\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "base")
            base_sha = git("rev-parse", "HEAD")

            (root / "README.md").write_text("child\n", encoding="utf-8")
            git("add", "README.md")
            git("commit", "-m", "ordinary child")
            ordinary_head = git("rev-parse", "HEAD")

            env = os.environ.copy()
            python_path = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                str(repository_root)
                if not python_path
                else str(repository_root) + os.pathsep + python_path
            )

            ordinary = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "control.tools.reconvergence_integrity",
                    "--base",
                    base_sha,
                    "--head",
                    ordinary_head,
                ],
                cwd=root,
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(ordinary.returncode, 0, ordinary.stderr + ordinary.stdout)

            protected_base = ordinary_head
            sentinel.write_text("TRUST = 2\n", encoding="utf-8")
            git("add", sentinel.relative_to(root).as_posix())
            git("commit", "-m", "modify trust root")
            protected_head = git("rev-parse", "HEAD")

            blocked = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "control.tools.reconvergence_integrity",
                    "--base",
                    protected_base,
                    "--head",
                    protected_head,
                ],
                cwd=root,
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(blocked.returncode, 2, blocked.stderr + blocked.stdout)
            self.assertIn("modified without exact-path authorization", blocked.stdout)

            directory_scope = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "control.tools.reconvergence_integrity",
                    "--base",
                    protected_base,
                    "--head",
                    protected_head,
                    "--allowed-scope",
                    "control/tools",
                    "--allowed-scope-head",
                    protected_head,
                ],
                cwd=root,
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(
                directory_scope.returncode,
                2,
                directory_scope.stderr + directory_scope.stdout,
            )

            exact_scope = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "control.tools.reconvergence_integrity",
                    "--base",
                    protected_base,
                    "--head",
                    protected_head,
                    "--allowed-scope",
                    "control/tools/reconvergence_integrity.py",
                    "--allowed-scope-head",
                    protected_head,
                ],
                cwd=root,
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(
                exact_scope.returncode,
                0,
                exact_scope.stderr + exact_scope.stdout,
            )

    def test_module_entrypoint_rejects_diverged_and_sparse_real_git_histories(self):
        repository_root = Path.cwd().resolve()
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

            def run_guard(base: str, head: str) -> subprocess.CompletedProcess[str]:
                env = os.environ.copy()
                python_path = env.get("PYTHONPATH")
                env["PYTHONPATH"] = (
                    str(repository_root)
                    if not python_path
                    else str(repository_root) + os.pathsep + python_path
                )
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
                    env=env,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

            git("init")
            git("config", "user.email", "reconvergence-history@example.invalid")
            git("config", "user.name", "Reconvergence History Test")
            (root / "README.md").write_text("root\n", encoding="utf-8")
            git("add", "README.md")
            git("commit", "-m", "root")
            root_sha = git("rev-parse", "HEAD")

            for index in range(100):
                (root / f"path-{index:03d}.txt").write_text(
                    f"{index}\n",
                    encoding="utf-8",
                )
            git("add", ".")
            git("commit", "-m", "accepted base")
            base_sha = git("rev-parse", "HEAD")

            git("checkout", "-b", "diverged", root_sha)
            (root / "diverged.txt").write_text("sibling\n", encoding="utf-8")
            git("add", "diverged.txt")
            git("commit", "-m", "diverged sibling")
            diverged_sha = git("rev-parse", "HEAD")
            diverged = run_guard(base_sha, diverged_sha)
            self.assertEqual(
                diverged.returncode,
                2,
                diverged.stderr + diverged.stdout,
            )
            self.assertIn(
                "head is not descended from exact base revision",
                diverged.stdout,
            )

            git("checkout", "-B", "sparse", base_sha)
            for index in range(60):
                (root / f"path-{index:03d}.txt").unlink()
            git("add", "-A")
            git("commit", "-m", "sparse replacement")
            sparse_sha = git("rev-parse", "HEAD")
            sparse = run_guard(base_sha, sparse_sha)
            self.assertEqual(
                sparse.returncode,
                2,
                sparse.stderr + sparse.stdout,
            )
            self.assertIn("mass base-tree deletion", sparse.stdout)

    def test_canonical_workflow_rejects_stale_target_branch_event_base(self):
        workflow = Path(
            ".github/workflows/reconvergence-integrity.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("Confirm event base is current target tip", workflow)
        self.assertIn("BASE_REF:", workflow)
        self.assertIn("EVENT_BASE_SHA:", workflow)
        self.assertIn("refs/autotrade/reconvergence-target", workflow)
        self.assertIn(
            'test "$(git rev-parse "refs/autotrade/reconvergence-target^{commit}")" = "$EVENT_BASE_SHA"',
            workflow,
        )

    def test_canonical_workflow_does_not_treat_pr_body_as_mutation_authority(self):
        workflow = Path(
            ".github/workflows/reconvergence-integrity.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("pull_request_target:", workflow)
        self.assertNotIn("--pull-request-event", workflow)
        self.assertNotIn("--allowed-scope", workflow)
        self.assertNotIn("edited", workflow)


    def test_nul_parser_and_change_graph_fail_closed(self):
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
        with self.assertRaisesRegex(ValueError, "absent from the base tree"):
            assess_reconvergence(
                base_paths=["README.md"],
                changes=[Change(status="M", path="missing.py")],
                protected_sentinels=frozenset(),
            )
        with self.assertRaisesRegex(ValueError, "multiple mutating Git changes"):
            assess_reconvergence(
                base_paths=["README.md", "src/a.py"],
                changes=[
                    Change(status="M", path="src/a.py"),
                    Change(status="D", path="src/a.py"),
                ],
                protected_sentinels=frozenset(),
            )

    def test_mass_rename_is_destructive_but_copy_is_not(self):
        base = [f"path-{index:03d}.txt" for index in range(100)]
        renames = [
            Change(
                status="R100",
                previous_path=path,
                path=f"moved/{path}",
            )
            for path in base[:60]
        ]
        renamed = assess_reconvergence(
            base_paths=base,
            changes=renames,
            max_deletions=50,
            max_deleted_fraction=0.35,
            protected_sentinels=frozenset(),
        )
        self.assertFalse(renamed.allowed)
        self.assertEqual(renamed.deletion_count, 0)
        self.assertEqual(renamed.destructive_change_count, 60)
        self.assertIn("mass destructive base-tree change", renamed.reasons[-1])

        copies = [
            Change(
                status="C100",
                previous_path=path,
                path=f"copies/{path}",
            )
            for path in base[:60]
        ]
        copied = assess_reconvergence(
            base_paths=base,
            changes=copies,
            max_deletions=50,
            max_deleted_fraction=0.35,
            protected_sentinels=frozenset(),
        )
        self.assertTrue(copied.allowed)
        self.assertEqual(copied.destructive_change_count, 0)

    def test_workflow_authority_is_dynamic_and_exact_scope_is_required(self):
        new_workflow = ".github/workflows/new-authority.yml"
        blocked = assess_reconvergence(
            base_paths=["README.md"],
            changes=[Change(status="A", path=new_workflow)],
        )
        self.assertFalse(blocked.allowed)
        self.assertIn(
            "authority destination without exact authorization",
            blocked.reasons[0],
        )
        authorized = assess_reconvergence(
            base_paths=["README.md"],
            changes=[Change(status="A", path=new_workflow)],
            allowed_scopes=(new_workflow,),
        )
        self.assertTrue(authorized.allowed)

        next_run_blocked = assess_reconvergence(
            base_paths=["README.md", new_workflow],
            changes=[Change(status="M", path=new_workflow)],
        )
        self.assertFalse(next_run_blocked.allowed)
        self.assertIn("content change without exact authorization", next_run_blocked.reasons[0])

    def test_protected_destination_replacement_requires_exact_scope(self):
        sentinel = "control/tools/reconvergence_integrity.py"
        for status in ("R100", "C100"):
            with self.subTest(status=status):
                blocked = assess_reconvergence(
                    base_paths=["candidate.py", "README.md"],
                    changes=[
                        Change(
                            status=status,
                            previous_path="candidate.py",
                            path=sentinel,
                        )
                    ],
                )
                self.assertFalse(blocked.allowed)
                self.assertIn(
                    "authority destination without exact authorization",
                    blocked.reasons[0],
                )

    def test_candidate_tree_rejects_windows_unsafe_and_case_colliding_paths(self):
        for bad_path in (
            "src/CON.txt",
            "src/name?.py",
            "src/trailing.",
            "src/trailing ",
        ):
            with self.subTest(path=bad_path):
                with self.assertRaisesRegex(ValueError, "Windows-checkout-safe"):
                    assess_reconvergence(
                        base_paths=["README.md"],
                        changes=[Change(status="A", path=bad_path)],
                        protected_sentinels=frozenset(),
                    )

        with self.assertRaisesRegex(ValueError, "case-insensitive path collision"):
            assess_reconvergence(
                base_paths=["README.md", "src/runtime.py"],
                changes=[Change(status="A", path="readme.MD")],
                protected_sentinels=frozenset(),
            )

    def test_cli_scope_authorization_is_exact_head_bound(self):
        repository_root = Path.cwd().resolve()
        sentinel = "control/tools/reconvergence_integrity.py"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "control" / "tools").mkdir(parents=True)

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
            git("config", "user.email", "scope-head@example.invalid")
            git("config", "user.name", "Scope Head")
            (root / sentinel).write_text("VALUE = 1\n", encoding="utf-8")
            (root / "README.md").write_text("base\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "base")
            base_sha = git("rev-parse", "HEAD")
            (root / sentinel).write_text("VALUE = 2\n", encoding="utf-8")
            git("commit", "-am", "candidate one")
            head_one = git("rev-parse", "HEAD")

            env = os.environ.copy()
            python_path = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                str(repository_root)
                if not python_path
                else str(repository_root) + os.pathsep + python_path
            )
            common = [
                sys.executable,
                "-m",
                "control.tools.reconvergence_integrity",
                "--repo",
                str(root),
                "--base",
                base_sha,
                "--head",
                head_one,
                "--allowed-scope",
                sentinel,
            ]
            exact = subprocess.run(
                [*common, "--allowed-scope-head", head_one],
                cwd=repository_root,
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(exact.returncode, 0, exact.stderr + exact.stdout)

            (root / "README.md").write_text("moved head\n", encoding="utf-8")
            git("commit", "-am", "candidate two")
            head_two = git("rev-parse", "HEAD")
            stale = subprocess.run(
                [
                    *common[:-3],
                    head_two,
                    "--allowed-scope",
                    sentinel,
                    "--allowed-scope-head",
                    head_one,
                ],
                cwd=repository_root,
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(stale.returncode, 0)
            self.assertIn(
                "not bound to the exact candidate head",
                stale.stderr + stale.stdout,
            )


if __name__ == "__main__":
    unittest.main()
