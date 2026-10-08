"""Adversarial same-source/same-run Plan-5 evidence join fixtures (no release authority)."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.check_plan5_component_evidence import inspect_component_receipts


SHA = "a" * 40


def receipt(group, *, sha=SHA):
    suite, runner = {
        "source": ("plan5-terminal-source-and-recovery", "Linux"),
        "browser": ("plan5-terminal-keyboard-browser", "Linux"),
        "desktop": ("plan5-terminal-desktop-native", "Windows"),
    }[group]
    return {
        "schema_version": "1.0.0",
        "source_sha": sha,
        "checked_out_sha": sha,
        "suite": suite,
        "command": "executed existing component tests",
        "result": "PASS",
        "runner_os": runner,
        "python_version": "3.12.10",
        "github": {
            "event_name": "pull_request",
            "run_id": "314159",
            "run_attempt": "2",
            "workflow": "plan5-terminal-component-qualification",
        },
        "generated_at": "2026-10-08T20:00:00+00:00",
        "contains_secrets": False,
    }


class Plan5ComponentEvidenceJoinTests(unittest.TestCase):
    def setUp(self):
        self.work = TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        for group, name in (
            ("source", "plan5-terminal-source.json"),
            ("browser", "plan5-terminal-browser.json"),
            ("desktop", "plan5-terminal-desktop.json"),
        ):
            directory = self.root / group
            directory.mkdir()
            (directory / name).write_text(json.dumps(receipt(group)), encoding="utf-8")

    def check(self, **kwargs):
        expected = {
            "source_sha": SHA,
            "run_id": "314159",
            "run_attempt": "2",
            "event_name": "pull_request",
        }
        expected.update(kwargs)
        return inspect_component_receipts(self.root, **expected)

    def mutate(self, group, fn):
        name = f"plan5-terminal-{group}.json"
        p = self.root / group / name
        document = json.loads(p.read_text(encoding="utf-8"))
        fn(document)
        p.write_text(json.dumps(document), encoding="utf-8")

    def test_all_three_job_receipts_agree_without_issuing_done(self):
        result = self.check()
        self.assertEqual(result["source_sha"], SHA)
        self.assertEqual(result["run_id"], "314159")
        self.assertEqual(len(result["suite_names"]), 3)
        self.assertEqual(result["evidence_class"], "CI_STRUCTURAL_CONSISTENCY_ONLY")
        self.assertIs(result["terminal_done"], False)
        self.assertIs(result["release_qualified"], False)
        self.assertIs(result["independent_GitHub_API_authentication"], False)

    def test_stale_browser_source_sha_is_rejected(self):
        self.mutate("browser", lambda x: x.__setitem__("source_sha", "b"*40))
        with self.assertRaisesRegex(ValueError, "exact run/suite/source"):
            self.check()

    def test_source_receipt_from_different_ci_run_is_rejected(self):
        self.mutate("source", lambda x: x["github"].__setitem__("run_id", "314160"))
        with self.assertRaisesRegex(ValueError, "exact run/suite/source"):
            self.check()

    def test_desktop_receipt_different_retry_attempt_is_rejected(self):
        self.mutate("desktop", lambda x: x["github"].__setitem__("run_attempt", "1"))
        with self.assertRaisesRegex(ValueError, "exact run/suite/source"):
            self.check()

    def test_failed_desktop_build_cannot_join_positive_qualification(self):
        self.mutate("desktop", lambda x: x.__setitem__("result", "FAIL"))
        with self.assertRaisesRegex(ValueError, "exact run/suite/source"):
            self.check()

    def test_linux_result_cannot_impersonate_windows_native_build(self):
        self.mutate("desktop", lambda x: x.__setitem__("runner_os", "Linux"))
        with self.assertRaisesRegex(ValueError, "exact run/suite/source"):
            self.check()

    def test_unknown_extra_receipt_field_is_rejected(self):
        self.mutate("source", lambda x: x.__setitem__("terminal_done", True))
        with self.assertRaisesRegex(ValueError, "canonical writer"):
            self.check()

    def test_duplicate_json_keys_are_rejected(self):
        p = self.root / "browser" / "plan5-terminal-browser.json"
        content = p.read_text(encoding="utf-8")
        p.write_text(content.replace('"result": "PASS"', '"result": "PASS", "result": "PASS"'), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unambiguous UTF-8 JSON"):
            self.check()

    def test_missing_job_receipt_is_not_inferred_from_two_passes(self):
        (self.root / "desktop" / "plan5-terminal-desktop.json").unlink()
        with self.assertRaisesRegex(ValueError, "missing required CI receipt"):
            self.check()

    def test_not_git_sha_is_rejected_before_any_receipt_is_read(self):
        with self.assertRaisesRegex(ValueError, "expected component identity"):
            self.check(source_sha="main")

    def test_unqualified_event_class_cannot_cross_into_pull_request(self):
        self.mutate("browser", lambda x: x["github"].__setitem__("event_name", "workflow_dispatch"))
        with self.assertRaisesRegex(ValueError, "exact run/suite/source"):
            self.check()

    def test_naive_timestamp_is_rejected(self):
        self.mutate("source", lambda x: x.__setitem__("generated_at", "2026-10-08T20:00:00"))
        with self.assertRaisesRegex(ValueError, "timestamp is invalid"):
            self.check()

    def test_receipt_symlink_cannot_replace_exact_artifact(self):
        p = self.root / "desktop" / "plan5-terminal-desktop.json"
        other = self.root / "other.json"
        other.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        p.unlink()
        try:
            p.symlink_to(other)
        except (OSError, NotImplementedError):
            self.skipTest("platform cannot create test symlink")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.check()

    def test_receipt_over_budget_is_rejected_before_json_parse(self):
        p = self.root / "source" / "plan5-terminal-source.json"
        p.write_bytes(b"X" * 16385)
        with self.assertRaisesRegex(ValueError, "bounded source envelope"):
            self.check()


if __name__ == "__main__":
    unittest.main()
