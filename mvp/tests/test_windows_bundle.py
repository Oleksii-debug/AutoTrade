from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile

from tools.build_windows_bundle import BundleError, _windows_path_key, build_bundle


SOURCE_SHA = "a" * 40


class DeterministicWindowsBundleTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.staging = self.root / "staging"
        self.staging.mkdir()
        (self.staging / "AutoTrade.exe").write_bytes(b"binary-placeholder")
        (self.staging / "contracts").mkdir()
        (self.staging / "contracts" / "baseline.json").write_text(
            '{"version":"1.0.0"}\n',
            encoding="utf-8",
        )

    def provenance(self, *, eligible=False, source_sha=None):
        path = self.root / "provenance.json"
        blockers = [] if eligible else [
            {"code": "RELEASE_COMPOSITION_MISSING"},
            {"code": "MODEL_DATA_RIGHTS_MISSING"},
        ]
        document = {
            "schema_version": "1.0.0",
            "release_eligible": eligible,
            "blocking_issues": blockers,
        }
        if source_sha is not None:
            document["source_sha"] = source_sha
        elif eligible:
            document["source_sha"] = SOURCE_SHA
        path.write_text(
            json.dumps(document),
            encoding="utf-8",
        )
        return path

    def test_diagnostics_bundle_is_byte_reproducible(self):
        provenance = self.provenance(eligible=False)
        first = self.root / "first.zip"
        second = self.root / "second.zip"
        one = build_bundle(
            staging=self.staging,
            output=first,
            version="0.1.0-dev",
            source_sha=SOURCE_SHA,
            mode="diagnostics",
            provenance_path=provenance,
        )
        two = build_bundle(
            staging=self.staging,
            output=second,
            version="0.1.0-dev",
            source_sha=SOURCE_SHA,
            mode="diagnostics",
            provenance_path=provenance,
        )
        self.assertEqual(one["sha256"], two["sha256"])
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(
            Path(one["hash_file"]).read_text(encoding="utf-8").split()[0],
            sha256(first.read_bytes()).hexdigest(),
        )

    def test_manifest_binds_exact_source_and_payload_hashes(self):
        output = self.root / "diagnostics.zip"
        result = build_bundle(
            staging=self.staging,
            output=output,
            version="0.1.0-dev",
            source_sha=SOURCE_SHA,
            mode="diagnostics",
            provenance_path=self.provenance(eligible=False),
        )
        self.assertFalse(result["manifest"]["release_eligible"])
        self.assertFalse(result["manifest"]["trading_authority_granted_by_artifact"])
        self.assertEqual(result["manifest"]["source_sha"], SOURCE_SHA)
        self.assertEqual(
            result["manifest"]["provenance_sha256"],
            "sha256:" + sha256(self.provenance(eligible=False).read_bytes()).hexdigest(),
        )
        self.assertEqual(
            result["manifest"]["provenance_blockers"],
            ["RELEASE_COMPOSITION_MISSING", "MODEL_DATA_RIGHTS_MISSING"],
        )
        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
            self.assertEqual(
                names,
                [
                    "bundle-manifest.json",
                    "payload/AutoTrade.exe",
                    "payload/contracts/baseline.json",
                ],
            )
            manifest = json.loads(archive.read("bundle-manifest.json"))
            entries = {item["path"]: item for item in manifest["files"]}
            self.assertEqual(
                entries["AutoTrade.exe"]["sha256"],
                "sha256:" + sha256(b"binary-placeholder").hexdigest(),
            )

    def test_diagnostics_never_inherits_release_eligible_label(self):
        output = self.root / "diagnostics-eligible-source.zip"
        result = build_bundle(
            staging=self.staging,
            output=output,
            version="1.0.0",
            source_sha=SOURCE_SHA,
            mode="diagnostics",
            provenance_path=self.provenance(eligible=True),
        )
        self.assertFalse(result["manifest"]["release_eligible"])
        self.assertFalse(result["manifest"]["trading_authority_granted_by_artifact"])

    def test_provenance_content_is_cryptographically_bound_to_bundle_manifest(self):
        provenance = self.provenance(eligible=False)
        first = build_bundle(
            staging=self.staging,
            output=self.root / "provenance-a.zip",
            version="0.1.0-dev",
            source_sha=SOURCE_SHA,
            mode="diagnostics",
            provenance_path=provenance,
        )
        document = json.loads(provenance.read_text(encoding="utf-8"))
        document["note"] = "different evidence document"
        provenance.write_text(json.dumps(document), encoding="utf-8")
        second = build_bundle(
            staging=self.staging,
            output=self.root / "provenance-b.zip",
            version="0.1.0-dev",
            source_sha=SOURCE_SHA,
            mode="diagnostics",
            provenance_path=provenance,
        )
        self.assertNotEqual(
            first["manifest"]["provenance_sha256"],
            second["manifest"]["provenance_sha256"],
        )
        self.assertNotEqual(first["sha256"], second["sha256"])

    def test_release_mode_fails_closed_while_provenance_has_blockers(self):
        with self.assertRaisesRegex(
            BundleError,
            "RELEASE_COMPOSITION_MISSING",
        ):
            build_bundle(
                staging=self.staging,
                output=self.root / "release.zip",
                version="1.0.0",
                source_sha=SOURCE_SHA,
                mode="release",
                provenance_path=self.provenance(eligible=False),
            )
        self.assertFalse((self.root / "release.zip").exists())

    def test_release_eligible_manifest_cannot_keep_blockers(self):
        provenance = self.root / "contradictory-provenance.json"
        provenance.write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "release_eligible": True,
                    "blocking_issues": [{"code": "UNRESOLVED_RIGHTS"}],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(BundleError, "inconsistent"):
            build_bundle(
                staging=self.staging,
                output=self.root / "contradictory.zip",
                version="1.0.0",
                source_sha=SOURCE_SHA,
                mode="release",
                provenance_path=provenance,
            )
        self.assertFalse((self.root / "contradictory.zip").exists())

    def test_release_mode_can_package_only_explicitly_eligible_inputs(self):
        output = self.root / "eligible.zip"
        result = build_bundle(
            staging=self.staging,
            output=output,
            version="1.0.0",
            source_sha=SOURCE_SHA,
            mode="release",
            provenance_path=self.provenance(eligible=True),
        )
        self.assertTrue(result["manifest"]["release_eligible"])
        self.assertFalse(result["manifest"]["trading_authority_granted_by_artifact"])

    def test_release_mode_requires_exact_head_provenance_binding(self):
        missing = self.root / "eligible-without-sha.json"
        missing.write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "release_eligible": True,
                    "blocking_issues": [],
                }
            ),
            encoding="utf-8",
        )
        missing_output = self.root / "missing-sha.zip"
        with self.assertRaisesRegex(BundleError, "must bind.*source_sha"):
            build_bundle(
                staging=self.staging,
                output=missing_output,
                version="1.0.0",
                source_sha=SOURCE_SHA,
                mode="release",
                provenance_path=missing,
            )
        self.assertFalse(missing_output.exists())

        mismatch_output = self.root / "mismatch-sha.zip"
        with self.assertRaisesRegex(BundleError, "does not match"):
            build_bundle(
                staging=self.staging,
                output=mismatch_output,
                version="1.0.0",
                source_sha=SOURCE_SHA,
                mode="release",
                provenance_path=self.provenance(
                    eligible=True,
                    source_sha="b" * 40,
                ),
            )
        self.assertFalse(mismatch_output.exists())

    def test_secret_like_files_and_symlinks_are_rejected(self):
        secret = self.staging / "credentials.json"
        secret.write_text('{"secret":"must-not-ship"}', encoding="utf-8")
        with self.assertRaisesRegex(BundleError, "sensitive"):
            build_bundle(
                staging=self.staging,
                output=self.root / "bad.zip",
                version="0.1.0-dev",
                source_sha=SOURCE_SHA,
                mode="diagnostics",
                provenance_path=self.provenance(eligible=False),
            )
        secret.unlink()

        target = self.staging / "AutoTrade.exe"
        link = self.staging / "linked.exe"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("filesystem cannot create symlinks")
        with self.assertRaisesRegex(BundleError, "symlinks"):
            build_bundle(
                staging=self.staging,
                output=self.root / "bad-link.zip",
                version="0.1.0-dev",
                source_sha=SOURCE_SHA,
                mode="diagnostics",
                provenance_path=self.provenance(eligible=False),
            )

    def test_windows_path_validation_rejects_ambiguous_or_reserved_names(self):
        self.assertEqual(
            _windows_path_key("Payload/Readme.TXT"),
            _windows_path_key("payload/readme.txt"),
        )
        for relative in (
            "CON.txt",
            "folder/NUL",
            "folder/name.",
            "folder/name ",
            "folder/bad?.txt",
        ):
            with self.subTest(relative=relative):
                with self.assertRaises(BundleError):
                    _windows_path_key(relative)

    def test_case_insensitive_path_collision_is_rejected_when_host_can_represent_it(self):
        first = self.staging / "CaseCollision.txt"
        second = self.staging / "casecollision.TXT"
        first.write_bytes(b"one")
        second.write_bytes(b"two")
        represented = [
            path.name
            for path in self.staging.iterdir()
            if path.name.casefold() == "casecollision.txt"
        ]
        if len(represented) < 2:
            self.skipTest("host filesystem is already case-insensitive")
        with self.assertRaisesRegex(BundleError, "case-insensitive path collision"):
            build_bundle(
                staging=self.staging,
                output=self.root / "collision.zip",
                version="0.1.0-dev",
                source_sha=SOURCE_SHA,
                mode="diagnostics",
                provenance_path=self.provenance(eligible=False),
            )

    def test_output_and_hash_must_be_outside_staging(self):
        provenance = self.provenance(eligible=False)
        output = self.staging / "diagnostics.zip"
        with self.assertRaisesRegex(BundleError, "outside the staging"):
            build_bundle(
                staging=self.staging,
                output=output,
                version="0.1.0-dev",
                source_sha=SOURCE_SHA,
                mode="diagnostics",
                provenance_path=provenance,
            )
        self.assertFalse(output.exists())
        self.assertFalse(output.with_suffix(".zip.sha256").exists())

    def test_source_sha_and_empty_staging_are_rejected(self):
        with self.assertRaisesRegex(BundleError, "source_sha"):
            build_bundle(
                staging=self.staging,
                output=self.root / "bad-sha.zip",
                version="0.1.0-dev",
                source_sha="main",
                mode="diagnostics",
                provenance_path=self.provenance(eligible=False),
            )
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(BundleError, "no files"):
            build_bundle(
                staging=empty,
                output=self.root / "empty.zip",
                version="0.1.0-dev",
                source_sha=SOURCE_SHA,
                mode="diagnostics",
                provenance_path=self.provenance(eligible=False),
            )


if __name__ == "__main__":
    unittest.main()
