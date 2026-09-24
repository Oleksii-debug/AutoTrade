from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from tools.check_nvda_qualification import (
    NvdaQualificationError,
    validate_evidence,
)


ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = json.loads(
    (ROOT / "qualification" / "nvda" / "requirements.json").read_text(
        encoding="utf-8"
    )
)


def complete_evidence():
    return {
        "schema_version": "1.0.0",
        "method": "REAL_NVDA_KEYBOARD",
        "source_sha": "a" * 40,
        "artifact_sha256": "sha256:" + "b" * 64,
        "release_artifact": True,
        "environment": {
            "windows_version": "Windows 11 24H2",
            "assistive_technology": "NVDA",
            "nvda_version": "2026.1",
            "input_mode": "keyboard-only",
        },
        "reviewer": "qualification-reviewer",
        "observed_at": "2026-09-24T20:00:00Z",
        "workflows": [
            {
                "id": item["id"],
                "passed": True,
                "keyboard_steps": f"Keyboard-only steps for {item['id']}",
                "nvda_observation": f"Observed NVDA output for {item['id']}",
                "evidence_ref": "sha256:" + sha256(
                    item["id"].encode("utf-8")
                ).hexdigest(),
            }
            for item in REQUIREMENTS["workflows"]
        ],
    }


class NvdaQualificationGateTests(unittest.TestCase):
    def test_repository_status_truthfully_remains_unqualified(self):
        result = subprocess.run(
            [sys.executable, "tools/check_nvda_qualification.py", "--check-status"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertFalse(status["qualified"])
        self.assertEqual(status["reason"], "NO_REAL_NVDA_RELEASE_EVIDENCE")

    def test_complete_real_evidence_shape_can_qualify_exact_artifact(self):
        result = validate_evidence(complete_evidence(), REQUIREMENTS)
        self.assertTrue(result["qualified"])
        self.assertEqual(result["source_sha"], "a" * 40)
        self.assertEqual(result["artifact_sha256"], "sha256:" + "b" * 64)
        self.assertEqual(
            result["workflow_count"],
            len(REQUIREMENTS["workflows"]),
        )

    def test_missing_workflow_or_failed_workflow_is_rejected(self):
        missing = complete_evidence()
        missing["workflows"] = missing["workflows"][:-1]
        with self.assertRaisesRegex(NvdaQualificationError, "missing="):
            validate_evidence(missing, REQUIREMENTS)

        failed = complete_evidence()
        failed["workflows"][0]["passed"] = False
        with self.assertRaisesRegex(NvdaQualificationError, "did not pass"):
            validate_evidence(failed, REQUIREMENTS)

    def test_static_or_synthetic_method_cannot_claim_real_nvda(self):
        for method in (
            "STATIC_MARKUP",
            "UI_AUTOMATION_TREE",
            "SYNTHETIC_SCREEN_READER",
            "SOURCE_REVIEW",
        ):
            evidence = complete_evidence()
            evidence["method"] = method
            with self.subTest(method=method), self.assertRaisesRegex(
                NvdaQualificationError,
                "not real NVDA",
            ):
                validate_evidence(evidence, REQUIREMENTS)

    def test_wrong_assistive_technology_is_rejected(self):
        evidence = complete_evidence()
        evidence["environment"]["assistive_technology"] = "synthetic-reader"
        with self.assertRaisesRegex(NvdaQualificationError, "assistive technology"):
            validate_evidence(evidence, REQUIREMENTS)

    def test_malformed_or_duplicate_requirements_fail_closed(self):
        malformed = dict(REQUIREMENTS)
        malformed["workflows"] = [*REQUIREMENTS["workflows"], "not-an-object"]
        with self.assertRaisesRegex(NvdaQualificationError, "must be an object"):
            validate_evidence(complete_evidence(), malformed)

        duplicated = dict(REQUIREMENTS)
        duplicated["workflows"] = [*REQUIREMENTS["workflows"], REQUIREMENTS["workflows"][0]]
        evidence = complete_evidence()
        evidence["workflows"].append(dict(evidence["workflows"][0]))
        with self.assertRaisesRegex(NvdaQualificationError, "duplicate workflow evidence|must be unique"):
            validate_evidence(evidence, duplicated)

    def test_observed_at_requires_timezone_aware_iso_timestamp(self):
        for value in ("not-a-time", "2026-09-24T20:00:00"):
            evidence = complete_evidence()
            evidence["observed_at"] = value
            with self.subTest(value=value), self.assertRaisesRegex(
                NvdaQualificationError,
                "observed_at",
            ):
                validate_evidence(evidence, REQUIREMENTS)

    def test_wrong_os_mouse_input_or_nonrelease_artifact_is_rejected(self):
        cases = (
            ("windows_version", "Windows 10", "Windows 11"),
            ("input_mode", "mouse-and-keyboard", "keyboard-only"),
        )
        for field, value, message in cases:
            evidence = complete_evidence()
            evidence["environment"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(
                NvdaQualificationError,
                message,
            ):
                validate_evidence(evidence, REQUIREMENTS)

        evidence = complete_evidence()
        evidence["release_artifact"] = False
        with self.assertRaisesRegex(NvdaQualificationError, "delivered release"):
            validate_evidence(evidence, REQUIREMENTS)

    def test_workflow_evidence_refs_must_be_unique_immutable_hashes(self):
        malformed = complete_evidence()
        malformed["workflows"][0]["evidence_ref"] = "artifact://nvda/claim"
        with self.assertRaisesRegex(NvdaQualificationError, "immutable sha256"):
            validate_evidence(malformed, REQUIREMENTS)

        duplicated = complete_evidence()
        duplicated["workflows"][1]["evidence_ref"] = duplicated["workflows"][0]["evidence_ref"]
        with self.assertRaisesRegex(NvdaQualificationError, "must be unique"):
            validate_evidence(duplicated, REQUIREMENTS)

    def test_schema_versions_must_match_supported_contract(self):
        evidence = complete_evidence()
        evidence["schema_version"] = "2.0.0"
        with self.assertRaisesRegex(NvdaQualificationError, "schema_version"):
            validate_evidence(evidence, REQUIREMENTS)

    def test_exact_source_and_artifact_hashes_are_mandatory(self):
        for field, value, message in (
            ("source_sha", "main", "40-character"),
            ("artifact_sha256", "sha256:abc", "64 lowercase"),
        ):
            evidence = complete_evidence()
            evidence[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(
                NvdaQualificationError,
                message,
            ):
                validate_evidence(evidence, REQUIREMENTS)

    def test_qualified_status_cannot_escape_nvda_evidence_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "status.json"
            status.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "qualified": True,
                        "source_sha": "a" * 40,
                        "artifact_sha256": "sha256:" + "b" * 64,
                        "evidence_file": "../outside.json",
                        "evidence_sha256": "sha256:" + "c" * 64,
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/check_nvda_qualification.py",
                    "--check-status",
                    "--status",
                    str(status),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("inside qualification/nvda", result.stderr)

    def test_qualified_checked_status_requires_evidence_digest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence.json"
            evidence.write_text(
                json.dumps(complete_evidence()),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/check_nvda_qualification.py",
                    "--evidence",
                    str(evidence),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            validated = json.loads(result.stdout)
            self.assertTrue(validated["qualified"])
            self.assertTrue(validated["evidence_sha256"].startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
