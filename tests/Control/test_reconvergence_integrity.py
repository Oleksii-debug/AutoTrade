from __future__ import annotations

from pathlib import Path
import json
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control.tools.reconvergence_integrity import (
    Change,
    IntegrityAssessment,
    PROTECTED_SENTINELS,
    assess_git_revisions,
    assess_reconvergence,
    main,
    parse_name_status,
    reconvergence_evidence,
)


class ReconvergenceIntegrityTests(unittest.TestCase):
    def test_exact_revision_evidence_is_canonical_and_secret_free(self):
        assessment = IntegrityAssessment(
            allowed=True,
            base_is_ancestor=True,
            base_path_count=700,
            deletion_count=0,
            deletion_fraction=0.0,
            protected_deletions=(),
            protected_violations=(),
            scope_violations=(),
            reasons=(),
        )
        evidence = reconvergence_evidence(
            base_sha="a" * 40,
            head_sha="b" * 40,
            assessment=assessment,
        )
        self.assertEqual(evidence["base_sha"], "a" * 40)
        self.assertEqual(evidence["head_sha"], "b" * 40)
        self.assertEqual(evidence["result"], "PASS")
        self.assertFalse(evidence["contains_secrets"])

        with self.assertRaisesRegex(ValueError, "exact lowercase Git object id"):
            reconvergence_evidence(
                base_sha="A" * 40,
                head_sha="b" * 40,
                assessment=assessment,
            )

    def test_cli_writes_fail_evidence_before_returning_blocked(self):
        blocked = IntegrityAssessment(
            allowed=False,
            base_is_ancestor=False,
            base_path_count=700,
            deletion_count=0,
            deletion_fraction=0.0,
            protected_deletions=(),
            protected_violations=(),
            scope_violations=(),
            reasons=("head is not descended from exact base revision",),
        )
        with TemporaryDirectory() as directory:
            output = Path(directory) / "reconvergence.json"
            with patch(
                "control.tools.reconvergence_integrity._git_commit_id",
                side_effect=["a" * 40, "b" * 40],
            ), patch(
                "control.tools.reconvergence_integrity.assess_git_revisions",
                return_value=blocked,
            ):
                status = main(
                    [
                        "--base",
                        "trusted-base",
                        "--head",
                        "candidate-head",
                        "--evidence-output",
                        str(output),
                    ]
                )
            self.assertEqual(status, 2)
            evidence = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(evidence["base_sha"], "a" * 40)
            self.assertEqual(evidence["head_sha"], "b" * 40)
            self.assertEqual(evidence["result"], "FAIL")
            self.assertEqual(
                evidence["reasons"],
                ["head is not descended from exact base revision"],
            )
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

    def test_canonical_workflow_persists_exact_base_head_guard_evidence(self):
        workflow = Path(
            ".github/workflows/reconvergence-integrity.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            '--evidence-output "artifacts/reconvergence-integrity.json"',
            workflow,
        )
        self.assertIn("Upload exact-head reconvergence evidence", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)
        self.assertIn(
            "reconvergence-integrity-${{ github.event.pull_request.number }}-"
            "${{ github.event.pull_request.head.sha }}",
            workflow,
        )
        self.assertIn("if: always() && github.event_name == 'pull_request_target'", workflow)
    def test_canonical_workflow_does_not_treat_pr_body_as_mutation_authority(self):
        workflow = Path(
            ".github/workflows/reconvergence-integrity.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("pull_request_target:", workflow)
        self.assertNotIn("--pull-request-event", workflow)
        self.assertNotIn("--allowed-scope", workflow)
        self.assertNotIn("edited", workflow)



if __name__ == "__main__":
    unittest.main()
