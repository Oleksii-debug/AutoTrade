import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from tools.build_provenance_manifest import (
    _normalized_dotnet_lock,
    dependency_advisory_evidence_document,
    dotnet_lock_graph,
    dotnet_package_dependencies,
    normalize_inspected_components,
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

    def test_dotnet_package_reference_is_exact_and_lock_graph_is_canonical(self):
        self.assertEqual(
            dotnet_package_dependencies(),
            [{"name": "Velopack", "version": "1.2.158"}],
        )
        project = (
            ROOT / "src" / "AutoTrade.Desktop" / "AutoTrade.Desktop.csproj"
        ).read_text(encoding="utf-8")
        self.assertIn(
            '<PackageReference Include="Velopack" Version="[1.2.158]" />',
            project,
        )

        graph = dotnet_lock_graph()
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(manifest["dotnet_lock_graph"], graph)
        self.assertEqual(
            manifest["source_inventory"]["dotnet_lock_blob_shas"],
            {item["lock_file"]: item["lock_blob_sha"] for item in graph},
        )

        desktop = next(
            item
            for item in graph
            if item["project"]
            == "src/AutoTrade.Desktop/AutoTrade.Desktop.csproj"
        )
        velopack = desktop["dependencies"]["net10.0-windows7.0"]["Velopack"]
        self.assertEqual(velopack["requested"], "[1.2.158]")
        self.assertEqual(velopack["resolved"], "1.2.158")
        self.assertTrue(velopack["contentHash"])

    def test_lock_resolution_or_content_hash_change_changes_dependency_graph_identity(self):
        with TemporaryDirectory() as directory:
            lock = Path(directory) / "packages.lock.json"
            base = {
                "version": 1,
                "dependencies": {
                    "net10.0": {
                        "Example.Package": {
                            "type": "Direct",
                            "requested": "[1.2.3]",
                            "resolved": "1.2.3",
                            "contentHash": "first-hash",
                        }
                    }
                },
            }
            lock.write_text(json.dumps(base), encoding="utf-8")
            first = _normalized_dotnet_lock(lock)

            changed_hash = json.loads(json.dumps(base))
            changed_hash["dependencies"]["net10.0"]["Example.Package"][
                "contentHash"
            ] = "second-hash"
            lock.write_text(json.dumps(changed_hash), encoding="utf-8")
            second = _normalized_dotnet_lock(lock)
            self.assertNotEqual(first, second)

            changed_resolution = json.loads(json.dumps(base))
            changed_resolution["dependencies"]["net10.0"]["Example.Package"][
                "resolved"
            ] = "1.2.4"
            lock.write_text(json.dumps(changed_resolution), encoding="utf-8")
            third = _normalized_dotnet_lock(lock)
            self.assertNotEqual(first, third)

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
                        "sha256": "sha256:" + "c" * 64,
                        "observed_at": "2026-09-24T20:00:01Z",
                    },
                ],
                [{
                    "artifact_id": "qualification-impossible-time",
                    "sha256": "sha256:" + "d" * 64,
                    "observed_at": "2026-99-99T20:00:00Z",
                }],
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

    def test_component_provenance_requires_canonical_unique_source_identity(self):
        base = {
            "name": "Component A",
            "repository": "owner/repo",
            "revision": "a" * 40,
            "license": "MIT",
            "adoption_state": "CANDIDATE",
            "source_import_allowed": "AFTER_QUALIFICATION",
            "release_distribution_state": "BLOCKED",
        }
        components, unresolved = normalize_inspected_components(
            {"components": [base]}
        )
        self.assertEqual(components[0]["revision"], "a" * 40)
        self.assertEqual(unresolved, [])

        invalid_documents = (
            {"components": [{**base, "revision": "main"}]},
            {"components": [{**base, "revision": "A" * 40}]},
            {"components": [{**base, "repository": "owner-only"}]},
            {"components": [{**base, "name": " Component A"}]},
            {"components": [base, {**base, "repository": "other/repo", "revision": "b" * 64}]},
            {
                "components": [
                    base,
                    {
                        **base,
                        "name": "Component B",
                    },
                ]
            },
        )
        for document in invalid_documents:
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    normalize_inspected_components(document)

    def test_component_provenance_accepts_sha256_git_object_identity(self):
        component = {
            "name": "Component SHA256",
            "repository": "owner/repo",
            "revision": "c" * 64,
            "license": "Apache-2.0",
            "adoption_state": "QUALIFICATION_PENDING",
            "source_import_allowed": "AFTER_QUALIFICATION",
            "release_distribution_state": "BLOCKED",
        }
        components, unresolved = normalize_inspected_components(
            {"components": [component]}
        )
        self.assertEqual(components[0]["revision"], "c" * 64)
        self.assertEqual(unresolved, [])

    def test_machine_release_distribution_state_is_preserved_and_blocks_manifest(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        inspected = manifest["inspected_components"]
        self.assertTrue(inspected)
        self.assertTrue(
            all(
                component["release_distribution_state"] == "BLOCKED"
                for component in inspected
            )
        )
        blockers = {
            item["code"]: item
            for item in manifest["blocking_issues"]
        }
        release_blocker = blockers["COMPONENT_RELEASE_DISTRIBUTION_BLOCKED"]
        self.assertEqual(
            release_blocker["components"],
            sorted(component["name"] for component in inspected),
        )

    def test_component_provenance_rejects_missing_or_invalid_release_state(self):
        base = {
            "name": "Component State",
            "repository": "owner/state",
            "revision": "d" * 40,
            "license": "MIT",
            "adoption_state": "CANDIDATE",
            "source_import_allowed": "AFTER_QUALIFICATION",
        }
        with self.assertRaisesRegex(
            ValueError,
            "release_distribution_state",
        ):
            normalize_inspected_components({"components": [base]})

        for invalid in ("READY", "approved", "BLOCKED ", ""):
            with self.subTest(invalid=invalid):
                candidate = dict(base)
                candidate["release_distribution_state"] = invalid
                with self.assertRaises(ValueError):
                    normalize_inspected_components({"components": [candidate]})

    def test_release_approved_component_requires_resolved_rights_and_three_digests(self):
        approved = {
            "name": "Component Approved",
            "repository": "owner/approved",
            "revision": "e" * 40,
            "license": "MIT",
            "adoption_state": "QUALIFIED",
            "source_import_allowed": "QUALIFIED",
            "release_distribution_state": "APPROVED",
        }

        unresolved = dict(approved)
        unresolved["license"] = "UNRESOLVED_FIRST_PARTY_RIGHTS_RECORD"
        with self.assertRaisesRegex(ValueError, "unresolved license"):
            normalize_inspected_components({"components": [unresolved]})

        fields = (
            "dependency_graph_sha256",
            "notice_sha256",
            "advisory_review_sha256",
        )
        for missing in fields:
            with self.subTest(missing=missing):
                candidate = dict(approved)
                for field in fields:
                    candidate[field] = "sha256:" + ("1" * 64)
                del candidate[missing]
                with self.assertRaisesRegex(ValueError, missing):
                    normalize_inspected_components({"components": [candidate]})

        accepted = dict(approved)
        for index, field in enumerate(fields, start=1):
            accepted[field] = "sha256:" + (str(index) * 64)
        components, unresolved_names = normalize_inspected_components(
            {"components": [accepted]}
        )
        self.assertEqual(unresolved_names, [])
        self.assertEqual(components[0]["release_distribution_state"], "APPROVED")
        for field in fields:
            self.assertEqual(components[0][field], accepted[field])

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
        self.assertTrue(
            all(
                candidate["release_distribution_state"] == "BLOCKED"
                for candidate in candidates.values()
            )
        )

    def test_no_secret_like_material_is_recorded(self):
        text = MANIFEST.read_text(encoding="utf-8").lower()
        for forbidden in ("api_key", "client_secret", "access_token", "private_key"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
