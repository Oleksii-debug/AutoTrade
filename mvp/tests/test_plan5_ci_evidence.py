"""Negative and positive fixtures for exact-head Plan-5 component CI evidence.

The canonical writer grants no release, financial, or terminal-DONE authority.
These tests exercise identity and provenance invariants, not GitHub runner truth.
"""
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools import write_ci_evidence as evidence


SHA = "a" * 40
OTHER_SHA = "b" * 40


def _ci_env(*, sha=SHA, head=SHA, event="pull_request", runner="Windows"):
    return {
        "AUTOTRADE_SOURCE_SHA": sha,
        "AUTOTRADE_PR_HEAD_SHA": head,
        "GITHUB_EVENT_NAME": event,
        "GITHUB_RUN_ID": "314159",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_WORKFLOW": "plan5-terminal-component-qualification",
        "RUNNER_OS": runner,
    }


class Plan5CIEvidenceProvenanceTests(unittest.TestCase):
    def build(self, *, actual=SHA, **kwargs):
        with patch.dict(os.environ, _ci_env(**kwargs), clear=True):
            with patch.object(evidence, "checked_out_sha", return_value=actual):
                return evidence.build_evidence(
                    suite="plan5-terminal-desktop-native",
                    command="executed Windows source/build/emergency tests",
                )

    def test_exact_pr_sha_and_real_runner_metadata_are_bound(self):
        record = self.build()
        self.assertEqual(record["source_sha"], SHA)
        self.assertEqual(record["checked_out_sha"], SHA)
        self.assertEqual(record["result"], "PASS")
        self.assertEqual(record["runner_os"], "Windows")
        self.assertEqual(record["github"]["run_id"], "314159")
        self.assertEqual(record["github"]["run_attempt"], "2")
        self.assertEqual(record["github"]["event_name"], "pull_request")
        self.assertFalse(record["contains_secrets"])
        self.assertNotIn("DONE", record)
        self.assertNotIn("release_qualified", record)

    def test_actual_checkout_differs_from_frozen_source_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "checkout SHA mismatch"):
            self.build(actual=OTHER_SHA)

    def test_pull_request_head_cannot_differ_from_evidence_source(self):
        with self.assertRaisesRegex(ValueError, "PR evidence is not bound"):
            self.build(head=OTHER_SHA)

    def test_invalid_source_SHA_is_rejected_before_github_event_use(self):
        with self.assertRaisesRegex(ValueError, "exact Git SHA"):
            self.build(sha="untrusted-branch", actual="untrusted-branch")

    def test_missing_runner_metadata_cannot_issue_positive_receipt(self):
        environment = _ci_env()
        del environment["RUNNER_OS"]
        with patch.dict(os.environ, environment, clear=True):
            with patch.object(evidence, "checked_out_sha", return_value=SHA):
                with self.assertRaisesRegex(ValueError, "RUNNER_OS is required"):
                    evidence.build_evidence(suite="plan5-terminal-source-and-recovery", command="checks")

    def test_missing_pull_request_head_is_not_silently_inferred(self):
        environment = _ci_env()
        del environment["AUTOTRADE_PR_HEAD_SHA"]
        with patch.dict(os.environ, environment, clear=True):
            with patch.object(evidence, "checked_out_sha", return_value=SHA):
                with self.assertRaisesRegex(ValueError, "PR evidence is not bound"):
                    evidence.build_evidence(suite="plan5-terminal-keyboard-browser", command="checks")

    def test_workflow_dispatch_evidence_retains_distinct_event_class(self):
        record = self.build(event="workflow_dispatch", head=OTHER_SHA, runner="Linux")
        self.assertEqual(record["github"]["event_name"], "workflow_dispatch")
        self.assertEqual(record["runner_os"], "Linux")
        self.assertNotEqual(record["github"]["event_name"], "pull_request")

    def test_failed_sha_validation_does_not_publish_evidence(self):
        with TemporaryDirectory() as root:
            output = Path(root) / "no-positive-evidence.json"
            argv = ["write_ci_evidence.py", "--suite", "plan5-terminal-desktop-native",
                    "--command", "executed tests", "--output", str(output)]
            with patch.dict(os.environ, _ci_env(), clear=True):
                with patch.object(evidence, "checked_out_sha", return_value=OTHER_SHA):
                    with patch.object(sys, "argv", argv):
                        with self.assertRaisesRegex(ValueError, "checkout SHA mismatch"):
                            evidence.main()
            self.assertFalse(output.exists())

    def test_successful_writer_records_only_component_evidence(self):
        with TemporaryDirectory() as root:
            output = Path(root) / "component.json"
            argv = ["write_ci_evidence.py", "--suite", "plan5-terminal-keyboard-browser",
                    "--command", "real Playwright navigation", "--output", str(output)]
            with patch.dict(os.environ, _ci_env(runner="Linux"), clear=True):
                with patch.object(evidence, "checked_out_sha", return_value=SHA):
                    with patch.object(sys, "argv", argv):
                        self.assertEqual(evidence.main(), 0)
            record = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(record["source_sha"], SHA)
            self.assertEqual(record["suite"], "plan5-terminal-keyboard-browser")
            self.assertEqual(record["github"]["workflow"], "plan5-terminal-component-qualification")
            self.assertNotIn("DONE", record)
            self.assertNotIn("signed_attestation", record)


if __name__ == "__main__":
    unittest.main()
