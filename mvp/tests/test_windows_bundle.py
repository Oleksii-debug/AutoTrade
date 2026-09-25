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

    def composition(self, *, source_sha=SOURCE_SHA, overrides=None):
        dependency_lock = self.staging / "dependency-lock.json"
        dependency_lock.write_text(
            '{"dependencies":{"runtime":"1.0.0"}}\n',
            encoding="utf-8",
        )
        sbom = self.staging / "sbom.spdx.json"
        sbom.write_text(
            '{"SPDXID":"SPDXRef-DOCUMENT","spdxVersion":"SPDX-2.3"}\n',
            encoding="utf-8",
        )
        components = []
        for path in sorted(self.staging.rglob("*")):
            if path.is_file() and not path.is_symlink():
                relative = path.relative_to(self.staging).as_posix()
                if relative == "dependency-lock.json":
                    kind = "dependency-lock"
                elif relative == "sbom.spdx.json":
                    kind = "sbom"
                elif relative.endswith(".exe"):
                    kind = "runtime"
                else:
                    kind = "asset"
                components.append(
                    {
                        "component_id": relative.replace("/", "-"),
                        "kind": kind,
                        "path": relative,
                        "version": "1.0.0",
                        "sha256": "sha256:" + sha256(path.read_bytes()).hexdigest(),
                    }
                )
        document = {
            "schema_version": "1.0.0",
            "product": "AutoTrade",
            "source_sha": source_sha,
            "dependency_lock_sha256": "sha256:" + sha256(
                dependency_lock.read_bytes()
            ).hexdigest(),
            "sbom_sha256": "sha256:" + sha256(sbom.read_bytes()).hexdigest(),
            "schema_compatibility": {"minimum": "1.0.0", "maximum": "1.0.x"},
            "runtime": {
                "architecture": "x64",
                "runtime_identifier": "win-x64",
                "minimum_windows_version": "10.0.22621",
            },
            "components": components,
        }
        if overrides:
            document.update(overrides)
        path = self.root / "composition.json"
        path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
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
            composition_path=self.composition(),
        )
        self.assertTrue(result["manifest"]["release_eligible"])
        self.assertFalse(result["manifest"]["trading_authority_granted_by_artifact"])

    def test_release_mode_requires_exact_composition_manifest(self):
        output = self.root / "missing-composition.zip"
        with self.assertRaisesRegex(BundleError, "requires an exact Windows composition"):
            build_bundle(
                staging=self.staging,
                output=output,
                version="1.0.0",
                source_sha=SOURCE_SHA,
                mode="release",
                provenance_path=self.provenance(eligible=True),
            )
        self.assertFalse(output.exists())

    def test_release_composition_binds_source_sbom_runtime_and_every_staged_file(self):
        composition = self.composition()
        output = self.root / "composed.zip"
        result = build_bundle(
            staging=self.staging,
            output=output,
            version="1.0.0",
            source_sha=SOURCE_SHA,
            mode="release",
            provenance_path=self.provenance(eligible=True),
            composition_path=composition,
        )
        manifest = result["manifest"]
        self.assertEqual(
            manifest["composition_sha256"],
            "sha256:" + sha256(composition.read_bytes()).hexdigest(),
        )
        self.assertEqual(manifest["composition"]["source_sha"], SOURCE_SHA)
        self.assertEqual(manifest["composition"]["runtime"]["runtime_identifier"], "win-x64")
        self.assertEqual(
            {item["path"] for item in manifest["composition"]["components"]},
            {
                "AutoTrade.exe",
                "contracts/baseline.json",
                "dependency-lock.json",
                "sbom.spdx.json",
            },
        )

        extra = self.staging / "debug.log"
        extra.write_text("must not silently ship\n", encoding="utf-8")
        with self.assertRaisesRegex(BundleError, "undeclared staging files"):
            build_bundle(
                staging=self.staging,
                output=self.root / "extra-file.zip",
                version="1.0.0",
                source_sha=SOURCE_SHA,
                mode="release",
                provenance_path=self.provenance(eligible=True),
                composition_path=composition,
            )

    def test_release_composition_rejects_wrong_source_and_component_digest(self):
        wrong_source = self.composition(source_sha="b" * 40)
        with self.assertRaisesRegex(BundleError, "composition source_sha does not match"):
            build_bundle(
                staging=self.staging,
                output=self.root / "wrong-composition-source.zip",
                version="1.0.0",
                source_sha=SOURCE_SHA,
                mode="release",
                provenance_path=self.provenance(eligible=True),
                composition_path=wrong_source,
            )

        document = json.loads(self.composition().read_text(encoding="utf-8"))
        document["components"][0]["sha256"] = "sha256:" + "f" * 64
        bad_digest = self.root / "composition-bad-digest.json"
        bad_digest.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(BundleError, "digest does not match staged file"):
            build_bundle(
                staging=self.staging,
                output=self.root / "bad-component-digest.zip",
                version="1.0.0",
                source_sha=SOURCE_SHA,
                mode="release",
                provenance_path=self.provenance(eligible=True),
                composition_path=bad_digest,
            )

    def test_release_composition_requires_real_sbom_lock_and_runtime_pair(self):
        composition = self.composition()
        document = json.loads(composition.read_text(encoding="utf-8"))

        wrong_sbom = {
            **document,
            "sbom_sha256": "sha256:" + "e" * 64,
        }
        bad_sbom = self.root / "composition-wrong-sbom.json"
        bad_sbom.write_text(json.dumps(wrong_sbom), encoding="utf-8")
        with self.assertRaisesRegex(BundleError, "sbom digest does not match"):
            build_bundle(
                staging=self.staging,
                output=self.root / "wrong-sbom.zip",
                version="1.0.0",
                source_sha=SOURCE_SHA,
                mode="release",
                provenance_path=self.provenance(eligible=True),
                composition_path=bad_sbom,
            )

        wrong_runtime = {
            **document,
            "runtime": {
                "architecture": "x64",
                "runtime_identifier": "win-arm64",
                "minimum_windows_version": "10.0.22621",
            },
        }
        bad_runtime = self.root / "composition-wrong-runtime.json"
        bad_runtime.write_text(json.dumps(wrong_runtime), encoding="utf-8")
        with self.assertRaisesRegex(BundleError, "runtime_identifier does not match"):
            build_bundle(
                staging=self.staging,
                output=self.root / "wrong-runtime.zip",
                version="1.0.0",
                source_sha=SOURCE_SHA,
                mode="release",
                provenance_path=self.provenance(eligible=True),
                composition_path=bad_runtime,
            )

        uppercase_digest = {
            **document,
            "dependency_lock_sha256": document["dependency_lock_sha256"].upper(),
        }
        bad_case = self.root / "composition-uppercase-digest.json"
        bad_case.write_text(json.dumps(uppercase_digest), encoding="utf-8")
        with self.assertRaisesRegex(BundleError, "canonical lowercase"):
            build_bundle(
                staging=self.staging,
                output=self.root / "uppercase-digest.zip",
                version="1.0.0",
                source_sha=SOURCE_SHA,
                mode="release",
                provenance_path=self.provenance(eligible=True),
                composition_path=bad_case,
            )

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

        env_variant = self.staging / ".env.production"
        env_variant.write_text("API_TOKEN=must-not-ship\n", encoding="utf-8")
        with self.assertRaisesRegex(BundleError, "sensitive"):
            build_bundle(
                staging=self.staging,
                output=self.root / "bad-env-variant.zip",
                version="0.1.0-dev",
                source_sha=SOURCE_SHA,
                mode="diagnostics",
                provenance_path=self.provenance(eligible=False),
            )
        env_variant.unlink()

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

    def test_renamed_credential_vault_content_is_rejected(self):
        disguised = self.staging / "runtime-state.json"
        disguised.write_text(
            json.dumps(
                {
                    "version": 2,
                    "records": {
                        "cred-live": {
                            "handle": {
                                "handle_id": "cred-live",
                                "account_id": "acct-1",
                                "provider": "SIMULATED",
                                "environment": "LIVE",
                                "purpose": "TRADE",
                                "generation": 1,
                            },
                            "owner_identity": "windows-user",
                            "ciphertext": "AAECAwQ=",
                            "active": True,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(BundleError, "credential-vault content"):
            build_bundle(
                staging=self.staging,
                output=self.root / "renamed-vault.zip",
                version="0.1.0-dev",
                source_sha=SOURCE_SHA,
                mode="diagnostics",
                provenance_path=self.provenance(eligible=False),
            )
        self.assertFalse((self.root / "renamed-vault.zip").exists())

    def test_embedded_private_key_material_is_rejected_under_innocent_name(self):
        disguised = self.staging / "runtime-notes.txt"
        disguised.write_bytes(
            b"ordinary prefix\n"
            b"-----BEGIN PRIVATE KEY-----\n"
            b"must-not-ship\n"
            b"-----END PRIVATE KEY-----\n"
        )
        with self.assertRaisesRegex(BundleError, "private-key material"):
            build_bundle(
                staging=self.staging,
                output=self.root / "embedded-private-key.zip",
                version="0.1.0-dev",
                source_sha=SOURCE_SHA,
                mode="diagnostics",
                provenance_path=self.provenance(eligible=False),
            )
        self.assertFalse((self.root / "embedded-private-key.zip").exists())

    def test_unrelated_json_with_ciphertext_word_is_not_misclassified_as_vault(self):
        ordinary = self.staging / "protocol-sample.json"
        ordinary.write_text(
            json.dumps(
                {
                    "records": [{"ciphertext": "protocol-field"}],
                    "owner_identity": "documentation-label",
                }
            ),
            encoding="utf-8",
        )
        result = build_bundle(
            staging=self.staging,
            output=self.root / "ordinary-json.zip",
            version="0.1.0-dev",
            source_sha=SOURCE_SHA,
            mode="diagnostics",
            provenance_path=self.provenance(eligible=False),
        )
        self.assertTrue((self.root / "ordinary-json.zip").exists())
        self.assertIn(
            "protocol-sample.json",
            {item["path"] for item in result["manifest"]["files"]},
        )

    def test_binary_payload_without_secret_markers_is_not_content_scanned_as_text(self):
        binary = self.staging / "runtime.bin"
        binary.write_bytes(bytes(range(256)) * 4)
        result = build_bundle(
            staging=self.staging,
            output=self.root / "binary-ok.zip",
            version="0.1.0-dev",
            source_sha=SOURCE_SHA,
            mode="diagnostics",
            provenance_path=self.provenance(eligible=False),
        )
        self.assertTrue((self.root / "binary-ok.zip").exists())
        self.assertIn(
            "runtime.bin",
            {item["path"] for item in result["manifest"]["files"]},
        )

    def test_hash_sidecar_publish_removes_stale_temporary_file(self):
        provenance = self.provenance(eligible=False)
        output = self.root / "atomic-hash.zip"
        stale = output.with_suffix(".zip.sha256.tmp")
        stale.write_text("stale partial digest", encoding="utf-8")
        result = build_bundle(
            staging=self.staging,
            output=output,
            version="0.1.0-dev",
            source_sha=SOURCE_SHA,
            mode="diagnostics",
            provenance_path=provenance,
        )
        self.assertFalse(stale.exists())
        self.assertEqual(
            output.with_suffix(".zip.sha256").read_text(encoding="utf-8").split()[0],
            result["sha256"],
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

    def test_windows_case_insensitive_path_collisions_are_rejected(self):
        upper = self.staging / "CaseCollision.dll"
        lower = self.staging / "casecollision.dll"
        upper.write_bytes(b"one")
        try:
            lower.write_bytes(b"two")
        except OSError:
            self.skipTest("filesystem is case-insensitive")
        if upper.resolve() == lower.resolve():
            self.skipTest("filesystem is case-insensitive")
        with self.assertRaisesRegex(BundleError, "Windows path collision"):
            build_bundle(
                staging=self.staging,
                output=self.root / "collision.zip",
                version="0.1.0-dev",
                source_sha=SOURCE_SHA,
                mode="diagnostics",
                provenance_path=self.provenance(eligible=False),
            )

    def test_windows_reserved_and_trailing_dot_paths_are_rejected(self):
        with self.assertRaisesRegex(BundleError, "reserved device name"):
            _windows_path_key("CON.txt")
        with self.assertRaisesRegex(BundleError, "reserved device name"):
            _windows_path_key("nested/LPT9.log")
        with self.assertRaisesRegex(BundleError, "trailing space/dot"):
            _windows_path_key("nested/report.")
        with self.assertRaisesRegex(BundleError, "trailing space/dot"):
            _windows_path_key("nested/report ")
        with self.assertRaisesRegex(BundleError, "alternate-data-stream"):
            _windows_path_key("nested/report.txt:payload")

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
