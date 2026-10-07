from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from control.tools.reconvergence_integrity import (
    Change,
    PROTECTED_MUTATION_ROOTS,
    PROTECTED_SENTINELS,
    TRUSTED_SCOPE_APPROVAL_MARKER,
    assess_git_revisions,
    assess_reconvergence,
    parse_name_status,
    parse_trusted_scope_approval,
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

    def test_parser_rejects_unsupported_git_status_and_malformed_paths(self):
        for record in (
            "U100\tfile.py",
            "Rxx\told.py\tnew.py",
            "M100\tfile.py",
            "Z\tfile.py",
            "M\t../file.py",
            "M\t/control/file.py",
            "M\tcontrol\\\\file.py",
            "M\tcontrol//file.py",
        ):
            with self.subTest(record=record):
                with self.assertRaises(ValueError):
                    parse_name_status([record])

    def test_assessment_rejects_synthetic_unsupported_or_malformed_change(self):
        for change in (
            Change(status="Z", path="README.md"),
            Change(status="M100", path="README.md"),
            Change(status="R101", previous_path="README.md", path="README2.md"),
            Change(status="R1", previous_path="README.md", path="README2.md"),
        ):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    assess_reconvergence(
                        base_paths=["README.md"],
                        changes=[change],
                        protected_sentinels=frozenset(),
                    )

    def test_assessment_rejects_synthetic_missing_or_spurious_previous_path(self):
        cases = (
            Change(status="R100", path="new.py"),
            Change(status="M", path="file.py", previous_path="old.py"),
        )
        for change in cases:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    assess_reconvergence(
                        base_paths=["new.py", "file.py"],
                        changes=[change],
                        protected_sentinels=frozenset(),
                    )

    def test_protected_rename_to_sentinel_is_blocked(self):
        sentinel = "control/INDEX.json"
        result = assess_reconvergence(
            base_paths=[sentinel, "other.py"],
            changes=[
                Change(
                    status="R100",
                    previous_path="other.py",
                    path=sentinel,
                )
            ],
        )

        self.assertFalse(result.allowed)
        self.assertIn(
            "other.py -> control/INDEX.json (rename)",
            result.protected_violations,
        )

    def test_copy_or_add_to_protected_sentinel_is_blocked(self):
        sentinel = "control/INDEX.json"
        for change in (
            Change(status="A", path=sentinel),
            Change(status="C100", previous_path="other.py", path=sentinel),
        ):
            with self.subTest(change=change):
                result = assess_reconvergence(
                    base_paths=[sentinel, "other.py"],
                    changes=[change],
                )

                self.assertFalse(result.allowed)
                self.assertIn(
                    f"{sentinel} (addition/copy)",
                    result.protected_violations,
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

    def test_executable_trust_root_modification_fails_closed_by_default(self):
        sentinel = "control/tools/reconvergence_integrity.py"
        result = assess_reconvergence(
            base_paths=[sentinel, "README.md"],
            changes=[Change(status="M", path=sentinel)],
        )

        self.assertFalse(result.allowed)
        self.assertEqual(
            result.protected_violations,
            (f"{sentinel} (modification)",),
        )

    def test_non_executable_protected_metadata_content_remains_mutable(self):
        sentinel = "control/INDEX.json"
        result = assess_reconvergence(
            base_paths=[sentinel, "README.md"],
            changes=[Change(status="M", path=sentinel)],
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.protected_violations, ())

    def test_only_two_executable_roots_accept_special_modification_authority(self):
        self.assertEqual(
            PROTECTED_MUTATION_ROOTS,
            frozenset(
                {
                    ".github/workflows/reconvergence-integrity.yml",
                    "control/tools/reconvergence_integrity.py",
                }
            ),
        )
        with self.assertRaisesRegex(ValueError, "executable trust roots"):
            assess_reconvergence(
                base_paths=["control/INDEX.json"],
                changes=[Change(status="M", path="control/INDEX.json")],
                authorized_protected_sentinel_paths=("control/INDEX.json",),
            )

    def test_trusted_protected_sentinel_modification_requires_exact_path_authorization(self):
        sentinel = "control/tools/reconvergence_integrity.py"
        result = assess_reconvergence(
            base_paths=[sentinel, "README.md"],
            changes=[Change(status="M", path=sentinel)],
            authorized_protected_sentinel_paths=(sentinel,),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.protected_violations, ())

    def test_protected_modification_authorization_does_not_become_a_directory_scope(self):
        with self.assertRaises(ValueError):
            assess_reconvergence(
                base_paths=["control/tools/reconvergence_integrity.py"],
                changes=[
                    Change(
                        status="M",
                        path="control/tools/reconvergence_integrity.py",
                    )
                ],
                authorized_protected_sentinel_paths=("control/tools",),
            )

    def test_protected_structure_remains_blocked_with_exact_modification_authorization(self):
        sentinel = "control/tools/reconvergence_integrity.py"
        result = assess_reconvergence(
            base_paths=[sentinel, "README.md"],
            changes=[Change(status="D", path=sentinel)],
            authorized_protected_sentinel_paths=(sentinel,),
        )

        self.assertFalse(result.allowed)
        self.assertIn(sentinel, result.protected_violations)

    def test_malformed_exact_protected_path_authorization_fails_closed(self):
        for value in ("", "control/*", "../control/INDEX.json", "/control/INDEX.json", "control\\\\INDEX.json", "control/tools/"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    assess_reconvergence(
                        base_paths=["control/INDEX.json"],
                        changes=[Change(status="M", path="control/INDEX.json")],
                        authorized_protected_sentinel_paths=(value,),
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

    def test_module_entrypoint_help_starts_from_repository_root(self):
        result = subprocess.run(
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

        self.assertEqual(result.returncode, 0)
        self.assertIn("Fail closed on malformed reconvergence tree destruction.", result.stdout)

    def test_module_entrypoint_assesses_temporary_git_repository(self):
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
            base_sha = git("rev-parse", "HEAD")

            (root / "README.md").write_text("child\n", encoding="utf-8")
            git("commit", "-am", "child")
            head_sha = git("rev-parse", "HEAD")

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "control.tools.reconvergence_integrity",
                    "--base",
                    base_sha,
                    "--head",
                    head_sha,
                    "--cwd",
                    str(root),
                ],
                cwd=Path.cwd(),
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Reconvergence tree guard passed.", result.stdout)

    def test_module_entrypoint_uses_exact_external_trust_root_approval(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "control" / "tools"
            target.mkdir(parents=True)
            guarded = target / "reconvergence_integrity.py"

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
            guarded.write_text("base\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "base")
            base_sha = git("rev-parse", "HEAD")

            guarded.write_text("child\n", encoding="utf-8")
            git("commit", "-am", "child")
            head_sha = git("rev-parse", "HEAD")

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "control.tools.reconvergence_integrity",
                    "--base",
                    base_sha,
                    "--head",
                    head_sha,
                    "--cwd",
                    str(root),
                    "--trusted-root-approval",
                    "control/tools/reconvergence_integrity.py",
                ],
                cwd=Path.cwd(),
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Reconvergence tree guard passed.", result.stdout)

    def test_module_entrypoint_rejects_diverged_temporary_git_repository(self):
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
            common_sha = git("rev-parse", "HEAD")

            git("checkout", "-b", "expected-base")
            (root / "README.md").write_text("base\n", encoding="utf-8")
            git("commit", "-am", "base")
            base_sha = git("rev-parse", "HEAD")

            git("checkout", "-b", "stale-rebuild", common_sha)
            (root / "README.md").write_text("stale\n", encoding="utf-8")
            git("commit", "-am", "stale")
            head_sha = git("rev-parse", "HEAD")

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "control.tools.reconvergence_integrity",
                    "--base",
                    base_sha,
                    "--head",
                    head_sha,
                    "--cwd",
                    str(root),
                ],
                cwd=Path.cwd(),
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("head is not descended from exact base revision", result.stdout)

    def test_owner_comment_approval_is_exact_head_bound_and_malformed_current_record_fails(self):
        head = "a" * 40
        body = "\n".join(
            (
                TRUSTED_SCOPE_APPROVAL_MARKER,
                f"head: {head}",
                "path: control/tools/reconvergence_integrity.py",
            )
        )
        self.assertEqual(
            parse_trusted_scope_approval(body, expected_head_sha=head),
            ("control/tools/reconvergence_integrity.py",),
        )
        self.assertIsNone(
            parse_trusted_scope_approval(
                body,
                expected_head_sha="b" * 40,
            )
        )
        with self.assertRaises(ValueError):
            parse_trusted_scope_approval(
                "\n".join(
                    (
                        TRUSTED_SCOPE_APPROVAL_MARKER,
                        f"head: {head}",
                        "path: ../escape.py",
                    )
                ),
                expected_head_sha=head,
            )

    def test_canonical_workflow_resolves_owner_comment_authority_not_pr_authorship(self):
        workflow = Path(
            ".github/workflows/reconvergence-integrity.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("issues: read", workflow)
        self.assertIn("author_association", workflow)
        self.assertIn('\"OWNER\"', workflow)
        self.assertIn("parse_trusted_scope_approval", workflow)
        self.assertIn("--trusted-root-approval", workflow)
        self.assertIn("HEAD_SHA:", workflow)
        self.assertIn("Reverify external exact-head trust-root approvals", workflow)
        self.assertIn('EVENT_BASE_SHA', workflow)
        self.assertIn('target_tip="$(git ls-remote origin', workflow)
        self.assertNotIn("AUTHOR_LOGIN", workflow)
        self.assertNotIn("github.repository_owner", workflow)
        self.assertNotIn("github.event.pull_request.user.login", workflow)
        self.assertNotIn("github.event.pull_request.body", workflow)
        self.assertNotIn("github.event.pull_request.title", workflow)

    def test_canonical_workflow_does_not_treat_pr_body_as_mutation_authority(self):
        workflow = Path(
            ".github/workflows/reconvergence-integrity.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("pull_request_target:", workflow)
        self.assertNotIn("--pull-request-event", workflow)
        self.assertNotIn("--allowed-scope", workflow)
        self.assertNotIn("edited", workflow)
        self.assertNotIn("--allow-protected-sentinel-modification", workflow)



if __name__ == "__main__":
    unittest.main()
