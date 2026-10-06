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
        digest = candidate._require_webview2_input_identity(
            candidate.ROOT,
            inputs,
        )
        self.assertEqual(inputs["webview2_sdk"]["version"], "1.0.4258.31")
        self.assertEqual(
            digest,
            "sHVZ2MQrHT1J3q5/6csn0fIP27rzpTO/"
            "RRC+wPQDrZheB7T4t2n+vVbY6SdCKr4WRV9s/"
            "gmBM3PPMWWJtoDzMg==",
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
            with self.assertRaisesRegex(
                ValueError,
                "differs from release provenance",
            ):
                candidate._require_webview2_input_identity(root, inputs)


if __name__ == "__main__":
    unittest.main()
