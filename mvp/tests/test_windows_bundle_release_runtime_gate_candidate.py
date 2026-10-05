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


def digest(data: bytes) -> str:
    return "sha256:" + sha256(data).hexdigest()


class WindowsBundleReleaseRuntimeGateCandidateTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.staging = self.root / "staging"
        self.staging.mkdir()
        self.provenance = self.root / "provenance.json"
        self.provenance.write_text(
            json.dumps(
                {
                    "source_sha": SOURCE_SHA,
                    "release_eligible": True,
                    "blocking_issues": [],
                }
            ),
            encoding="utf-8",
        )
        self.composition = self.root / "composition.json"
        self.composition.write_text("{}", encoding="utf-8")
        self.output = self.root / "release.zip"

    def _build(self, *, composition_path=None):
        return build_bundle(
            staging=self.staging,
            output=self.output,
            version="1.0.0",
            source_sha=SOURCE_SHA,
            mode="release",
            provenance_path=self.provenance,
            composition_path=(
                self.composition if composition_path is None else composition_path
            ),
        )

    def test_release_bundle_invokes_exact_source_runtime_gate_before_collection(self):
        gate_error = FoundationStagingError("sentinel exact-source gate")
        with patch.object(
            bundle_module,
            "stage_windows_release_runtime",
            side_effect=gate_error,
        ) as release_stage, patch.object(bundle_module, "_collect") as collect:
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

    def test_missing_release_composition_fails_before_runtime_gate_or_collection(self):
        with patch.object(
            bundle_module,
            "stage_windows_release_runtime",
        ) as release_stage, patch.object(bundle_module, "_collect") as collect:
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
        collect.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_post_stage_runtime_replacement_cannot_rebind_release(self):
        expected = []
        files = []
        components = []
        for descriptor in bundle_module._RELEASE_RUNTIME_REQUIRED:
            data = descriptor.path.encode("utf-8")
            item = {
                "component_id": descriptor.component_id,
                "kind": descriptor.kind,
                "path": descriptor.path,
                "version": "source-controlled",
                "sha256": digest(data),
            }
            expected.append(item)
            files.append((descriptor.path, Path(descriptor.path), data))
            components.append(dict(item))

        target_path, target_file, _target_data = files[-1]
        files[-1] = (target_path, target_file, b"post-stage replacement")
        components[-1]["sha256"] = digest(b"post-stage replacement")

        with self.assertRaisesRegex(
            BundleError,
            "release runtime changed after exact-source staging",
        ):
            bundle_module._require_release_runtime_snapshot_binding(
                files=files,
                composition={"components": components},
                staged_expected=tuple(expected),
            )

    def test_runtime_snapshot_must_cover_exact_canonical_descriptor_set(self):
        with self.assertRaisesRegex(BundleError, "canonical 37-leaf snapshot"):
            bundle_module._require_release_runtime_snapshot_binding(
                files=[],
                composition={"components": []},
                staged_expected=(),
            )


if __name__ == "__main__":
    unittest.main()
