from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import tools.stage_windows_foundation as staging_module
from tools.stage_windows_foundation import FoundationStagingError, stage_windows_foundation


ROOT = Path(__file__).resolve().parents[2]
SOURCE_SHA = subprocess.check_output(
    ("git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD"),
    text=True,
).strip()


@unittest.skipIf(os.name == "nt", "POSIX fail-closed staging acceptance")
class PosixStagingNamespaceAuthorityTests(unittest.TestCase):
    @staticmethod
    def _fixture(root: Path) -> tuple[Path, Path]:
        staging = root / "staging"
        staging.mkdir()
        composition = root / "composition.json"
        composition.write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "product": "AutoTrade",
                    "source_sha": SOURCE_SHA,
                    "components": [],
                }
            ),
            encoding="utf-8",
        )
        return staging, composition

    def test_public_entrypoint_rejects_before_any_publication_authority_is_entered(self):
        with TemporaryDirectory() as directory:
            staging, composition = self._fixture(Path(directory))
            before = composition.read_bytes()

            with patch.object(
                staging_module,
                "_composition_publish_transaction",
                side_effect=AssertionError("publication transaction must remain unreachable"),
            ), patch.object(
                staging_module,
                "_write_posix_new_regular",
                side_effect=AssertionError("POSIX component mutation must remain unreachable"),
            ):
                with self.assertRaisesRegex(
                    FoundationStagingError,
                    "requires Windows retained namespace authority",
                ):
                    stage_windows_foundation(
                        staging=staging,
                        composition_path=composition,
                    )

            self.assertEqual(composition.read_bytes(), before)
            self.assertEqual(list(staging.iterdir()), [])

    def test_internal_reviewed_descriptor_tcb_rejects_posix_before_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging, composition = self._fixture(root)
            existing = staging / "sentinel.bin"
            existing.write_bytes(b"staging-before")
            outside = root / "outside"
            outside.mkdir()
            external = outside / "sentinel.bin"
            external.write_bytes(b"external-before")
            before = composition.read_bytes()

            with self.assertRaisesRegex(
                FoundationStagingError,
                "requires Windows retained namespace authority",
            ):
                staging_module._stage_source_controlled_components(
                    staging=staging,
                    composition_path=composition,
                    source_root=ROOT,
                    descriptors=staging_module._REQUIRED,
                )

            self.assertEqual(composition.read_bytes(), before)
            self.assertEqual(existing.read_bytes(), b"staging-before")
            self.assertEqual(external.read_bytes(), b"external-before")
            self.assertEqual(
                sorted(path.name for path in staging.iterdir()),
                ["sentinel.bin"],
            )

    def test_unserialized_tcb_has_no_posix_bypass(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging, composition = self._fixture(root)
            before = composition.read_bytes()

            with self.assertRaisesRegex(
                FoundationStagingError,
                "requires Windows retained namespace authority",
            ):
                staging_module._stage_source_controlled_components_unserialized(
                    staging=staging,
                    composition_path=composition,
                    source_root=ROOT,
                    descriptors=staging_module._REQUIRED,
                    posix_staging_authority=-1,
                    posix_composition_authority=-1,
                )

            self.assertEqual(composition.read_bytes(), before)
            self.assertEqual(list(staging.iterdir()), [])

    def test_low_level_posix_component_writer_is_permanently_fail_closed(self):
        with self.assertRaisesRegex(
            FoundationStagingError,
            "requires Windows retained namespace authority",
        ):
            staging_module._write_posix_new_regular(
                -1,
                target_name="component.py",
                data=b"must-not-write",
                relative="pkg/component.py",
            )

    def test_low_level_posix_directory_authority_never_drops_nofollow_capabilities(self):
        with TemporaryDirectory() as directory:
            staging = Path(directory)
            for attribute in ("O_NOFOLLOW", "O_DIRECTORY"):
                with self.subTest(attribute=attribute):
                    with patch.object(staging_module.os, attribute, 0, create=True):
                        with self.assertRaisesRegex(
                            FoundationStagingError,
                            f"{attribute} is required",
                        ):
                            with staging_module._retained_posix_directory(
                                staging,
                                label="staging",
                            ):
                                self.fail("missing POSIX no-follow capability was accepted")


if __name__ == "__main__":
    unittest.main()
