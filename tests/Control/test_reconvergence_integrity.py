from __future__ import annotations

from pathlib import Path
import subprocess
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
            max_deletions=1,
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

    def test_canonical_workflow_does_not_treat_pr_body_as_mutation_authority(self):
        workflow = Path(
            ".github/workflows/reconvergence-integrity.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("pull_request_target:", workflow)
        self.assertNotIn("--pull-request-event", workflow)
        self.assertNotIn("--allowed-scope", workflow)
        self.assertNotIn("--allow-protected-sentinel", workflow)
        self.assertIn("--base-ref \"$BASE_REF\"", workflow)
        self.assertIn("BASE_REF: ${{ github.event.pull_request.base.ref }}", workflow)
        self.assertNotIn("edited", workflow)


    def test_protected_sentinel_plain_modification_requires_exact_authorization(self):
        sentinel = "control/INDEX.json"
        blocked = assess_reconvergence(
            base_paths=[sentinel, "README.md"],
            changes=[Change(status="M", path=sentinel)],
        )

        self.assertFalse(blocked.allowed)
        self.assertEqual(
            blocked.protected_violations,
            (f"{sentinel} (modified)",),
        )

        authorized = assess_reconvergence(
            base_paths=[sentinel, "README.md"],
            changes=[Change(status="M", path=sentinel)],
            allowed_protected_sentinels=(sentinel,),
        )
        self.assertTrue(authorized.allowed)
        self.assertEqual(authorized.protected_violations, ())

    def test_protected_authorization_requires_exact_sentinel_path(self):
        with self.assertRaises(ValueError):
            assess_reconvergence(
                base_paths=["control/INDEX.json", "README.md"],
                changes=[Change(status="M", path="control/INDEX.json")],
                allowed_protected_sentinels=("control",),
            )

    def test_protected_sentinel_add_and_copy_destination_fail_closed(self):
        sentinel = ".github/workflows/reconvergence-integrity.yml"
        added = assess_reconvergence(
            base_paths=["README.md"],
            changes=[Change(status="A", path=sentinel)],
        )
        copied = assess_reconvergence(
            base_paths=["README.md", "source.yml"],
            changes=[
                Change(
                    status="C100",
                    previous_path="source.yml",
                    path=sentinel,
                )
            ],
        )

        self.assertFalse(added.allowed)
        self.assertFalse(copied.allowed)
        self.assertIn(f"{sentinel} (added)", added.protected_violations)
        self.assertIn(
            f"{sentinel} (copy destination)",
            copied.protected_violations,
        )

    def test_parser_rejects_unmerged_unknown_and_broken_statuses(self):
        for status in ("U", "X", "B"):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    parse_name_status([f"{status}\tREADME.md"])

    def test_parser_rejects_noncanonical_changed_paths(self):
        for path in (
            "../escape.py",
            "nested/../escape.py",
            "/absolute.py",
            "nested\\windows.py",
            "nested//double.py",
        ):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    parse_name_status([f"M\t{path}"])

    def test_assessment_revalidates_synthetic_change_records(self):
        with self.assertRaises(ValueError):
            assess_reconvergence(
                base_paths=["README.md", "control/INDEX.json"],
                changes=[Change(status="M", path="../control/INDEX.json")],
            )
        with self.assertRaises(ValueError):
            assess_reconvergence(
                base_paths=["README.md"],
                changes=[Change(status="U", path="README.md")],
                protected_sentinels=frozenset(),
            )
        with self.assertRaises(ValueError):
            assess_reconvergence(
                base_paths=["README.md"],
                changes=[
                    Change(
                        status="M",
                        path="README.md",
                        previous_path="old.md",
                    )
                ],
                protected_sentinels=frozenset(),
            )

    def test_nul_delimited_parser_preserves_rename_fields(self):
        changes = parse_name_status_z(
            b"M\x00mvp/runtime.py\x00R100\x00old.py\x00new.py\x00"
        )
        self.assertEqual(
            changes,
            (
                Change(status="M", path="mvp/runtime.py"),
                Change(status="R100", previous_path="old.py", path="new.py"),
            ),
        )

    def test_nul_delimited_parser_rejects_truncated_or_non_utf8_output(self):
        with self.assertRaises(ValueError):
            parse_name_status_z(b"M\x00README.md")
        with self.assertRaises(ValueError):
            parse_name_status_z(b"M\x00\xff\x00")

    def test_git_guard_rejects_stale_event_base_against_live_target_tip(self):
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
            git("branch", "-M", "main")
            (root / "README.md").write_text("base\n", encoding="utf-8")
            git("add", "README.md")
            git("commit", "-m", "base")
            base_sha = git("rev-parse", "HEAD")

            git("checkout", "-b", "feature")
            (root / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
            git("add", "feature.py")
            git("commit", "-m", "feature")
            head_sha = git("rev-parse", "HEAD")

            git("checkout", "main")
            (root / "README.md").write_text("advanced\n", encoding="utf-8")
            git("add", "README.md")
            git("commit", "-m", "advance main")

            result = assess_git_revisions(
                base_sha,
                head_sha,
                base_ref="main",
                remote=str(root),
                cwd=root,
            )

        self.assertFalse(result.allowed)
        self.assertTrue(result.base_is_ancestor)
        self.assertFalse(result.base_matches_remote_tip)
        self.assertIn(
            "event base revision is not current remote target tip",
            result.reasons,
        )

    def test_git_guard_accepts_exact_live_target_tip(self):
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
            git("branch", "-M", "main")
            (root / "README.md").write_text("base\n", encoding="utf-8")
            git("add", "README.md")
            git("commit", "-m", "base")
            base_sha = git("rev-parse", "HEAD")

            git("checkout", "-b", "feature")
            (root / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
            git("add", "feature.py")
            git("commit", "-m", "feature")
            head_sha = git("rev-parse", "HEAD")

            result = assess_git_revisions(
                base_sha,
                head_sha,
                base_ref="main",
                remote=str(root),
                cwd=root,
            )

        self.assertTrue(result.allowed)
        self.assertTrue(result.base_matches_remote_tip)


if __name__ == "__main__":
    unittest.main()
