from pathlib import Path
import unittest

from tools.whole_product_gate import (
    EvidenceRecord,
    WholeProductGateError,
    evaluate_whole_product,
    load_canonical,
    product_section_ids,
    work_package_ids,
)

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40


class WholeProductGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec, cls.bank = load_canonical(ROOT)

    def test_canonical_scope_is_exactly_40_sections_and_65_packages(self):
        self.assertEqual(len(product_section_ids(self.spec)), 40)
        self.assertEqual(len(work_package_ids(self.bank)), 65)

    def test_empty_evidence_cannot_be_called_complete(self):
        verdict = evaluate_whole_product(
            spec_text=self.spec,
            bank_text=self.bank,
            evidence=(),
            exact_source_sha=SHA,
        )
        self.assertEqual(verdict.status, "INCOMPLETE")
        self.assertEqual(len(verdict.missing_sections), 40)
        self.assertEqual(len(verdict.missing_packages), 65)

    def test_green_subset_is_not_whole_product_completion(self):
        evidence = [
            EvidenceRecord("PRODUCT_SECTION", "SECTION-01", SHA, "PASS", "proof/section-01"),
            EvidenceRecord("WORK_PACKAGE", "WP-01", SHA, "PASS", "proof/wp-01"),
        ]
        verdict = evaluate_whole_product(
            spec_text=self.spec,
            bank_text=self.bank,
            evidence=evidence,
            exact_source_sha=SHA,
        )
        self.assertEqual(verdict.status, "INCOMPLETE")

    def test_blocked_or_stale_evidence_fails_closed(self):
        records = []
        for section in product_section_ids(self.spec):
            records.append(EvidenceRecord("PRODUCT_SECTION", section, SHA, "PASS", f"p/{section}"))
        for package in work_package_ids(self.bank):
            records.append(EvidenceRecord("WORK_PACKAGE", package, SHA, "PASS", f"p/{package}"))
        records[0] = EvidenceRecord("PRODUCT_SECTION", "SECTION-01", SHA, "BLOCKED", "p/blocked")
        records[40] = EvidenceRecord("WORK_PACKAGE", "WP-01", "b" * 40, "PASS", "p/stale")
        verdict = evaluate_whole_product(
            spec_text=self.spec,
            bank_text=self.bank,
            evidence=records,
            exact_source_sha=SHA,
        )
        self.assertEqual(verdict.status, "INCOMPLETE")
        self.assertEqual(len(verdict.nonpassing), 2)

    def test_only_exact_complete_matrix_can_pass(self):
        records = [
            EvidenceRecord("PRODUCT_SECTION", section, SHA, "PASS", f"p/{section}")
            for section in product_section_ids(self.spec)
        ] + [
            EvidenceRecord("WORK_PACKAGE", package, SHA, "PASS", f"p/{package}")
            for package in work_package_ids(self.bank)
        ]
        verdict = evaluate_whole_product(
            spec_text=self.spec,
            bank_text=self.bank,
            evidence=records,
            exact_source_sha=SHA,
        )
        self.assertEqual(verdict.status, "COMPLETE")
        self.assertEqual(verdict.missing_sections, ())
        self.assertEqual(verdict.missing_packages, ())
        self.assertEqual(verdict.nonpassing, ())

    def test_duplicate_requirement_is_rejected(self):
        record = EvidenceRecord("WORK_PACKAGE", "WP-01", SHA, "PASS", "proof")
        with self.assertRaisesRegex(WholeProductGateError, "duplicate"):
            evaluate_whole_product(
                spec_text=self.spec,
                bank_text=self.bank,
                evidence=[record, record],
                exact_source_sha=SHA,
            )


if __name__ == "__main__":
    unittest.main()
