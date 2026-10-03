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
    parse_trusted_scope_approval,
    TRUSTED_SCOPE_APPROVAL_MARKER,
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

    def test_small_rename_away_counts_as_disappearance_without_mass_block(self):
        changes = parse_name_status(["R100\told.py\tnew.py"])

        result = assess_reconvergence(
            base_paths=["old.py", "other.py"],
            changes=changes,
            max_deletions=2,
            max_deleted_fraction=1.0,
            protected_sentinels=frozenset(),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.deletion_count, 1)

    def test_mass_rename_away_fails_but_mass_copy_does_not(self):
        base = [f"path-{index}.txt" for index in range(100)]
        renames = [
            Change(status="R100", previous_path=path, path=f"moved/{path}")
            for path in base[:60]
        ]
        copies = [
            Change(status="C100", previous_path=path, path=f"copies/{path}")
            for path in base[:60]
        ]

        renamed = assess_reconvergence(
            base_paths=base,
            changes=renames,
            max_deletions=50,
            max_deleted_fraction=0.35,
            protected_sentinels=frozenset(),
        )
        copied = assess_reconvergence(
            base_paths=base,
            changes=copies,
            max_deletions=50,
            max_deleted_fraction=0.35,
            protected_sentinels=frozenset(),
        )

        self.assertFalse(renamed.allowed)
        self.assertEqual(renamed.deletion_count, 60)
        self.assertIn("mass base-tree deletion/rename-away", renamed.reasons[0])
        self.assertTrue(copied.allowed)
        self.assertEqual(copied.deletion_count, 0)

    def test_parser_rejects_malformed_records(self):
        with self.assertRaises(ValueError):
            parse_name_status(["R100\tonly-old-path"])

    def test_parser_rejects_unsupported_unmerged_and_invalid_scores(self):
        for record in (
            "U\tcontrol/INDEX.json",
            "X\tcontrol/INDEX.json",
            "R101\told.py\tnew.py",
            "R-1\told.py\tnew.py",
            "C101\told.py\tnew.py",
        ):
            with self.subTest(record=record):
                with self.assertRaises(ValueError):
                    parse_name_status([record])

    def test_parser_and_synthetic_changes_reject_noncanonical_paths(self):
        for path in (
            "../control/INDEX.json",
            "/control/INDEX.json",
            "control//INDEX.json",
            "control/./INDEX.json",
            "control\\INDEX.json",
            "control/INDEX.json\x00suffix",
            "control/INDEX.json\talias",
        ):
            with self.subTest(path=path):
                with self.assertRaises((TypeError, ValueError)):
                    assess_reconvergence(
                        base_paths=["README.md"],
                        changes=[Change(status="M", path=path)],
                        protected_sentinels=frozenset(),
                    )

    def test_synthetic_change_cannot_smuggle_previous_path_on_modify(self):
        with self.assertRaises(ValueError):
            assess_reconvergence(
                base_paths=["README.md"],
                changes=[
                    Change(
                        status="M",
                        path="README.md",
                        previous_path="control/INDEX.json",
                    )
                ],
                protected_sentinels=frozenset(),
            )

    def test_base_tree_paths_are_validated_before_assessment(self):
        with self.assertRaises(ValueError):
            assess_reconvergence(
                base_paths=["README.md", "../control/INDEX.json"],
                changes=[],
                protected_sentinels=frozenset(),
            )

    def test_public_module_entrypoint_boots_from_repository_root(self):
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
        self.assertIn("--allow-protected-path", completed.stdout)


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

    def test_protected_sentinel_modification_requires_exact_authorization(self):
        sentinel = "control/tools/reconvergence_integrity.py"
        result = assess_reconvergence(
            base_paths=[sentinel, "README.md"],
            changes=[Change(status="M", path=sentinel)],
            allowed_scopes=("control/tools",),
        )

        self.assertFalse(result.allowed)
        self.assertEqual(
            result.protected_violations,
            (f"{sentinel} (unauthorized trust-root modification)",),
        )
        self.assertEqual(result.scope_violations, ())
        self.assertIn("protected canonical sentinel damage", result.reasons[0])

    def test_exact_protected_path_authorization_allows_modification(self):
        sentinel = "control/tools/reconvergence_integrity.py"
        result = assess_reconvergence(
            base_paths=[sentinel, "README.md"],
            changes=[Change(status="M", path=sentinel)],
            allowed_scopes=("control/tools",),
            authorized_protected_paths=(sentinel,),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.protected_violations, ())
        self.assertEqual(result.scope_violations, ())

    def test_directory_scope_never_authorizes_protected_path(self):
        sentinel = ".github/workflows/reconvergence-integrity.yml"
        result = assess_reconvergence(
            base_paths=[sentinel, "README.md"],
            changes=[Change(status="M", path=sentinel)],
            allowed_scopes=(".github/workflows",),
        )

        self.assertFalse(result.allowed)
        self.assertEqual(
            result.protected_violations,
            (f"{sentinel} (unauthorized trust-root modification)",),
        )

    def test_protected_authorization_rejects_directory_or_non_sentinel(self):
        sentinel = "control/tools/reconvergence_integrity.py"
        for unauthorized in ("control/tools", "README.md"):
            with self.subTest(unauthorized=unauthorized):
                with self.assertRaises(ValueError):
                    assess_reconvergence(
                        base_paths=[sentinel, "README.md"],
                        changes=[Change(status="M", path=sentinel)],
                        authorized_protected_paths=(unauthorized,),
                    )

    def test_new_workflow_creation_requires_exact_authorization(self):
        path = ".github/workflows/spoof-verify.yml"
        blocked = assess_reconvergence(
            base_paths=[".github/workflows/verify.yml", "README.md"],
            changes=[Change(status="A", path=path)],
        )
        allowed = assess_reconvergence(
            base_paths=[".github/workflows/verify.yml", "README.md"],
            changes=[Change(status="A", path=path)],
            authorized_protected_paths=(path,),
        )

        self.assertFalse(blocked.allowed)
        self.assertEqual(
            blocked.protected_violations,
            (f"{path} (unauthorized workflow-authority creation)",),
        )
        self.assertTrue(allowed.allowed)

    def test_copy_or_rename_into_new_workflow_requires_exact_authorization(self):
        for status in ("C100", "R100"):
            with self.subTest(status=status):
                path = f".github/workflows/spoof-{status[0].lower()}.yml"
                change = Change(
                    status=status,
                    previous_path="templates/check.yml",
                    path=path,
                )
                blocked = assess_reconvergence(
                    base_paths=["templates/check.yml", ".github/workflows/verify.yml"],
                    changes=[change],
                )
                allowed = assess_reconvergence(
                    base_paths=["templates/check.yml", ".github/workflows/verify.yml"],
                    changes=[change],
                    authorized_protected_paths=(path,),
                )
                self.assertFalse(blocked.allowed)
                self.assertTrue(allowed.allowed)

    def test_exact_authorization_never_allows_sentinel_rename_away(self):
        sentinel = "control/tools/reconvergence_integrity.py"
        result = assess_reconvergence(
            base_paths=[sentinel, "README.md"],
            changes=[
                Change(
                    status="R100",
                    previous_path=sentinel,
                    path="control/tools/reconvergence_integrity.retired.py",
                )
            ],
            authorized_protected_paths=(sentinel,),
        )

        self.assertFalse(result.allowed)
        self.assertIn(
            f"{sentinel} -> control/tools/reconvergence_integrity.retired.py (rename)",
            result.protected_violations,
        )

    def test_exact_authorization_never_allows_sentinel_type_change(self):
        sentinel = "control/tools/reconvergence_integrity.py"
        result = assess_reconvergence(
            base_paths=[sentinel, "README.md"],
            changes=[Change(status="T", path=sentinel)],
            authorized_protected_paths=(sentinel,),
        )

        self.assertFalse(result.allowed)
        self.assertIn(f"{sentinel} (type change)", result.protected_violations)

    def test_non_executable_control_metadata_can_evolve_without_protected_approval(self):
        path = "control/qualification.json"
        result = assess_reconvergence(
            base_paths=[path, "README.md"],
            changes=[Change(status="M", path=path)],
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.protected_violations, ())

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

    def test_trusted_scope_approval_is_exact_head_bound(self):
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

    def test_current_head_malformed_approval_fails_closed(self):
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

    def test_canonical_workflow_uses_owner_exact_head_authority_only(self):
        workflow = Path(
            ".github/workflows/reconvergence-integrity.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("pull_request_target:", workflow)
        self.assertIn("issues: read", workflow)
        self.assertNotIn("--pull-request-event", workflow)
        self.assertNotIn("--allowed-scope", workflow)
        self.assertIn("--allow-protected-path", workflow)
        self.assertNotIn("pull_request.body", workflow)
        self.assertNotIn("pull_request.title", workflow)
        self.assertIn("Require event base to match live target branch tip", workflow)
        self.assertIn("BASE_REF: ${{ github.event.pull_request.base.ref }}", workflow)
        self.assertIn("BASE_SHA: ${{ github.event.pull_request.base.sha }}", workflow)
        self.assertIn('comment.get("author_association") != "OWNER"', workflow)
        self.assertIn("parse_trusted_scope_approval", workflow)
        self.assertIn("expected_head_sha=head_sha", workflow)
        self.assertIn("/issues/{pr_number}/comments", workflow)
        self.assertIn('args+=(--allow-protected-path "$path")', workflow)



if __name__ == "__main__":
    unittest.main()
