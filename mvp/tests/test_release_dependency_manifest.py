import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from tools.build_provenance_manifest import (
    dependency_advisory_evidence_document,
    release_evidence_document,
)


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "provenance" / "release-dependency-manifest.json"


class ReleaseDependencyManifestTests(unittest.TestCase):
    def test_checked_manifest_is_deterministic_and_current(self):
        result = subprocess.run(
            [sys.executable, "tools/build_provenance_manifest.py", "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_current_release_is_fail_closed_on_known_rights_and_composition_gaps(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertFalse(manifest["release_eligible"])
        codes = {item["code"] for item in manifest["blocking_issues"]}
        self.assertIn("RELEASE_COMPOSITION_MISSING", codes)
        self.assertIn("MODEL_DATA_RIGHTS_MISSING", codes)
        self.assertIn("FIRST_PARTY_RIGHTS_UNRESOLVED", codes)
        self.assertIn("DEPENDENCY_ADVISORY_EVIDENCE_MISSING", codes)

    def test_all_python_development_dependencies_are_exactly_versioned(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        dependencies = manifest["python_development_dependencies"]
        self.assertTrue(dependencies)
        for item in dependencies:
            self.assertTrue(item["name"])
            self.assertRegex(item["version"], r"^[0-9][A-Za-z0-9.+-]*$")
            self.assertNotIn("*", item["version"])

    def test_empty_or_assertion_only_release_evidence_cannot_remove_blocker(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence.json"
            for document, expected_reason in (
                ({}, "not_qualified"),
                ({"qualified": True}, "missing_schema_version"),
                (
                    {
                        "qualified": True,
                        "schema_version": "1.0.0",
                        "source_sha": "a" * 40,
                        "evidence_refs": [],
                    },
                    "missing_evidence_refs",
                ),
            ):
                evidence.write_text(json.dumps(document), encoding="utf-8")
                qualified, reason = release_evidence_document(
                    evidence,
                    label="test evidence",
                )
                self.assertFalse(qualified)
                self.assertEqual(reason, expected_reason)

            evidence.write_text(
                json.dumps(
                    {
                        "qualified": True,
                        "schema_version": "1.0.0",
                        "source_sha": "a" * 40,
                        "evidence_refs": [{
                            "artifact_id": "qualification-1",
                            "sha256": "sha256:" + "b" * 64,
                            "observed_at": "2026-09-24T20:00:00Z",
                        }],
                    }
                ),
                encoding="utf-8",
            )
            qualified, reason = release_evidence_document(
                evidence,
                label="test evidence",
            )
            self.assertTrue(qualified)
            self.assertIsNone(reason)


    def test_release_evidence_cannot_be_reused_for_another_candidate_sha(self):
        with TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence.json"
            evidence.write_text(
                json.dumps(
                    {
                        "qualified": True,
                        "schema_version": "1.0.0",
                        "source_sha": "a" * 40,
                        "evidence_refs": [{
                            "artifact_id": "qualification-1",
                            "sha256": "sha256:" + "b" * 64,
                            "observed_at": "2026-09-24T20:00:00Z",
                        }],
                    }
                ),
                encoding="utf-8",
            )
            qualified, reason = release_evidence_document(
                evidence,
                label="candidate evidence",
                expected_source_sha="c" * 40,
            )
            self.assertFalse(qualified)
            self.assertEqual(reason, "source_sha_mismatch")

            qualified, reason = release_evidence_document(
                evidence,
                label="candidate evidence",
                expected_source_sha="a" * 40,
            )
            self.assertTrue(qualified)
            self.assertIsNone(reason)

    def test_opaque_or_mutable_evidence_refs_cannot_qualify_release(self):
        with TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence.json"
            base = {
                "qualified": True,
                "schema_version": "1.0.0",
                "source_sha": "a" * 40,
            }
            invalid_refs = (
                ["artifact://qualification/1"],
                [{
                    "artifact_id": "qualification-1",
                    "sha256": "sha256:" + "b" * 63,
                    "observed_at": "2026-09-24T20:00:00Z",
                }],
                [{
                    "artifact_id": "qualification-1",
                    "sha256": "sha256:" + "B" * 64,
                    "observed_at": "2026-09-24T20:00:00Z",
                }],
                [{
                    "artifact_id": "qualification-1",
                    "sha256": "sha256:" + "b" * 64,
                    "observed_at": "2026-09-24T20:00:00+02:00",
                }],
                [
                    {
                        "artifact_id": "qualification-1",
                        "sha256": "sha256:" + "b" * 64,
                        "observed_at": "2026-09-24T20:00:00Z",
                    },
                    {
                        "artifact_id": " qualification-1 ",
                        "sha256": "sha256:" + "b" * 64,
                        "observed_at": "2026-09-24T20:00:01Z",
                    },
                ],
            )
            for refs in invalid_refs:
                with self.subTest(refs=refs):
                    evidence.write_text(
                        json.dumps({**base, "evidence_refs": refs}),
                        encoding="utf-8",
                    )
                    qualified, reason = release_evidence_document(
                        evidence,
                        label="test evidence",
                    )
                    self.assertFalse(qualified)
                    self.assertEqual(reason, "invalid_evidence_refs")

    def test_advisory_evidence_for_another_dependency_graph_is_rejected(self):
        expected_graph = {
            "python_development_dependencies": [{"name": "attrs", "version": "26.1.0"}],
            "dotnet_package_dependencies": [],
            "inspected_components": [],
        }
        with TemporaryDirectory() as directory:
            evidence = Path(directory) / "advisories.json"
            base = {
                "qualified": True,
                "schema_version": "1.0.0",
                "source_sha": "a" * 40,
                "evidence_refs": [{
                    "artifact_id": "advisories-1",
                    "sha256": "sha256:" + "c" * 64,
                    "observed_at": "2026-09-24T20:00:00Z",
                }],
            }
            evidence.write_text(json.dumps(base), encoding="utf-8")
            qualified, reason = dependency_advisory_evidence_document(
                evidence,
                expected_dependency_graph=expected_graph,
            )
            self.assertFalse(qualified)
            self.assertEqual(reason, "dependency_graph_mismatch")

            evidence.write_text(
                json.dumps({**base, "dependency_graph": expected_graph}),
                encoding="utf-8",
            )
            qualified, reason = dependency_advisory_evidence_document(
                evidence,
                expected_dependency_graph=expected_graph,
                expected_source_sha="a" * 40,
            )
            self.assertTrue(qualified)
            self.assertIsNone(reason)

            qualified, reason = dependency_advisory_evidence_document(
                evidence,
                expected_dependency_graph=expected_graph,
                expected_source_sha="b" * 40,
            )
            self.assertFalse(qualified)
            self.assertEqual(reason, "source_sha_mismatch")

    def test_dependency_manifest_never_upgrades_candidate_to_release_approval(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        candidates = {
            item["name"]: item
            for item in manifest["inspected_components"]
        }
        self.assertIn("PENDING", candidates["QuantConnect LEAN"]["source_import_allowed"])
        self.assertIn(
            "AFTER_EXACT_GRAPH_QUALIFICATION",
            candidates["WhiteBit.Net"]["source_import_allowed"],
        )
        self.assertTrue(
            candidates["Autosport first-party source"]["license"].startswith("UNRESOLVED_")
        )

    def test_no_secret_like_material_is_recorded(self):
        text = MANIFEST.read_text(encoding="utf-8").lower()
        for forbidden in ("api_key", "client_secret", "access_token", "private_key"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
