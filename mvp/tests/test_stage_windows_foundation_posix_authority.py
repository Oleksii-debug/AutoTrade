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
    def test_parent_generation_swap_is_rejected_before_external_mutation(self):
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
                        "root-relative beneath/no-symlink open failed",
                    ):
                        staging_module._write_posix_new_regular(
                            staging_descriptor,
                            parent_descriptor,
                            target_name="component.py",
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
                            "POSIX retained component publication failed",
                        ):
                            staging_module._atomic_publish_regular(
                                target,
                                b"trusted-component",
                                root=staging,
                                relative="pkg/component.py",
                                posix_parent_authority=parent_descriptor,
                                posix_root_authority=staging_descriptor,
                            )

            self.assertTrue(raced)
            self.assertEqual(target.read_bytes(), b"raced-component")

    def test_missing_nested_parent_creation_fails_closed(self):
        with TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            staging.mkdir()
            parent = staging / "pkg"
            parent.mkdir()

            with staging_module._retained_posix_directory(
                staging,
                label="staging",
            ) as staging_descriptor:
                with self.assertRaisesRegex(
                    FoundationStagingError,
                    "nested directory creation is not authorized",
                ):
                    with staging_module._retained_posix_relative_directory(
                        staging_descriptor,
                        ("pkg", "nested"),
                        create=True,
                    ):
                        self.fail("unsafe nested creation must not yield authority")

            self.assertFalse((parent / "nested").exists())

    def test_posix_component_publication_fails_closed_without_openat2(self):
        with TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
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
                    with patch.object(staging_module.sys, "platform", "darwin"):
                        with self.assertRaisesRegex(
                            FoundationStagingError,
                            "requires Linux openat2 beneath/no-symlink authority",
                        ):
                            staging_module._write_posix_new_regular(
                                staging_descriptor,
                                parent_descriptor,
                                target_name="component.py",
                                data=b"trusted-component",
                                relative="pkg/component.py",
                            )

            self.assertFalse(target.exists())

    @unittest.skipUnless(sys.platform == "linux", "Linux openat2 mount-boundary regression")
    def test_openat2_rejects_mount_crossing_beneath_retained_root(self):
        proc_version = Path("/proc/version")
        if not proc_version.is_file():
            self.skipTest("/proc is unavailable on this Linux host")

        root_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | staging_module._required_posix_open_flag("O_DIRECTORY")
            | staging_module._required_posix_open_flag("O_NOFOLLOW")
        )
        root_descriptor = os.open("/", root_flags)
        try:
            with self.assertRaisesRegex(
                FoundationStagingError,
                "root-relative beneath/no-symlink open failed",
            ):
                staging_module._open_posix_beneath(
                    root_descriptor,
                    "proc/version",
                    flags=os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                    mode=0,
                    subject="POSIX mount-crossing regression",
                )
        finally:
            os.close(root_descriptor)

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
