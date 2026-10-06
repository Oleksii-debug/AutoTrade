from __future__ import annotations

import base64
from hashlib import sha256, sha512
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile

from tools import build_provider_free_candidate as candidate
from tools.release_scope_mapping import strict_json_bytes


class ProviderFreeDependencyIdentityTests(unittest.TestCase):
    def test_repository_webview_input_matches_release_provenance(self):
        inputs = strict_json_bytes(
            (
                candidate.ROOT
                / "packaging/windows/provider-free-inputs.json"
            ).read_bytes(),
            label="provider-free inputs",
        )
        nuget_lock = strict_json_bytes(
            (
                candidate.ROOT
                / "src/AutoTrade.Desktop/packages.lock.json"
            ).read_bytes(),
            label="Desktop NuGet lock",
        )
        digest = candidate._require_webview2_input_identity(
            candidate.ROOT,
            inputs,
            nuget_lock,
        )
        self.assertEqual(inputs["webview2_sdk"]["version"], "1.0.4258.31")
        self.assertEqual(
            digest,
            "sHVZ2MQrHT1J3q5/6csn0fIP27rzpTO/"
            "RRC+wPQDrZheB7T4t2n+vVbY6SdCKr4WRV9s/"
            "gmBM3PPMWWJtoDzMg==",
        )


    def test_repository_reviewed_license_evidence_is_stageable(self):
        policy = strict_json_bytes(
            (
                candidate.ROOT
                / "provenance/dotnet-package-rights.json"
            ).read_bytes(),
            label="NuGet package rights",
        )
        expected = {
            record["expected_license_text_path"]
            for record in policy["packages"]
        }
        self.assertTrue(expected)
        for relative in expected:
            with self.subTest(relative=relative):
                self.assertTrue(candidate._source_path_selected(relative))
                self.assertTrue((candidate.ROOT / relative).is_file())
        self.assertEqual(
            set(
                candidate._require_staged_reviewed_license_evidence(
                    candidate.ROOT
                )
            ),
            expected,
        )

    def test_missing_reviewed_license_evidence_fails_staged_product(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            policy_dir = root / "provenance"
            policy_dir.mkdir(parents=True)
            (policy_dir / "dotnet-package-rights.json").write_text(
                json.dumps({
                    "schema_version": "1.0.0",
                    "packages": [{
                        "expected_license_text_path":
                            "provenance/licenses/Missing.LICENSE.txt",
                    }],
                }),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "absent from staged product"
            ):
                candidate._require_staged_reviewed_license_evidence(root)


    def test_webview_archive_rights_bind_exact_policy_and_reviewed_license(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "provenance/licenses").mkdir(parents=True)
            license_text = (
                "Example license text.\n"
                "Redistribution is permitted.\n"
            )
            (root / "provenance/licenses/WebView.LICENSE.txt").write_text(
                license_text,
                encoding="utf-8",
            )
            archive = root / "webview.nupkg"
            with zipfile.ZipFile(archive, "w") as value:
                value.writestr("LICENSE.txt", license_text.encode("utf-8"))
                value.writestr("NOTICE.txt", b"Required notice\n")
                value.writestr(
                    "Microsoft.Web.WebView2.nuspec",
                    (
                        "<?xml version=\"1.0\"?>"
                        "<package><metadata>"
                        "<id>Microsoft.Web.WebView2</id>"
                        "<version>1.2.3</version>"
                        "<license type=\"file\">LICENSE.txt</license>"
                        "</metadata></package>"
                    ).encode("utf-8"),
                )
            content_hash = base64.b64encode(
                sha512(archive.read_bytes()).digest()
            ).decode("ascii")
            (root / "provenance/dotnet-package-rights.json").write_text(
                json.dumps({
                    "schema_version": "1.0.0",
                    "packages": [{
                        "name": "Microsoft.Web.WebView2",
                        "version": "1.2.3",
                        "content_hash_sha512_base64": content_hash,
                        "license_id": "BSD-3-Clause",
                        "license_file": "LICENSE.txt",
                        "notice_file": "NOTICE.txt",
                        "expected_license_text_path":
                            "provenance/licenses/WebView.LICENSE.txt",
                    }],
                }),
                encoding="utf-8",
            )
            candidate._require_webview2_archive_rights(
                root,
                archive,
                version="1.2.3",
                content_hash=content_hash,
            )

            (root / "provenance/licenses/WebView.LICENSE.txt").write_text(
                license_text + "unexpected restriction\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "differs from reviewed evidence"
            ):
                candidate._require_webview2_archive_rights(
                    root,
                    archive,
                    version="1.2.3",
                    content_hash=content_hash,
                )

    def test_webview_archive_rights_reject_wrong_nuspec_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "provenance/licenses").mkdir(parents=True)
            license_text = "Example license\n"
            (root / "provenance/licenses/WebView.LICENSE.txt").write_text(
                license_text,
                encoding="utf-8",
            )
            archive = root / "webview.nupkg"
            with zipfile.ZipFile(archive, "w") as value:
                value.writestr("LICENSE.txt", license_text.encode("utf-8"))
                value.writestr("NOTICE.txt", b"Required notice\n")
                value.writestr(
                    "wrong.nuspec",
                    (
                        "<package><metadata>"
                        "<id>Wrong.Package</id>"
                        "<version>1.2.3</version>"
                        "<license type=\"file\">LICENSE.txt</license>"
                        "</metadata></package>"
                    ).encode("utf-8"),
                )
            content_hash = base64.b64encode(
                sha512(archive.read_bytes()).digest()
            ).decode("ascii")
            (root / "provenance/dotnet-package-rights.json").write_text(
                json.dumps({
                    "schema_version": "1.0.0",
                    "packages": [{
                        "name": "Microsoft.Web.WebView2",
                        "version": "1.2.3",
                        "content_hash_sha512_base64": content_hash,
                        "license_id": "BSD-3-Clause",
                        "license_file": "LICENSE.txt",
                        "notice_file": "NOTICE.txt",
                        "expected_license_text_path":
                            "provenance/licenses/WebView.LICENSE.txt",
                    }],
                }),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "nuspec id mismatch"):
                candidate._require_webview2_archive_rights(
                    root,
                    archive,
                    version="1.2.3",
                    content_hash=content_hash,
                )

    def test_sha512_archive_identity_is_verified_before_extraction(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "package.nupkg"
            destination = root / "out"
            with zipfile.ZipFile(archive, "w") as value:
                value.writestr("payload.dll", b"verified")
            raw = archive.read_bytes()
            expected = base64.b64encode(sha512(raw).digest()).decode("ascii")
            candidate.extract_pinned(
                archive,
                destination,
                expected,
                digest_algorithm="sha512-base64",
            )
            self.assertEqual(
                (destination / "payload.dll").read_bytes(),
                b"verified",
            )

    def test_sha512_archive_mismatch_fails_without_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "package.nupkg"
            destination = root / "out"
            with zipfile.ZipFile(archive, "w") as value:
                value.writestr("payload.dll", b"verified")
            wrong = base64.b64encode(sha512(b"wrong").digest()).decode("ascii")
            with self.assertRaisesRegex(
                ValueError,
                "differs from frozen input",
            ):
                candidate.extract_pinned(
                    archive,
                    destination,
                    wrong,
                    digest_algorithm="sha512-base64",
                )
            self.assertFalse(destination.exists())

    def test_candidate_sbom_exposes_python_and_webview_identities(self):
        inputs = strict_json_bytes(
            (
                candidate.ROOT
                / "packaging/windows/provider-free-inputs.json"
            ).read_bytes(),
            label="provider-free inputs",
        )
        inventory = [{
            "path": "AutoTrade.Desktop.exe",
            "sha256": "sha256:" + "1" * 64,
        }]
        document = candidate._build_candidate_sbom(
            "a" * 40,
            inventory,
            inputs,
            inputs["webview2_sdk"]["content_hash_sha512_base64"],
        )
        packages = {item["name"]: item for item in document["packages"]}
        self.assertEqual(packages["CPython"]["versionInfo"], "3.12.10")
        self.assertEqual(
            packages["Microsoft.Web.WebView2"]["versionInfo"],
            "1.0.4258.31",
        )
        self.assertEqual(document["spdxVersion"], "SPDX-2.3")

    def test_workflow_uses_source_controlled_runtime_urls(self):
        workflow = (
            candidate.ROOT / ".github/workflows/provider-free-product.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "packaging/windows/provider-free-inputs.json",
            workflow,
        )
        self.assertIn("$inputs.webview2_sdk.url", workflow)
        self.assertNotIn("1.0.4191.47", workflow)

    def test_input_drift_from_release_provenance_fails(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "provenance").mkdir()
            manifest = {
                "dotnet_package_dependencies": [{
                    "name": "Microsoft.Web.WebView2",
                    "version": "1.0.4258.31",
                    "content_hash_sha512_base64": "canonical",
                }]
            }
            (root / "provenance/release-dependency-manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            inputs = {
                "webview2_sdk": {
                    "version": "1.0.4191.47",
                    "url": (
                        "https://api.nuget.org/v3-flatcontainer/"
                        "microsoft.web.webview2/1.0.4191.47/"
                        "microsoft.web.webview2.1.0.4191.47.nupkg"
                    ),
                    "content_hash_sha512_base64": "stale",
                }
            }
            nuget_lock = {
                "version": 1,
                "dependencies": {
                    "net10.0-windows7.0": {
                        "Microsoft.Web.WebView2": {
                            "type": "Direct",
                            "requested": "[1.0.4191.47, )",
                            "resolved": "1.0.4191.47",
                            "contentHash": "stale",
                        }
                    }
                },
            }
            with self.assertRaisesRegex(
                ValueError,
                "lock differs from release provenance",
            ):
                candidate._require_webview2_input_identity(
                    root,
                    inputs,
                    nuget_lock,
                )


    def test_lock_drift_from_frozen_input_fails_before_candidate_build(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "provenance").mkdir()
            manifest = {
                "dotnet_package_dependencies": [{
                    "project": "src/AutoTrade.Desktop/AutoTrade.Desktop.csproj",
                    "target": "net10.0-windows7.0",
                    "name": "Microsoft.Web.WebView2",
                    "type": "Direct",
                    "version": "1.0.4258.31",
                    "content_hash_sha512_base64": "canonical",
                    "dependencies": [],
                    "requested": "[1.0.4258.31, )",
                }]
            }
            (root / "provenance/release-dependency-manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            inputs = {
                "webview2_sdk": {
                    "version": "1.0.4258.31",
                    "url": (
                        "https://api.nuget.org/v3-flatcontainer/"
                        "microsoft.web.webview2/1.0.4258.31/"
                        "microsoft.web.webview2.1.0.4258.31.nupkg"
                    ),
                    "content_hash_sha512_base64": "canonical",
                }
            }
            drifted_lock = {
                "version": 1,
                "dependencies": {
                    "net10.0-windows7.0": {
                        "Microsoft.Web.WebView2": {
                            "type": "Direct",
                            "requested": "[1.0.4258.31, )",
                            "resolved": "1.0.4258.31",
                            "contentHash": "different",
                        }
                    }
                },
            }
            with self.assertRaisesRegex(
                ValueError, "lock differs from frozen input"
            ):
                candidate._require_webview2_input_identity(
                    root,
                    inputs,
                    drifted_lock,
                )


if __name__ == "__main__":
    unittest.main()
