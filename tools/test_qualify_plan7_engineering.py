"""Negative/recovery checks for Plan-7 orchestration, not scientific gate issuance."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from tools import qualify_plan7_engineering as q


class Plan7OrchestratorContractTests(unittest.TestCase):
    def _sandbox(self, root, body="def test_fixture():\n    pass\n"):
        path = root / "tests" / "Science" / "test_fixture.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def _validate(self, root, **kwargs):
        campaigns = {"science": ("tests.Science.test_fixture",)}
        return q.validate_inventory(root, campaigns=campaigns, negatives={}, **kwargs)

    def test_valid_fixture_inventory_is_not_executed_test_proof(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            self._sandbox(root)
            self.assertEqual(self._validate(root), {"science": 1})

    def test_missing_suite_fails_closed(self):
        with TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "missing or symlinked"):
                self._validate(Path(folder))

    def test_empty_suite_fails_closed(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            self._sandbox(root, "def helper():\n    pass\n")
            with self.assertRaisesRegex(ValueError, "empty suite"):
                self._validate(root)

    def test_removed_critical_negative_fails_closed(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            self._sandbox(root)
            with self.assertRaisesRegex(ValueError, "required negative disappeared"):
                q.validate_inventory(
                    root,
                    campaigns={"science": ("tests.Science.test_fixture",)},
                    negatives={"tests.Science.test_fixture": ("test_recovery_refusal",)},
                )

    def test_unscheduled_critical_suite_fails_closed(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            self._sandbox(root)
            with self.assertRaisesRegex(ValueError, "critical negative suites"):
                q.validate_inventory(
                    root,
                    campaigns={"science": ("tests.Science.test_fixture",)},
                    negatives={"tests.Science.test_not_scheduled": ("test_case",)},
                )

    def test_duplicate_suite_fails_closed(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            self._sandbox(root)
            with self.assertRaisesRegex(ValueError, "duplicate suite"):
                q.validate_inventory(
                    root,
                    campaigns={"science": ("tests.Science.test_fixture",) * 2},
                    negatives={},
                )

    def test_syntax_error_is_not_accepted_as_source_inventory(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            self._sandbox(root, "def test_broken(:\n    pass\n")
            with self.assertRaises(SyntaxError):
                self._validate(root)

    def test_invalid_module_names_rejected_without_directory_escape(self):
        for name in ("../test_fixture", "tests.Science.test-file", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                q.module_path(Path("repo"), name)

    def test_missing_dynamic_negative_recovery_campaign_fails_closed(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "mvp" / "tests").mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "required dynamic campaign missing"):
                q.campaigns_for(root)

    def test_source_pin_mismatch_fails_closed(self):
        with patch.object(q.subprocess, "run", return_value=SimpleNamespace(
            stdout="a" * 40 + "\n",
        )):
            with self.assertRaisesRegex(ValueError, "source SHA mismatch"):
                q.verify_source_pin(Path("."), "b" * 40)

    def test_source_pin_must_match_exactly(self):
        with patch.object(q.subprocess, "run", return_value=SimpleNamespace(
            stdout="a" * 40 + "\n",
        )):
            q.verify_source_pin(Path("."), "a" * 40)

    def test_absent_pin_does_not_invent_a_successful_run(self):
        with patch.object(q.subprocess, "run") as execute:
            q.verify_source_pin(Path("."), None)
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
