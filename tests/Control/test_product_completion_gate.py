import json
from pathlib import Path
import subprocess
import sys
import unittest

from tools.check_product_completion import (
    EXPECTED_PACKAGE_IDS,
    ProductCompletionError,
    evaluate_completion,
)


ROOT = Path(__file__).resolve().parents[2]


def complete_bank():
    return {
        "schema_version": "1.0.0",
        "packages": [
            {
                "id": package_id,
                "proposed_status": "DONE",
            }
            for package_id in EXPECTED_PACKAGE_IDS
        ],
    }


def complete_qualification():
    return {
        "schema_version": "1.0.0",
        "overall_status": "FULL_PRODUCT_QUALIFIED",
        "gates": {
            "financial_runtime": "QUALIFIED",
            "providers": "QUALIFIED",
            "economic_edge": "QUALIFIED",
            "accessibility": "QUALIFIED",
            "release": "QUALIFIED",
        },
    }


class ProductCompletionGateTests(unittest.TestCase):
    def test_current_repository_cannot_be_misreported_as_complete(self):
        result = subprocess.run(
            [
                sys.executable,
                "tools/check_product_completion.py",
                "--require-complete",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["complete"])
        self.assertEqual(report["package_count"], 65)
        self.assertGreater(len(report["incomplete_packages"]), 0)
        self.assertGreater(len(report["nonterminal_gates"]), 0)
        self.assertFalse(report["nvda_qualified"])

    def test_only_explicit_terminal_states_can_report_complete(self):
        report = evaluate_completion(
            complete_bank(),
            complete_qualification(),
            {"qualified": True},
        )
        self.assertTrue(report["complete"])
        self.assertEqual(report["blockers"], [])
        self.assertEqual(report["incomplete_packages"], [])
        self.assertEqual(report["nonterminal_gates"], {})

    def test_one_unfinished_package_blocks_whole_product_completion(self):
        bank = complete_bank()
        bank["packages"][31]["proposed_status"] = "IN_PROGRESS"
        report = evaluate_completion(
            bank,
            complete_qualification(),
            {"qualified": True},
        )
        self.assertFalse(report["complete"])
        self.assertIn("WP-32", report["incomplete_packages"])

    def test_unproven_economic_edge_or_release_blocks_completion(self):
        qualification = complete_qualification()
        qualification["gates"]["economic_edge"] = "UNPROVEN"
        qualification["gates"]["release"] = "NOT_STARTED"
        report = evaluate_completion(
            complete_bank(),
            qualification,
            {"qualified": True},
        )
        self.assertFalse(report["complete"])
        self.assertEqual(
            report["nonterminal_gates"]["economic_edge"],
            "UNPROVEN",
        )
        self.assertEqual(
            report["nonterminal_gates"]["release"],
            "NOT_STARTED",
        )

    def test_missing_or_duplicate_work_package_is_protocol_error(self):
        missing = complete_bank()
        missing["packages"].pop()
        with self.assertRaisesRegex(ProductCompletionError, "exactly WP-01"):
            evaluate_completion(
                missing,
                complete_qualification(),
                {"qualified": True},
            )

        duplicated = complete_bank()
        duplicated["packages"].append(dict(duplicated["packages"][0]))
        with self.assertRaisesRegex(ProductCompletionError, "duplicate"):
            evaluate_completion(
                duplicated,
                complete_qualification(),
                {"qualified": True},
            )

    def test_nvda_must_be_real_terminal_evidence_not_truthy_text(self):
        report = evaluate_completion(
            complete_bank(),
            complete_qualification(),
            {"qualified": "true"},
        )
        self.assertFalse(report["complete"])
        self.assertFalse(report["nvda_qualified"])


if __name__ == "__main__":
    unittest.main()
