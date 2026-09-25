import json
from pathlib import Path
import subprocess
import sys
import unittest

from tools.check_product_completion import (
    EXPECTED_GATE_NAMES,
    EXPECTED_PACKAGE_IDS,
    EXPECTED_SECTION_IDS,
    ProductCompletionError,
    evaluate_completion,
)


ROOT = Path(__file__).resolve().parents[2]
SPEC = (ROOT / "docs/product/PRODUCT_SPEC_CANONICAL.txt").read_text(encoding="utf-8")
SHA = "a" * 40


def complete_bank():
    return {
        "schema_version": "1.0.0",
        "packages": [
            {"id": package_id, "proposed_status": "DONE"}
            for package_id in EXPECTED_PACKAGE_IDS
        ],
    }


def complete_evidence():
    records = [
        {
            "kind": "PRODUCT_SECTION",
            "requirement_id": section,
            "source_sha": SHA,
            "status": "PASS",
            "evidence_ref": f"artifact://whole-product/{section.lower()}",
        }
        for section in EXPECTED_SECTION_IDS
    ]
    records.extend(
        {
            "kind": "WORK_PACKAGE",
            "requirement_id": package,
            "source_sha": SHA,
            "status": "PASS",
            "evidence_ref": f"artifact://whole-product/{package.lower()}",
        }
        for package in EXPECTED_PACKAGE_IDS
    )
    return records


def complete_qualification():
    return {
        "schema_version": "2.0.0",
        "source_sha": SHA,
        "overall_status": "FULL_PRODUCT_QUALIFIED",
        "gates": {name: "QUALIFIED" for name in EXPECTED_GATE_NAMES},
        "whole_product_evidence": complete_evidence(),
    }


def nvda(*, qualified=True, source_sha=SHA):
    return {"qualified": qualified, "source_sha": source_sha}


def evaluate(bank=None, qualification=None, nvda_status=None, source_sha=SHA):
    return evaluate_completion(
        bank or complete_bank(),
        qualification or complete_qualification(),
        nvda_status or nvda(),
        spec_text=SPEC,
        exact_source_sha=source_sha,
    )


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
        self.assertEqual(report["product_section_count"], 40)
        self.assertEqual(report["package_count"], 65)
        self.assertGreater(len(report["incomplete_packages"]), 0)
        self.assertGreater(len(report["nonterminal_gates"]), 0)
        self.assertFalse(report["nvda_qualified"])

    def test_only_exact_complete_matrix_can_report_complete(self):
        report = evaluate()
        self.assertTrue(report["complete"])
        self.assertEqual(report["blockers"], [])
        self.assertEqual(report["missing_section_evidence"], [])
        self.assertEqual(report["missing_package_evidence"], [])
        self.assertEqual(report["nonpassing_evidence"], [])
        self.assertTrue(report["nvda_source_matches"])
        self.assertTrue(report["qualification_source_matches"])

    def test_one_unfinished_package_blocks_whole_product_completion(self):
        bank = complete_bank()
        bank["packages"][31]["proposed_status"] = "IN_PROGRESS"
        report = evaluate(bank=bank)
        self.assertFalse(report["complete"])
        self.assertIn("WP-32", report["incomplete_packages"])

    def test_missing_required_gate_cannot_disappear_from_completion_scope(self):
        qualification = complete_qualification()
        qualification["gates"].pop("economic_edge")
        report = evaluate(qualification=qualification)
        self.assertFalse(report["complete"])
        self.assertIn("economic_edge", report["missing_required_gates"])

    def test_unproven_economic_edge_or_release_blocks_completion(self):
        qualification = complete_qualification()
        qualification["gates"]["economic_edge"] = "UNPROVEN"
        qualification["gates"]["release"] = "NOT_STARTED"
        report = evaluate(qualification=qualification)
        self.assertFalse(report["complete"])
        self.assertEqual(report["nonterminal_gates"]["economic_edge"], "UNPROVEN")
        self.assertEqual(report["nonterminal_gates"]["release"], "NOT_STARTED")

    def test_missing_or_duplicate_work_package_is_protocol_error(self):
        missing = complete_bank()
        missing["packages"].pop()
        with self.assertRaisesRegex(ProductCompletionError, "exactly WP-01"):
            evaluate(bank=missing)

        duplicated = complete_bank()
        duplicated["packages"].append(dict(duplicated["packages"][0]))
        with self.assertRaisesRegex(ProductCompletionError, "duplicate"):
            evaluate(bank=duplicated)

    def test_each_product_section_and_package_needs_exact_source_pass_evidence(self):
        qualification = complete_qualification()
        qualification["whole_product_evidence"] = [
            item
            for item in qualification["whole_product_evidence"]
            if not (
                item["kind"] == "PRODUCT_SECTION"
                and item["requirement_id"] == "SECTION-07"
            )
        ]
        package = next(
            item
            for item in qualification["whole_product_evidence"]
            if item["kind"] == "WORK_PACKAGE" and item["requirement_id"] == "WP-33"
        )
        package["source_sha"] = "b" * 40
        report = evaluate(qualification=qualification)
        self.assertFalse(report["complete"])
        self.assertIn("SECTION-07", report["missing_section_evidence"])
        self.assertIn(
            "WORK_PACKAGE:WP-33:source_sha",
            report["nonpassing_evidence"],
        )

    def test_duplicate_or_unknown_requirement_evidence_is_protocol_error(self):
        qualification = complete_qualification()
        qualification["whole_product_evidence"].append(
            dict(qualification["whole_product_evidence"][0])
        )
        with self.assertRaisesRegex(ProductCompletionError, "duplicate"):
            evaluate(qualification=qualification)

        qualification = complete_qualification()
        qualification["whole_product_evidence"][0]["requirement_id"] = "SECTION-99"
        with self.assertRaisesRegex(ProductCompletionError, "unknown requirement"):
            evaluate(qualification=qualification)

    def test_nvda_must_be_real_terminal_evidence_on_same_source(self):
        report = evaluate(nvda_status={"qualified": "true", "source_sha": SHA})
        self.assertFalse(report["complete"])
        self.assertFalse(report["nvda_qualified"])

        report = evaluate(nvda_status=nvda(source_sha="b" * 40))
        self.assertFalse(report["complete"])
        self.assertFalse(report["nvda_source_matches"])

    def test_missing_or_noncanonical_exact_source_sha_blocks_completion(self):
        report = evaluate(source_sha=None)
        self.assertFalse(report["complete"])
        self.assertIsNone(report["exact_source_sha"])
        self.assertTrue(
            any("exact source SHA" in blocker for blocker in report["blockers"])
        )

    def test_qualification_cannot_self_assert_or_mismatch_exact_source(self):
        qualification = complete_qualification()
        qualification["source_sha"] = "b" * 40
        report = evaluate(qualification=qualification, source_sha=SHA)
        self.assertFalse(report["complete"])
        self.assertFalse(report["qualification_source_matches"])
        self.assertIn(
            "qualification source SHA is missing, non-canonical, or not the exact source SHA",
            report["blockers"],
        )

        qualification["source_sha"] = "NOT_A_SHA"
        report = evaluate(qualification=qualification, source_sha=SHA)
        self.assertFalse(report["qualification_source_matches"])


if __name__ == "__main__":
    unittest.main()
