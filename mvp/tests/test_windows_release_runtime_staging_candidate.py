from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import tools.stage_windows_release_runtime as release_runtime_module
from tools.stage_windows_foundation import FoundationStagingError, _REQUIRED as _FOUNDATION_REQUIRED
from tools.stage_windows_runtime import _RUNTIME_REQUIRED


SOURCE_SHA = "a" * 40
OTHER_SHA = "b" * 40


class WindowsReleaseRuntimeStagingCandidateTests(unittest.TestCase):
    def test_release_runtime_is_one_nonduplicated_37_leaf_descriptor_set(self):
        descriptors = release_runtime_module._RELEASE_RUNTIME_REQUIRED
        self.assertEqual(len(descriptors), 37)
        self.assertEqual(descriptors[: len(_FOUNDATION_REQUIRED)], _FOUNDATION_REQUIRED)
        self.assertEqual(descriptors[len(_FOUNDATION_REQUIRED) :], _RUNTIME_REQUIRED)
        self.assertEqual(len({item.component_id for item in descriptors}), len(descriptors))
        self.assertEqual(len({item.path for item in descriptors}), len(descriptors))

    def test_expected_source_sha_is_checked_from_identity_stable_composition(self):
        with TemporaryDirectory() as directory:
            composition = Path(directory) / "composition.json"
            composition.write_text(
                json.dumps({"source_sha": SOURCE_SHA, "components": []}),
                encoding="utf-8",
            )
            release_runtime_module._require_expected_source_sha(
                composition,
                expected_source_sha=SOURCE_SHA,
            )
            with self.assertRaisesRegex(
                FoundationStagingError,
                "does not match expected release source_sha",
            ):
                release_runtime_module._require_expected_source_sha(
                    composition,
                    expected_source_sha=OTHER_SHA,
                )

    def test_source_mismatch_fails_before_shared_staging_tcb_is_called(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            composition = root / "composition.json"
            composition.write_text(
                json.dumps({"source_sha": SOURCE_SHA, "components": []}),
                encoding="utf-8",
            )
            with patch.object(
                release_runtime_module,
                "_composition_publish_transaction",
                return_value=nullcontext(None),
            ), patch.object(
                release_runtime_module,
                "_stage_source_controlled_components_unserialized",
            ) as shared_stage:
                with self.assertRaisesRegex(
                    FoundationStagingError,
                    "does not match expected release source_sha",
                ):
                    release_runtime_module.stage_windows_release_runtime(
                        staging=root,
                        composition_path=composition,
                        expected_source_sha=OTHER_SHA,
                        source_root=root,
                    )
            shared_stage.assert_not_called()

    def test_release_runtime_delegates_once_to_shared_posix_staging_tcb(self):
        staging = Path("/candidate/staging")
        composition = Path("/candidate/composition.json")
        source_root = Path("/candidate/source")
        sentinel = ({"path": "sentinel"},)
        with patch.object(
            release_runtime_module,
            "_composition_publish_transaction",
            return_value=nullcontext(101),
        ), patch.object(
            release_runtime_module,
            "_require_expected_source_sha",
        ) as require_source, patch.object(
            release_runtime_module.os,
            "name",
            "posix",
        ), patch.object(
            release_runtime_module,
            "_retained_posix_directory",
            side_effect=(nullcontext(201), nullcontext(202)),
        ), patch.object(
            release_runtime_module,
            "_stage_source_controlled_components_unserialized",
            return_value=sentinel,
        ) as shared_stage:
            result = release_runtime_module.stage_windows_release_runtime(
                staging=staging,
                composition_path=composition,
                expected_source_sha=SOURCE_SHA,
                source_root=source_root,
            )
        self.assertIs(result, sentinel)
        require_source.assert_called_once_with(
            composition,
            expected_source_sha=SOURCE_SHA,
        )
        shared_stage.assert_called_once_with(
            staging=staging,
            composition_path=composition,
            source_root=source_root,
            descriptors=release_runtime_module._RELEASE_RUNTIME_REQUIRED,
            posix_staging_authority=202,
            posix_composition_authority=101,
        )

    def test_release_runtime_contains_canonical_neutral_store_not_research_alias(self):
        paths = {item.path for item in release_runtime_module._RELEASE_RUNTIME_REQUIRED}
        self.assertIn("autotrade_runtime/artifacts/store.py", paths)
        self.assertNotIn("research/autotrade_research/artifacts/content_store.py", paths)


if __name__ == "__main__":
    unittest.main()
