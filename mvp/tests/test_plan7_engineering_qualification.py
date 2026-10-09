"""Plan-7 Section-7 orchestration never promotes fixture PASS into release."""
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from tools import qualify_plan7_engineering as target


class Plan7WholeQualificationOrchestrationTests(unittest.TestCase):
    def test_all_scientific_trust_ablation_matrix_and_load_campaigns_are_selected(self):
        suites = target.campaigns_for()
        self.assertEqual(
            set(suites), {"science", "ablation", "economics", "trust",
                          "evidence-matrix", "performance", "target-host"},
        )
        counts = target.validate_inventory()
        self.assertTrue(all(n > 0 for n in counts.values()))
        self.assertTrue(set(target.CRITICAL_NEGATIVES).issubset({
            name for modules in suites.values() for name in modules
        }))

    def test_missing_suite_or_negative_can_never_pass_inventory(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            suite = root / "sample.py"
            suite.write_text("def test_ok():\n    pass\n", encoding="utf-8")
            self.assertEqual(
                target.validate_inventory(root, {"x": ("sample",)}, {}), {"x": 1}
            )
            with self.assertRaisesRegex(ValueError, "required negative"):
                target.validate_inventory(
                    root, {"x": ("sample",)}, {"sample": ("test_anti_forgery",)}
                )
            suite.write_text("def helper():\n    pass\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "empty suite"):
                target.validate_inventory(root, {"x": ("sample",)}, {})
            suite.unlink()
            with self.assertRaisesRegex(ValueError, "missing"):
                target.validate_inventory(root, {"x": ("sample",)}, {})

    def test_unexecuted_critical_suite_cannot_be_omitted(self):
        with self.assertRaisesRegex(ValueError, "critical negative"):
            target.validate_inventory(
                target.ROOT, {"x": ("mvp.tests.test_evidence_class_matrix",)},
                {"tests.Science.test_science_qualification": ("test_dummy",)},
            )

    def test_source_mismatch_fail_closed_before_runner(self):
        with patch.object(target.subprocess, "run",
                          return_value=SimpleNamespace(stdout="b" * 40 + "\n")):
            with self.assertRaisesRegex(ValueError, "source SHA mismatch"):
                target.verify_source_pin(expected="a" * 40)
        with patch.object(target.subprocess, "run",
                          return_value=SimpleNamespace(stdout="a" * 40 + "\n")):
            target.verify_source_pin(expected="a" * 40)

    def test_inventory_check_is_not_execution_and_cannot_emit_pass(self):
        output = StringIO()
        with patch("sys.argv", ["qualify_plan7_engineering", "--check"]), \
             patch.object(target, "campaigns_for", return_value={"science": ("a",)}), \
             patch.object(target, "validate_inventory", return_value={"science": 7}), \
             patch.object(target, "verify_source_pin"), \
             patch.object(target.subprocess, "run", side_effect=AssertionError("must not execute")):
            with redirect_stdout(output):
                self.assertEqual(target.main(), 0)
        self.assertIn("SOURCE_INVENTORY_ONLY", output.getvalue())
        self.assertNotIn("TESTS_PASS", output.getvalue())

    def test_failed_suite_aborts_without_pass_or_next_campaign(self):
        output, error = StringIO(), StringIO()
        with patch("sys.argv", ["qualify_plan7_engineering", "--suite", "evidence-matrix"]), \
             patch.object(target, "campaigns_for",
                          return_value={"evidence-matrix": ("mvp.tests.test_evidence_class_matrix",)}), \
             patch.object(target, "validate_inventory",
                          return_value={"evidence-matrix": 11}), \
             patch.object(target, "verify_source_pin"), \
             patch.object(target.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
            with redirect_stdout(output), redirect_stderr(error):
                self.assertEqual(target.main(), 1)
        self.assertIn("PLAN7_ENGINEERING_CAMPAIGN_FAIL", error.getvalue())
        self.assertNotIn("TESTS_PASS", output.getvalue())

    def test_success_is_only_engineering_not_economic_or_release_authority(self):
        output = StringIO()
        with patch("sys.argv", ["qualify_plan7_engineering", "--suite", "evidence-matrix"]), \
             patch.object(target, "campaigns_for",
                          return_value={"evidence-matrix": ("mvp.tests.test_evidence_class_matrix",)}), \
             patch.object(target, "validate_inventory",
                          return_value={"evidence-matrix": 11}), \
             patch.object(target, "verify_source_pin"), \
             patch.object(target.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            with redirect_stdout(output):
                self.assertEqual(target.main(), 0)
        self.assertIn("ENGINEERING_TESTS_PASS_ONLY", output.getvalue())
        self.assertIn("no scientific edge", output.getvalue())

    def test_paths_reject_traversal_and_fake_modules(self):
        for module in ("../../secrets", "bad-name", "", ".top"):
            with self.subTest(module=module):
                with self.assertRaises(ValueError):
                    target.module_path(target.ROOT, module)

    def test_performance_and_host_campaign_discovery_is_nonempty_and_sorted(self):
        campaigns = target.campaigns_for()
        for name in ("performance", "target-host"):
            self.assertEqual(len(campaigns[name]), len(set(campaigns[name])))
            self.assertTrue(any("runtime_" in module for module in campaigns[name]))
