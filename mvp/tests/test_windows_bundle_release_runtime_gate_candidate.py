from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import tools.build_windows_bundle as bundle_module
from tools.build_windows_bundle import BundleError, build_bundle
from tools.stage_windows_foundation import FoundationStagingError


SOURCE_SHA = "a" * 40


class WindowsBundleReleaseRuntimeGateCandidateTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.staging = self.root / "staging"
        self.staging.mkdir()
        payload = self.staging / "AutoTrade.Desktop.exe"
        payload.write_bytes(b"desktop")
        digest = "sha256:" + sha256(payload.read_bytes()).hexdigest()
        self.composition = self.root / "composition.json"
        self.composition.write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "product": "AutoTrade",
                    "source_sha": SOURCE_SHA,
                    "dependency_lock_sha256": digest,
                    "sbom_sha256": digest,
                    "schema_compatibility": {
                        "minimum": "1.0.0",
                        "maximum": "1.0.x",
                    },
                    "runtime": {
                        "architecture": "x64",
                        "runtime_identifier": "win-x64",
                        "minimum_windows_version": "10.0.22621",
                    },
                    "components": [
                        {
                            "component_id": "desktop",
                            "kind": "dependency-lock",
                            "path": "AutoTrade.Desktop.exe",
                            "version": "1.0.0",
                            "sha256": digest,
                        },
                        {
                            "component_id": "sbom",
                            "kind": "sbom",
                            "path": "AutoTrade.Desktop.exe",
                            "version": "1.0.0",
                            "sha256": digest,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        # The composition above intentionally cannot reach archive publication;
        # the tests below assert the release staging gate fires before collection.
        self.provenance = self.root / "provenance.json"
        self.provenance.write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "source_sha": SOURCE_SHA,
                    "release_eligible": True,
                    "blocking_issues": [],
                }
            ),
            encoding="utf-8",
        )
        self.output = self.root / "release.zip"

    def _build(self):
        return build_bundle(
            staging=self.staging,
            output=self.output,
            version="1.0.0",
            source_sha=SOURCE_SHA,
            mode="release",
            provenance_path=self.provenance,
            composition_path=self.composition,
        )

    def test_release_bundle_invokes_exact_source_runtime_gate_before_collection(self):
        gate_error = FoundationStagingError("sentinel exact-source gate")
        with patch.object(
            bundle_module,
            "stage_windows_release_runtime",
            side_effect=gate_error,
        ) as release_stage, patch.object(
            bundle_module,
            "_collect",
        ) as collect:
            with self.assertRaisesRegex(
                BundleError,
                "release source-controlled runtime staging failed closed",
            ):
                self._build()
        release_stage.assert_called_once_with(
            staging=self.staging,
            composition_path=self.composition,
            expected_source_sha=SOURCE_SHA,
            source_root=bundle_module.ROOT,
        )
        collect.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_missing_release_composition_fails_before_runtime_gate(self):
        with patch.object(
            bundle_module,
            "stage_windows_release_runtime",
        ) as release_stage:
            with self.assertRaisesRegex(
                BundleError,
                "requires an exact Windows composition manifest",
            ):
                build_bundle(
                    staging=self.staging,
                    output=self.output,
                    version="1.0.0",
                    source_sha=SOURCE_SHA,
                    mode="release",
                    provenance_path=self.provenance,
                    composition_path=None,
                )
        release_stage.assert_not_called()
        self.assertFalse(self.output.exists())


    def test_post_stage_runtime_and_composition_replacement_cannot_rebind_release(self):
        runtime = self.staging / "AutoTrade.Desktop.exe"
        dependency_lock = self.staging / "dependency-lock.json"
        sbom = self.staging / "sbom.spdx.json"
        dependency_lock.write_bytes(b"lock")
        sbom.write_bytes(b"sbom")

        def digest(data: bytes) -> str:
            return "sha256:" + sha256(data).hexdigest()

        expected_runtime = {
            "component_id": "canonical-runtime-leaf",
            "kind": "runtime",
            "path": "AutoTrade.Desktop.exe",
            "version": "source-controlled",
            "sha256": digest(runtime.read_bytes()),
        }
        expected = [expected_runtime]
        for index in range(36):
            path = self.staging / f"canonical-{index}.py"
            payload = f"canonical-{index}".encode("utf-8")
            path.write_bytes(payload)
            expected.append(
                {
                    "component_id": f"canonical-{index}",
                    "kind": "runtime",
                    "path": path.name,
                    "version": "source-controlled",
                    "sha256": digest(payload),
                }
            )

        def write_composition(runtime_digest: str) -> None:
            components = [
                {**expected_runtime, "sha256": runtime_digest},
                *expected[1:],
                {
                    "component_id": "dependency-lock",
                    "kind": "dependency-lock",
                    "path": "dependency-lock.json",
                    "version": "1.0.0",
                    "sha256": digest(dependency_lock.read_bytes()),
                },
                {
                    "component_id": "sbom",
                    "kind": "sbom",
                    "path": "sbom.spdx.json",
                    "version": "1.0.0",
                    "sha256": digest(sbom.read_bytes()),
                },
            ]
            self.composition.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "product": "AutoTrade",
                        "source_sha": SOURCE_SHA,
                        "dependency_lock_sha256": components[-2]["sha256"],
                        "sbom_sha256": components[-1]["sha256"],
                        "schema_compatibility": {
                            "minimum": "1.0.0",
                            "maximum": "1.0.x",
                        },
                        "runtime": {
                            "architecture": "x64",
                            "runtime_identifier": "win-x64",
                            "minimum_windows_version": "10.0.22621",
                        },
                        "components": components,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

        write_composition(expected_runtime["sha256"])

        def stage_then_replace(**_kwargs):
            runtime.write_bytes(b"post-stage replacement")
            write_composition(digest(runtime.read_bytes()))
            return tuple(expected)

        with patch.object(
            bundle_module,
            "stage_windows_release_runtime",
            side_effect=stage_then_replace,
        ):
            with self.assertRaisesRegex(
                BundleError,
                "release runtime changed after exact-source staging",
            ):
                self._build()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
