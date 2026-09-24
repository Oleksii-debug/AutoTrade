import os
from pathlib import Path
import unittest
from unittest.mock import patch

from tools.write_ci_evidence import build_evidence


ROOT = Path(__file__).resolve().parents[2]


class ExactHeadCiEvidenceTests(unittest.TestCase):
    def env(self, *, event="pull_request", source="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", pr_head=None):
        return {
            "AUTOTRADE_SOURCE_SHA": source,
            "AUTOTRADE_PR_HEAD_SHA": source if pr_head is None else pr_head,
            "GITHUB_EVENT_NAME": event,
            "RUNNER_OS": "Linux",
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_WORKFLOW": "Verify AutoTrade",
        }

    def test_pr_evidence_requires_checkout_to_exact_head(self):
        with patch.dict(os.environ, self.env(), clear=True), patch(
            "tools.write_ci_evidence.checked_out_sha",
            return_value="a" * 40,
        ):
            evidence = build_evidence(
                suite="full-repository",
                command="python tools/verify.py",
            )
        self.assertEqual(evidence["source_sha"], "a" * 40)
        self.assertEqual(evidence["checked_out_sha"], "a" * 40)
        self.assertEqual(evidence["result"], "PASS")
        self.assertFalse(evidence["contains_secrets"])

    def test_synthetic_merge_or_other_checkout_is_rejected(self):
        with patch.dict(os.environ, self.env(), clear=True), patch(
            "tools.write_ci_evidence.checked_out_sha",
            return_value="b" * 40,
        ):
            with self.assertRaisesRegex(ValueError, "checkout SHA mismatch"):
                build_evidence(
                    suite="full-repository",
                    command="python tools/verify.py",
                )

    def test_pr_head_environment_must_match_source_sha(self):
        with patch.dict(
            os.environ,
            self.env(pr_head="b" * 40),
            clear=True,
        ), patch(
            "tools.write_ci_evidence.checked_out_sha",
            return_value="a" * 40,
        ):
            with self.assertRaisesRegex(ValueError, "pull_request.head.sha"):
                build_evidence(
                    suite="full-repository",
                    command="python tools/verify.py",
                )

    def test_push_evidence_uses_checked_out_github_sha_without_pr_head(self):
        environment = self.env(event="push", pr_head="")
        with patch.dict(os.environ, environment, clear=True), patch(
            "tools.write_ci_evidence.checked_out_sha",
            return_value="a" * 40,
        ):
            evidence = build_evidence(
                suite="baseline",
                command="python tools/verify.py",
            )
        self.assertEqual(evidence["github"]["event_name"], "push")

    def test_primary_python_gates_checkout_explicit_exact_head_and_use_no_secrets(self):
        for relative in (
            ".github/workflows/verify.yml",
            ".github/workflows/baseline.yml",
        ):
            with self.subTest(relative=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn(
                    "AUTOTRADE_SOURCE_SHA: ${{ github.event.pull_request.head.sha || github.sha }}",
                    text,
                )
                self.assertIn("ref: ${{ env.AUTOTRADE_SOURCE_SHA }}", text)
                self.assertIn("tools/write_ci_evidence.py", text)
                self.assertIn("actions/upload-artifact@v4", text)
                self.assertNotIn("${{ secrets.", text)

    def test_all_ci_workflows_cancel_stale_runs_for_the_same_pr_or_ref(self):
        for relative in (
            ".github/workflows/baseline.yml",
            ".github/workflows/verify.yml",
            ".github/workflows/contracts.yml",
            ".github/workflows/research-primitives.yml",
            ".github/workflows/dotnet-foundation.yml",
            ".github/workflows/control-plane.yml",
        ):
            with self.subTest(relative=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("concurrency:", text)
                self.assertIn(
                    "group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}",
                    text,
                )
                self.assertIn("cancel-in-progress: true", text)

    def test_path_scoped_workflows_do_not_duplicate_feature_branch_push_and_pr_runs(self):
        for relative in (
            ".github/workflows/contracts.yml",
            ".github/workflows/research-primitives.yml",
            ".github/workflows/dotnet-foundation.yml",
            ".github/workflows/control-plane.yml",
        ):
            with self.subTest(relative=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                push_section = text.split("  pull_request:", 1)[0]
                self.assertIn("  push:\n    branches: [main]\n", push_section)
    def test_ci_evidence_writer_rejects_non_exact_source_identifier(self):
        environment = self.env(source="main", pr_head="main")
        with patch.dict(os.environ, environment, clear=True), patch(
            "tools.write_ci_evidence.checked_out_sha",
            return_value="main",
        ):
            with self.assertRaisesRegex(ValueError, "exact Git SHA"):
                build_evidence(
                    suite="full-repository",
                    command="python tools/verify.py",
                )


if __name__ == "__main__":
    unittest.main()
