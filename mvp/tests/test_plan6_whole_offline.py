"""Negative, source-binding and restart/receipt tests for Plan 6 Section 6.

Only synthetic CompletedProcess receipts; no provider network or credentials.
The actual whole runner invokes the canonical Section 1-5 executable suites.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.plan6_offline_sections import SECTION_MODULES
from tools.qualify_plan6_whole_offline import (
    Plan6WholeQualificationError,
    capability_matrix,
    publish_receipt,
    qualify_sections,
    read_section_receipt,
    verify_source_sha,
)


SHA = "a" * 40


def result(section, *, code=0, **changes):
    receipt = {
        "schema_version": "plan6-offline-suite.v1",
        "section": section,
        "test_modules": list(dict.fromkeys(SECTION_MODULES[section])),
        "test_count": 100,
        "status": "PASS",
        "network": "DENIED",
        "provider_credentials": "ABSENT",
        "provider_account_activation": "NOT_CLAIMED",
        "paper_live": "NOT_CLAIMED",
    }
    receipt["test_count"] = max(100, len(receipt["test_modules"]))
    receipt.update(changes)
    return subprocess.CompletedProcess(
        args=["synthetic-only"], returncode=code,
        stdout=json.dumps(receipt) + "\n", stderr="",
    )


class Plan6WholeOfflineTests(unittest.TestCase):
    def test_exact_source_sha_required(self):
        self.assertEqual(verify_source_sha(expected=SHA, actual=SHA), SHA)
        for expected, actual in (
            ("", SHA), ("A" * 40, SHA), (SHA, "b" * 40),
            (SHA, SHA[:-1]), ("invalid", "invalid"),
        ):
            with self.subTest(expected=expected, actual=actual):
                with self.assertRaises(Plan6WholeQualificationError):
                    verify_source_sha(expected=expected, actual=actual)

    def test_every_section_receipt_accepts_only_matching_real_exit_zero(self):
        for section in range(1, 6):
            with self.subTest(section=section):
                self.assertEqual(
                    read_section_receipt(section, result(section))["section"],
                    section,
                )

    def test_nonzero_exit_cannot_self_assert_pass(self):
        with self.assertRaisesRegex(Plan6WholeQualificationError, "FAILED"):
            read_section_receipt(5, result(5, code=1))

    def test_forged_source_receipt_fails_closed(self):
        for change in (
            {"status": "FAIL"},
            {"network": "ALLOWED"},
            {"provider_credentials": "PRESENT"},
            {"provider_account_activation": "READY"},
            {"paper_live": "PASS"},
            {"test_count": 0},
            {"section": 4},
            {"test_modules": ["mvp.tests.test_plan6_offline_harness"]},
            {"schema_version": "fake.v1"},
        ):
            with self.subTest(change=change):
                with self.assertRaises(Plan6WholeQualificationError):
                    read_section_receipt(5, result(5, **change))

    def test_missing_malformed_and_nonfinal_receipt_rejected(self):
        for stdout in ("", "not json\n", "{}\n", '{"status":"PASS"}\nnoise\n'):
            with self.subTest(stdout=stdout):
                with self.assertRaises(Plan6WholeQualificationError):
                    read_section_receipt(
                        5, subprocess.CompletedProcess(
                            args=[], returncode=0, stdout=stdout, stderr=""
                        )
                    )

    def test_unknown_section_is_rejected_before_processing(self):
        for section in (0, 6, True, -1):
            with self.assertRaises(Plan6WholeQualificationError):
                read_section_receipt(section, result(5))

    def test_capability_matrix_separates_fixture_and_real_authority(self):
        matrix = capability_matrix()
        self.assertEqual(matrix["provider_family_fixtures"], "OFFLINE_SUPPORTED")
        for key in (
            "authenticated_real_provider_account",
            "real_provider_network_qualification",
            "paper_or_live_trading_campaign",
            "provider_backed_financial_truth",
        ):
            self.assertEqual(matrix[key], "EXTERNAL_ACTIVATION_PENDING")
        self.assertEqual(matrix["signed_release_and_physical_nvda"], "NOT_CLAIMED")

    def test_atomic_receipt_publication_replaces_old_without_partial_json(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "artifacts" / "receipt.json"
            path.parent.mkdir()
            path.write_text("OLD_FALSE_PASS", encoding="utf-8")
            publish_receipt(path, {"schema_version": "plan6-whole-offline.v1"})
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"schema_version": "plan6-whole-offline.v1"},
            )
            self.assertEqual(list(path.parent.glob(".plan6-whole-*.tmp")), [])

    def test_whole_orchestrator_consumes_all_five_real_receipts(self):
        calls = []
        def fake_run(command, **kwargs):
            calls.append(command)
            if command[:3] == ["git", "rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(command, 0, SHA + "\n", "")
            section = int(command[-1])
            return result(section)
        with patch("tools.qualify_plan6_whole_offline.subprocess.run", side_effect=fake_run):
            whole = qualify_sections(SHA)
        self.assertEqual(len(calls), 6)
        self.assertEqual([x["section"] for x in whole["section_results"]], [1, 2, 3, 4, 5])
        self.assertEqual(whole["source_sha"], SHA)
        self.assertEqual(whole["evidence_class"], "SOURCE_FIXTURE_TEST")
        self.assertEqual(whole["real_money"], "NOT_CLAIMED")

    def test_failed_section_halts_without_running_later_sections(self):
        seen = []
        def fake_run(command, **kwargs):
            if command[:3] == ["git", "rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(command, 0, SHA + "\n", "")
            section = int(command[-1])
            seen.append(section)
            return result(section, code=1 if section == 3 else 0)
        with patch("tools.qualify_plan6_whole_offline.subprocess.run", side_effect=fake_run):
            with self.assertRaises(Plan6WholeQualificationError):
                qualify_sections(SHA)
        self.assertEqual(seen, [1, 2, 3])

    def test_checkout_mismatch_refuses_all_section_execution(self):
        calls = []
        def fake_run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "b" * 40 + "\n", "")
        with patch("tools.qualify_plan6_whole_offline.subprocess.run", side_effect=fake_run):
            with self.assertRaises(Plan6WholeQualificationError):
                qualify_sections(SHA)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
