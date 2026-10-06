from pathlib import Path
import unittest

from tools.build_provenance_manifest import build_manifest, git_blob_sha


class ReleaseRightsSourceInventoryTests(unittest.TestCase):
    def test_release_rights_inputs_are_git_blob_bound(self):
        root = Path(__file__).resolve().parents[2]
        manifest = build_manifest()
        inventory = manifest["source_inventory"]
        expected = {
            "dotnet_package_rights_blob_sha":
                root / "provenance/dotnet-package-rights.json",
            "external_runtime_rights_blob_sha":
                root / "provenance/external-runtime-rights.json",
            "autosport_reuse_manifest_blob_sha":
                root / "provenance/reuse/autosport-neutral-primitives.json",
        }
        for key, path in expected.items():
            with self.subTest(key=key):
                self.assertEqual(inventory[key], git_blob_sha(path))

    def test_release_manifest_keeps_real_external_rights_blockers(self):
        manifest = build_manifest()
        self.assertFalse(manifest["release_eligible"])
        codes = {
            item["code"]
            for item in manifest["blocking_issues"]
        }
        self.assertIn("RELEASE_COMPOSITION_MISSING", codes)
        self.assertIn("MODEL_DATA_RIGHTS_MISSING", codes)
        self.assertIn("DEPENDENCY_ADVISORY_EVIDENCE_MISSING", codes)


if __name__ == "__main__":
    unittest.main()
