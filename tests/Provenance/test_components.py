import json
import re
import unittest
from datetime import datetime
from hashlib import sha1
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.build_provenance_manifest import git_blob_sha

ROOT = Path(__file__).resolve().parents[2]
COMPONENTS = ROOT / "provenance" / "components.json"

SHA1 = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
UNRESOLVED = "UNRESOLVED_FIRST_PARTY_RIGHTS_RECORD"
ROOT_LICENSE_NAMES = {"LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "NOTICE"}


class ProvenanceComponentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = json.loads(COMPONENTS.read_text(encoding="utf-8"))
        cls.components = cls.document["components"]

    def test_git_blob_identity_is_stable_across_lf_and_crlf_checkouts(self):
        canonical = b'{\n  "schema_version": "1.0.0"\n}\n'
        expected = sha1(
            b"blob " + str(len(canonical)).encode("ascii") + b"\0" + canonical
        ).hexdigest()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lf = root / "lf.json"
            crlf = root / "crlf.json"
            lf.write_bytes(canonical)
            crlf.write_bytes(canonical.replace(b"\n", b"\r\n"))
            self.assertEqual(git_blob_sha(lf), expected)
            self.assertEqual(git_blob_sha(crlf), expected)

    def test_inventory_has_unique_component_and_repository_identity(self):
        names = [component["name"] for component in self.components]
        repositories = [component["repository"] for component in self.components]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(repositories), len(set(repositories)))

    def test_every_component_has_exact_commit_revision(self):
        for component in self.components:
            with self.subTest(component=component["name"]):
                self.assertRegex(component["repository"], REPOSITORY)
                self.assertRegex(component["revision"], SHA1)
                if "revision_type" in component:
                    self.assertEqual(component["revision_type"], "commit")
                if "tree" in component:
                    self.assertRegex(component["tree"], SHA1)

    def test_external_components_have_license_evidence_and_observation_time(self):
        external = [
            component
            for component in self.components
            if component["license"] != UNRESOLVED
        ]
        self.assertTrue(external)
        for component in external:
            with self.subTest(component=component["name"]):
                self.assertNotIn("UNRESOLVED", component["license"])
                self.assertRegex(component["license_blob_sha"], SHA1)
                observed = component["observed_commit_date"]
                self.assertTrue(observed.endswith("Z"))
                datetime.fromisoformat(observed.removesuffix("Z") + "+00:00")
                self.assertTrue(component["source_import_allowed"])
                self.assertNotIn("RELEASE_APPROVED", component["source_import_allowed"])
                self.assertNotIn("APPROVED_FOR_RELEASE", component["source_import_allowed"])

    def test_unresolved_first_party_rights_stay_fail_closed_for_release(self):
        unresolved = [
            component
            for component in self.components
            if component["license"] == UNRESOLVED
        ]
        self.assertTrue(unresolved)
        for component in unresolved:
            with self.subTest(component=component["name"]):
                self.assertFalse(component["root_license_file_found"])
                checked = set(component["root_license_files_checked"])
                self.assertTrue(ROOT_LICENSE_NAMES.issubset(checked))
                disposition = component["source_import_allowed"]
                self.assertIn(
                    "RELEASE_DISTRIBUTION_RIGHTS_CHAIN_STILL_REQUIRES_WP03_RECORD",
                    disposition,
                )
                self.assertNotIn("RELEASE_APPROVED", disposition)
                self.assertNotIn("APPROVED_FOR_RELEASE", disposition)

    def test_optional_github_identity_evidence_matches_pinned_revision(self):
        for component in self.components:
            url = component.get("identity_evidence_url")
            if url is None:
                continue
            owner, repository = component["repository"].split("/", 1)
            expected = (
                f"https://api.github.com/repos/{owner}/{repository}/commits/"
                f"{component['revision']}"
            )
            with self.subTest(component=component["name"]):
                self.assertEqual(url, expected)

    def test_policy_does_not_treat_public_source_as_release_permission(self):
        policy = self.document["policy"].lower()
        self.assertIn("no source import is approved solely because a repository is public", policy)
        self.assertIn("public", policy)
        self.assertIn("license", policy)
        self.assertIn("rights", policy)


    def test_release_distribution_state_is_machine_fail_closed(self):
        canonical_sha256 = re.compile(r"^sha256:[0-9a-f]{64}$")
        for component in self.components:
            with self.subTest(component=component["name"]):
                state = component.get("release_distribution_state")
                self.assertIn(state, {"BLOCKED", "APPROVED"})
                if state == "BLOCKED":
                    continue

                self.assertNotEqual(component["license"], UNRESOLVED)
                for evidence_field in (
                    "dependency_graph_sha256",
                    "notice_sha256",
                    "advisory_review_sha256",
                ):
                    self.assertRegex(
                        component.get(evidence_field, ""),
                        canonical_sha256,
                        msg=(
                            f"{component['name']} cannot be release-approved "
                            f"without {evidence_field}"
                        ),
                    )

    def test_current_inventory_is_not_release_approved_by_bootstrap_evidence(self):
        self.assertTrue(self.components)
        self.assertTrue(
            all(
                component["release_distribution_state"] == "BLOCKED"
                for component in self.components
            )
        )


if __name__ == "__main__":
    unittest.main()
