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


@unittest.skipIf(os.name == "nt", "POSIX retained-directory publication regression")
class PosixStagingNamespaceAuthorityTests(unittest.TestCase):
    def test_parent_generation_swap_cannot_mutate_external_tree(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            parent = staging / "pkg"
            parent.mkdir()

            external = root / "external"
            external.mkdir()
            external_target = external / "component.py"
            external_target.write_bytes(b"external-before")
            retained_parent = root / "retained-pkg"

            with staging_module._retained_posix_directory(
                staging,
                label="staging",
            ) as staging_descriptor:
                with staging_module._retained_posix_relative_directory(
                    staging_descriptor,
                    ("pkg",),
                    create=False,
                ) as parent_descriptor:
                    parent.rename(retained_parent)
                    parent.symlink_to(external, target_is_directory=True)

                    with self.assertRaisesRegex(
                        FoundationStagingError,
                        "openat2 beneath/no-symlink authorization failed",
                    ):
                        staging_module._write_posix_new_regular_beneath(
                            staging_descriptor,
                            data=b"trusted-component",
                            relative="pkg/component.py",
                        )

            self.assertEqual(external_target.read_bytes(), b"external-before")
            self.assertFalse((retained_parent / "component.py").exists())

    def test_raced_component_leaf_is_not_overwritten(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            parent = staging / "pkg"
            parent.mkdir()
            target = parent / "component.py"

            with staging_module._retained_posix_directory(
                staging,
                label="staging",
            ) as staging_descriptor:
                with staging_module._retained_posix_relative_directory(
                    staging_descriptor,
                    ("pkg",),
                    create=False,
                ) as parent_descriptor:
                    original_preflight = staging_module._preflight_existing_chain
                    raced = False

                    def race_after_preflight(root_path, relative, **kwargs):
                        nonlocal raced
                        result = original_preflight(root_path, relative, **kwargs)
                        if not raced and root_path == staging and relative == "pkg/component.py":
                            descriptor = os.open(
                                "component.py",
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                0o600,
                                dir_fd=parent_descriptor,
                            )
                            try:
                                os.write(descriptor, b"raced-component")
                                os.fsync(descriptor)
                            finally:
                                os.close(descriptor)
                            raced = True
                        return result

                    with patch.object(
                        staging_module,
                        "_preflight_existing_chain",
                        side_effect=race_after_preflight,
                    ):
                        with self.assertRaisesRegex(
                            FoundationStagingError,
                            "openat2 beneath/no-symlink authorization failed",
                        ):
                            staging_module._atomic_publish_regular(
                                target,
                                b"trusted-component",
                                root=staging,
                                relative="pkg/component.py",
                                posix_root_authority=staging_descriptor,
                            )

            self.assertTrue(raced)
            self.assertEqual(target.read_bytes(), b"raced-component")

    def test_required_openat2_failure_precedes_parent_and_component_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
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
            before = composition.read_bytes()

            with patch.object(
                staging_module,
                "_require_linux_openat2_beneath_authority",
                side_effect=FoundationStagingError("required openat2 unavailable"),
            ):
                with self.assertRaisesRegex(
                    FoundationStagingError,
                    "required openat2 unavailable",
                ):
                    stage_windows_foundation(
                        staging=staging,
                        composition_path=composition,
                    )

            self.assertEqual(composition.read_bytes(), before)
            self.assertEqual(list(staging.iterdir()), [])

    def test_missing_nested_parent_is_not_created_through_descendant_fd(self):
        with TemporaryDirectory() as directory:
            staging = Path(directory)
            with staging_module._retained_posix_directory(
                staging,
                label="staging",
            ) as staging_descriptor:
                with self.assertRaisesRegex(
                    FoundationStagingError,
                    "missing nested staging parent",
                ):
                    with staging_module._retained_posix_relative_directory(
                        staging_descriptor,
                        ("first", "second"),
                        create=True,
                    ):
                        self.fail("unsafe nested parent creation was accepted")

            self.assertFalse((staging / "first").exists())

    def test_source_root_ancestor_symlink_is_rejected_before_staging_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            alias_parent = root / "source-parent-alias"
            alias_parent.symlink_to(ROOT.parent, target_is_directory=True)
            aliased_source = alias_parent / ROOT.name
            self.assertTrue(aliased_source.is_dir())

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
            before = composition.read_bytes()

            with self.assertRaisesRegex(
                FoundationStagingError,
                "source_root.*symlink|source_root.*alias",
            ):
                stage_windows_foundation(
                    staging=staging,
                    composition_path=composition,
                    source_root=aliased_source,
                )

            self.assertEqual(composition.read_bytes(), before)
            self.assertEqual(list(staging.iterdir()), [])

    def test_staging_root_ancestor_symlink_is_rejected_before_any_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            external_parent = root / "external-parent"
            real_staging = external_parent / "real-staging"
            real_staging.mkdir(parents=True)
            alias_parent = root / "staging-parent-alias"
            alias_parent.symlink_to(external_parent, target_is_directory=True)
            staging = alias_parent / "real-staging"

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
            before = composition.read_bytes()

            with self.assertRaisesRegex(
                FoundationStagingError,
                "staging.*symlink|staging.*alias",
            ):
                stage_windows_foundation(
                    staging=staging,
                    composition_path=composition,
                )

            self.assertEqual(composition.read_bytes(), before)
            self.assertEqual(list(real_staging.iterdir()), [])

    def test_composition_parent_ancestor_symlink_is_rejected_before_any_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()

            external_parent = root / "external-parent"
            real_parent = external_parent / "composition-dir"
            real_parent.mkdir(parents=True)
            real_composition = real_parent / "composition.json"
            real_composition.write_text(
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
            before = real_composition.read_bytes()
            alias_parent = root / "composition-parent-alias"
            alias_parent.symlink_to(external_parent, target_is_directory=True)
            composition = alias_parent / "composition-dir" / "composition.json"

            with self.assertRaisesRegex(
                FoundationStagingError,
                "composition parent.*symlink|composition parent.*alias",
            ):
                stage_windows_foundation(
                    staging=staging,
                    composition_path=composition,
                )

            self.assertEqual(real_composition.read_bytes(), before)
            self.assertEqual(list(staging.iterdir()), [])

    def test_staging_root_ancestor_swap_before_retention_cannot_redirect_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lexical_parent = root / "lexical-parent"
            staging = lexical_parent / "staging"
            staging.mkdir(parents=True)

            external_parent = root / "external-parent"
            external_staging = external_parent / "staging"
            external_staging.mkdir(parents=True)
            retained_parent = root / "retained-parent"

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
            before = composition.read_bytes()
            original = staging_module._require_posix_directory_no_alias
            swapped = False

            def swap_after_validation(path, *, label):
                nonlocal swapped
                result = original(path, label=label)
                if label == "staging" and Path(path) == staging and not swapped:
                    lexical_parent.rename(retained_parent)
                    lexical_parent.symlink_to(external_parent, target_is_directory=True)
                    swapped = True
                return result

            with patch.object(
                staging_module,
                "_require_posix_directory_no_alias",
                side_effect=swap_after_validation,
            ):
                with self.assertRaisesRegex(
                    FoundationStagingError,
                    "staging changed before retention|staging changed during retention",
                ):
                    stage_windows_foundation(
                        staging=staging,
                        composition_path=composition,
                    )

            self.assertTrue(swapped)
            self.assertEqual(composition.read_bytes(), before)
            self.assertEqual(list(external_staging.iterdir()), [])
            self.assertEqual(list((retained_parent / "staging").iterdir()), [])

    def test_composition_parent_swap_after_lock_cannot_redirect_manifest_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()

            lexical_parent = root / "composition-parent"
            lexical_parent.mkdir()
            composition = lexical_parent / "composition.json"
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
            retained_parent = root / "retained-composition-parent"

            external_parent = root / "external-composition-parent"
            external_parent.mkdir()
            external_composition = external_parent / "composition.json"
            external_composition.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "product": "AutoTrade",
                        "source_sha": "0" * 40,
                        "components": [],
                    }
                ),
                encoding="utf-8",
            )
            external_before = external_composition.read_bytes()

            original = staging_module._require_posix_directory_no_alias
            composition_checks = 0
            swapped = False

            def swap_after_locked_parent_validation(path, *, label):
                nonlocal composition_checks, swapped
                result = original(path, label=label)
                if label == "composition parent" and Path(path) == lexical_parent:
                    composition_checks += 1
                    if composition_checks == 2 and not swapped:
                        lexical_parent.rename(retained_parent)
                        lexical_parent.symlink_to(
                            external_parent,
                            target_is_directory=True,
                        )
                        swapped = True
                return result

            with patch.object(
                staging_module,
                "_require_posix_directory_no_alias",
                side_effect=swap_after_locked_parent_validation,
            ):
                staged = stage_windows_foundation(
                    staging=staging,
                    composition_path=composition,
                )

            self.assertTrue(swapped)
            self.assertEqual(external_composition.read_bytes(), external_before)
            retained_composition = retained_parent / "composition.json"
            retained_payload = json.loads(retained_composition.read_text(encoding="utf-8"))
            self.assertEqual(retained_payload["source_sha"], SOURCE_SHA)
            self.assertEqual(
                {item["path"] for item in retained_payload["components"]},
                {item["path"] for item in staged},
            )
            self.assertTrue(staged)

    def test_posix_directory_authority_never_silently_drops_nofollow_capabilities(self):
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
