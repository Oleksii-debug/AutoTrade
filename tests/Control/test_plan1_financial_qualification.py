"""Adversarial fixtures for the Plan-1-only qualification orchestrator."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.qualify_plan1_financial import (
    command_for, path_for, validate_inventory,
)


class Plan1QualificationInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.module = "mvp.tests.test_fixture"
        self.path = path_for(self.root, self.module)
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            "def test_invariant(): pass\n"
            "def test_fail_closed_negative(): pass\n",
            encoding="utf-8",
        )
        self.campaigns = {"financial": (self.module,)}
        self.negatives = {self.module: ("test_fail_closed_negative",)}

    def test_valid_inventory_is_only_source_not_execution_evidence(self):
        self.assertEqual(
            validate_inventory(self.root, self.campaigns, self.negatives),
            {"financial": 2},
        )

    def test_missing_mandatory_negative_rejected(self):
        self.path.write_text("def test_invariant(): pass\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "lost negative coverage"):
            validate_inventory(self.root, self.campaigns, self.negatives)

    def test_missing_or_empty_suite_rejected(self):
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, "missing or symlinked"):
            validate_inventory(self.root, self.campaigns, self.negatives)
        self.path.write_text("VALUE = 1\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "empty qualification suite"):
            validate_inventory(self.root, self.campaigns, self.negatives)

    def test_unexecuted_negative_source_rejected(self):
        with self.assertRaisesRegex(ValueError, "unexecuted suite"):
            validate_inventory(self.root, self.campaigns,
                               {"mvp.tests.absent": ("test_forgery",)})

    def test_symlinked_suite_fails_closed(self):
        target = self.root / "foreign.py"
        target.write_text("def test_fail_closed_negative(): pass\n", encoding="utf-8")
        self.path.unlink()
        try:
            self.path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not available")
        with self.assertRaisesRegex(ValueError, "missing or symlinked"):
            validate_inventory(self.root, self.campaigns, self.negatives)

    def test_bad_dotted_module_name_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid qualification module"):
            path_for(self.root, "../foreign")

    def test_portfolio_runs_pytest_and_science_uses_existing_discovery(self):
        self.assertIn("pytest", command_for("portfolio", ("mvp.tests.test_fixture",)))
        self.assertIn("test_strategy_economics*.py", command_for("science", (self.module,)))
        self.assertIn("unittest", command_for("financial", (self.module,)))


if __name__ == "__main__":
    unittest.main()
