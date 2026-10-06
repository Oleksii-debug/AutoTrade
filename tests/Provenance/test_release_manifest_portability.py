from hashlib import sha1
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.build_provenance_manifest import (
    OUTPUT,
    ROOT,
    ci_action_dependencies,
    dependency_advisory_evidence_document,
    git_blob_sha,
    python_runtime_dependencies,
    rendered_manifest,
)


class ReleaseManifestPortabilityTests(unittest.TestCase):
    def test_repository_text_blob_sha_is_identical_for_lf_and_crlf_checkout(self):
        canonical = b"alpha\nbeta\n"
        expected = sha1(
            b"blob " + str(len(canonical)).encode("ascii") + b"\0" + canonical
        ).hexdigest()

        provenance_root = ROOT / "provenance"
        with TemporaryDirectory(dir=provenance_root) as directory:
            root = Path(directory)
            lf = root / "lf.txt"
            crlf = root / "crlf.txt"
            lf.write_bytes(canonical)
            crlf.write_bytes(canonical.replace(b"\n", b"\r\n"))

            self.assertEqual(git_blob_sha(lf), expected)
            self.assertEqual(git_blob_sha(crlf), expected)
            self.assertEqual(git_blob_sha(lf), git_blob_sha(crlf))

    def test_real_content_change_changes_blob_identity(self):
        with TemporaryDirectory(dir=ROOT / "provenance") as directory:
            path = Path(directory) / "content.txt"
            path.write_bytes(b"alpha\nbeta\n")
            original = git_blob_sha(path)
            path.write_bytes(b"alpha\ngamma\n")
            self.assertNotEqual(git_blob_sha(path), original)

    def test_unavailable_or_outside_repository_path_fails_closed(self):
        missing = ROOT / "provenance" / ".missing-provenance-input"
        with self.assertRaisesRegex(ValueError, "unavailable"):
            git_blob_sha(missing)

        with TemporaryDirectory() as directory:
            outside = Path(directory) / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "inside repository"):
                git_blob_sha(outside)

    def test_manifest_records_hash_locked_python_artifacts(self):
        document = json.loads(rendered_manifest())
        dependencies = document["python_development_dependencies"]
        self.assertGreater(len(dependencies), 0)
        for dependency in dependencies:
            self.assertGreater(len(dependency["hashes"]), 0)
            self.assertTrue(
                all(
                    hash_value.startswith("sha256:") and len(hash_value) == 71
                    for hash_value in dependency["hashes"]
                )
            )

    def test_python_runtime_dependency_rejects_non_string_root_version(self):
        root_pyproject = ROOT / "pyproject.toml"
        original = root_pyproject.read_text(encoding="utf-8")
        mutated = original.replace('version = "0.0.1"', "version = 1", 1)
        self.assertNotEqual(mutated, original)
        original_read_text = Path.read_text

        def read_text(candidate: Path, *args, **kwargs):
            if candidate == root_pyproject:
                return mutated
            return original_read_text(candidate, *args, **kwargs)

        with patch.object(Path, "read_text", read_text):
            with self.assertRaisesRegex(ValueError, "identity is not exact"):
                python_runtime_dependencies()

    def test_manifest_records_source_bound_python_runtime_dependency(self):
        document = json.loads(rendered_manifest())
        self.assertEqual(
            document["python_runtime_dependencies"],
            [
                {
                    "name": "autotrade-exact-numeric",
                    "source": "repository-root",
                    "version": "0.0.1",
                }
            ],
        )
        self.assertEqual(
            document["source_inventory"]["root_pyproject_blob_sha"],
            git_blob_sha(ROOT / "pyproject.toml"),
        )

    def test_manifest_records_immutable_ci_action_dependencies(self):
        document = json.loads(rendered_manifest())
        self.assertEqual(
            document["ci_action_dependencies"],
            ci_action_dependencies(),
        )
        self.assertGreater(len(document["ci_action_dependencies"]), 0)
        for dependency in document["ci_action_dependencies"]:
            self.assertRegex(dependency["revision"], r"^(?:[0-9a-f]{40}|sha256:[0-9a-f]{64})$")
            self.assertGreater(len(dependency["workflow_blob_shas"]), 0)
            for binding in dependency["workflow_blob_shas"]:
                self.assertEqual(
                    binding["blob_sha"],
                    git_blob_sha(ROOT / binding["path"]),
                )

    def test_ci_action_dependency_rejects_mutable_action_tag(self):
        original_glob = Path.glob
        original_read_text = Path.read_text
        with TemporaryDirectory() as directory:
            workflow = Path(directory) / "mutable.yml"
            workflow.write_text(
                "steps:\n  - uses: actions/checkout@v4\n",
                encoding="utf-8",
            )

            def glob(candidate: Path, pattern: str):
                if candidate == ROOT / ".github" / "workflows":
                    return iter([workflow])
                return original_glob(candidate, pattern)

            def read_text(candidate: Path, *args, **kwargs):
                if candidate == workflow:
                    return "steps:\n  - uses: actions/checkout@v4\n"
                return original_read_text(candidate, *args, **kwargs)

            with patch.object(Path, "glob", glob), patch.object(Path, "read_text", read_text):
                with self.assertRaisesRegex(ValueError, "CI action is not immutable"):
                    ci_action_dependencies()

    def test_manifest_binds_research_dependency_entrypoint(self):
        document = json.loads(rendered_manifest())
        self.assertEqual(
            document["source_inventory"]["research_pyproject_blob_sha"],
            git_blob_sha(ROOT / "research" / "pyproject.toml"),
        )

    def test_manifest_binds_nuget_package_rights_policy(self):
        document = json.loads(rendered_manifest())
        self.assertEqual(document["dotnet_package_rights"], [])
        self.assertEqual(
            document["source_inventory"]["dotnet_package_rights_blob_sha"],
            git_blob_sha(ROOT / "provenance" / "dotnet-package-rights.json"),
        )
        self.assertFalse(
            any(
                issue.get("code") == "DOTNET_PACKAGE_RIGHTS_UNQUALIFIED"
                for issue in document["blocking_issues"]
            )
        )

    def _advisory_fixture(self):
        graph = {
            "python_development_dependencies": [
                {"name": "attrs", "version": "26.1.0", "hashes": ["sha256:" + "1" * 64]}
            ],
            "python_runtime_dependencies": [
                {
                    "name": "autotrade-exact-numeric",
                    "version": "0.0.1",
                    "source": "repository-root",
                }
            ],
            "ci_action_dependencies": [
                {
                    "name": "actions/checkout",
                    "revision": "c" * 40,
                    "workflow_blob_shas": [
                        {
                            "path": ".github/workflows/example.yml",
                            "blob_sha": "d" * 40,
                        }
                    ],
                }
            ],
            "dotnet_package_dependencies": [
                {"name": "Example.Package", "version": "1.2.3"}
            ],
            "inspected_components": [
                {"repository": "owner/repo", "revision": "a" * 40}
            ],
        }
        policy = {
            "artifact_id": "policy-1",
            "sha256": "sha256:" + "2" * 64,
            "observed_at": "2026-09-28T20:00:00Z",
        }
        source = {
            "artifact_id": "advisory-db-1",
            "sha256": "sha256:" + "3" * 64,
            "observed_at": "2026-09-28T20:01:00Z",
        }
        document = {
            "qualified": True,
            "schema_version": "1.0.0",
            "source_sha": "b" * 40,
            "evidence_refs": [policy, source],
            "dependency_graph": graph,
            "review_policy_evidence": policy,
            "advisory_source_evidence": [source],
            "reviewed_components": [
                "ci-action:actions/checkout@" + "c" * 40,
                "nuget:Example.Package@1.2.3",
                "python:attrs==26.1.0",
                "python-runtime:autotrade-exact-numeric==0.0.1",
                "source:owner/repo@" + "a" * 40,
            ],
            "blocking_findings": [],
            "residual_risks": [],
        }
        return graph, document

    def _write_advisory(self, root, document):
        path = Path(root) / "dependency-advisory-qualification.json"
        path.write_text(
            json.dumps(document, sort_keys=True),
            encoding="utf-8",
        )
        return path

    def test_advisory_qualification_requires_complete_policy_and_source_authority(self):
        graph, document = self._advisory_fixture()
        with TemporaryDirectory() as directory:
            path = self._write_advisory(directory, document)
            self.assertEqual(
                dependency_advisory_evidence_document(
                    path,
                    expected_dependency_graph=graph,
                    expected_source_sha="b" * 40,
                ),
                (False, "authenticated_trust_required"),
            )

            legacy = dict(document)
            legacy.pop("review_policy_evidence")
            path = self._write_advisory(directory, legacy)
            self.assertEqual(
                dependency_advisory_evidence_document(
                    path,
                    expected_dependency_graph=graph,
                    expected_source_sha="b" * 40,
                ),
                (False, "missing_review_policy_evidence"),
            )

            unbound_policy = dict(document)
            unbound_policy["review_policy_evidence"] = {
                "artifact_id": "policy-2",
                "sha256": "sha256:" + "4" * 64,
                "observed_at": "2026-09-28T20:02:00Z",
            }
            path = self._write_advisory(directory, unbound_policy)
            self.assertEqual(
                dependency_advisory_evidence_document(
                    path,
                    expected_dependency_graph=graph,
                    expected_source_sha="b" * 40,
                ),
                (False, "unbound_review_policy_evidence"),
            )

    def test_shape_valid_candidate_advisory_cannot_mint_release_pass(self):
        graph, document = self._advisory_fixture()
        with TemporaryDirectory() as directory:
            path = self._write_advisory(directory, document)
            qualified, reason = dependency_advisory_evidence_document(
                path,
                expected_dependency_graph=graph,
                expected_source_sha="b" * 40,
            )
            self.assertFalse(qualified)
            self.assertEqual(reason, "authenticated_trust_required")

    def test_advisory_nested_evidence_must_match_top_level_digest_and_time(self):
        graph, document = self._advisory_fixture()
        with TemporaryDirectory() as directory:
            wrong_policy_digest = dict(document)
            wrong_policy_digest["review_policy_evidence"] = {
                **document["review_policy_evidence"],
                "sha256": "sha256:" + "4" * 64,
            }
            path = self._write_advisory(directory, wrong_policy_digest)
            self.assertEqual(
                dependency_advisory_evidence_document(
                    path,
                    expected_dependency_graph=graph,
                    expected_source_sha="b" * 40,
                ),
                (False, "unbound_review_policy_evidence"),
            )

            wrong_source_time = dict(document)
            wrong_source_time["advisory_source_evidence"] = [
                {
                    **document["advisory_source_evidence"][0],
                    "observed_at": "2026-09-28T20:02:00Z",
                }
            ]
            path = self._write_advisory(directory, wrong_source_time)
            self.assertEqual(
                dependency_advisory_evidence_document(
                    path,
                    expected_dependency_graph=graph,
                    expected_source_sha="b" * 40,
                ),
                (False, "unbound_advisory_source_evidence"),
            )

    def test_advisory_source_must_be_distinct_from_policy_artifact(self):
        graph, document = self._advisory_fixture()
        with TemporaryDirectory() as directory:
            policy = document["review_policy_evidence"]
            same_authority = dict(document)
            same_authority["advisory_source_evidence"] = [policy]
            path = self._write_advisory(directory, same_authority)
            self.assertEqual(
                dependency_advisory_evidence_document(
                    path,
                    expected_dependency_graph=graph,
                    expected_source_sha="b" * 40,
                ),
                (False, "advisory_source_must_be_distinct_from_policy"),
            )

    def test_advisory_qualification_requires_exact_component_coverage(self):
        graph, document = self._advisory_fixture()
        with TemporaryDirectory() as directory:
            incomplete = dict(document)
            incomplete["reviewed_components"] = document["reviewed_components"][:-1]
            path = self._write_advisory(directory, incomplete)
            self.assertEqual(
                dependency_advisory_evidence_document(
                    path,
                    expected_dependency_graph=graph,
                    expected_source_sha="b" * 40,
                ),
                (False, "reviewed_component_coverage_mismatch"),
            )

            reordered = dict(document)
            reordered["reviewed_components"] = list(
                reversed(document["reviewed_components"])
            )
            path = self._write_advisory(directory, reordered)
            self.assertEqual(
                dependency_advisory_evidence_document(
                    path,
                    expected_dependency_graph=graph,
                    expected_source_sha="b" * 40,
                ),
                (False, "reviewed_component_coverage_mismatch"),
            )

    def test_advisory_qualification_cannot_hide_blocking_findings(self):
        graph, document = self._advisory_fixture()
        with TemporaryDirectory() as directory:
            blocked = dict(document)
            blocked["blocking_findings"] = ["GHSA-example remains unresolved"]
            path = self._write_advisory(directory, blocked)
            self.assertEqual(
                dependency_advisory_evidence_document(
                    path,
                    expected_dependency_graph=graph,
                    expected_source_sha="b" * 40,
                ),
                (False, "blocking_findings_present"),
            )

            malformed_residual = dict(document)
            malformed_residual["residual_risks"] = [" duplicate ", " duplicate "]
            path = self._write_advisory(directory, malformed_residual)
            self.assertEqual(
                dependency_advisory_evidence_document(
                    path,
                    expected_dependency_graph=graph,
                    expected_source_sha="b" * 40,
                ),
                (False, "invalid_residual_risks"),
            )

    def test_manifest_check_is_read_only_and_byte_stable(self):
        before = OUTPUT.read_bytes()
        result = subprocess.run(
            [sys.executable, "tools/build_provenance_manifest.py", "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(OUTPUT.read_bytes(), before)
        self.assertEqual(before, rendered_manifest().encode("utf-8"))
        self.assertNotIn(b"\r\n", before)


if __name__ == "__main__":
    unittest.main()
