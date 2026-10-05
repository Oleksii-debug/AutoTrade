from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

import tools.stage_windows_release_runtime as release_runtime_module
from tools.stage_windows_foundation import (
    FoundationStagingError,
    _REQUIRED as _FOUNDATION_REQUIRED,
)
from tools.stage_windows_runtime import _RUNTIME_REQUIRED


SOURCE_SHA = "a" * 40


class WindowsReleaseRuntimeStagingCandidateTests(unittest.TestCase):
    def test_release_runtime_is_one_nonduplicated_37_leaf_descriptor_set(self):
        descriptors = release_runtime_module._RELEASE_RUNTIME_REQUIRED
        self.assertEqual(len(_FOUNDATION_REQUIRED), 7)
        self.assertEqual(len(_RUNTIME_REQUIRED), 30)
        self.assertEqual(len(descriptors), 37)
        self.assertEqual(descriptors[: len(_FOUNDATION_REQUIRED)], _FOUNDATION_REQUIRED)
        self.assertEqual(descriptors[len(_FOUNDATION_REQUIRED) :], _RUNTIME_REQUIRED)
        self.assertEqual(len({item.component_id for item in descriptors}), 37)
        self.assertEqual(len({item.path for item in descriptors}), 37)

    def test_release_runtime_delegates_once_to_exact_source_staging_tcb(self):
        staging = Path("candidate-staging")
        composition = Path("candidate-composition.json")
        source_root = Path("candidate-source")
        sentinel = ({"path": "sentinel"},)
        with patch.object(
            release_runtime_module,
            "_stage_source_controlled_components",
            return_value=sentinel,
        ) as shared_stage:
            result = release_runtime_module.stage_windows_release_runtime(
                staging=staging,
                composition_path=composition,
                expected_source_sha=SOURCE_SHA,
                source_root=source_root,
            )
        self.assertIs(result, sentinel)
        shared_stage.assert_called_once_with(
            staging=staging,
            composition_path=composition,
            source_root=source_root,
            descriptors=release_runtime_module._RELEASE_RUNTIME_REQUIRED,
            expected_source_sha=SOURCE_SHA,
        )

    def test_invalid_expected_source_sha_fails_before_git_or_staging_mutation(self):
        with self.assertRaisesRegex(
            FoundationStagingError,
            "expected_source_sha must be an exact lowercase Git object id",
        ):
            release_runtime_module.stage_windows_release_runtime(
                staging=Path("candidate-staging"),
                composition_path=Path("candidate-composition.json"),
                expected_source_sha="NOT-A-GIT-SHA",
                source_root=Path("candidate-source"),
            )

    def test_release_runtime_contains_neutral_store_not_research_alias(self):
        paths = {item.path for item in release_runtime_module._RELEASE_RUNTIME_REQUIRED}
        self.assertIn("autotrade_runtime/artifacts/store.py", paths)
        self.assertNotIn(
            "research/autotrade_research/artifacts/content_store.py",
            paths,
        )


if __name__ == "__main__":
    unittest.main()
