from __future__ import annotations

import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import tools.build_windows_install_manifest as installer_manifest


class WindowsInstallerBundleReparseAuthorityTests(unittest.TestCase):
    @staticmethod
    def _stat_snapshot(*, attributes):
        return SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_dev=7,
            st_ino=11,
            st_nlink=1,
            st_size=4,
            st_mtime_ns=1,
            st_ctime_ns=1,
            st_file_attributes=attributes,
        )

    def test_release_bundle_reparse_snapshot_is_rejected_before_zip_parser(self) -> None:
        with TemporaryDirectory() as directory:
            bundle = Path(directory) / "release.zip"
            bundle.write_bytes(b"data")
            reparse = self._stat_snapshot(
                attributes=installer_manifest._WINDOWS_REPARSE_POINT
            )

            with patch.object(
                installer_manifest,
                "_assert_windows_path_chain_is_not_reparse",
            ), patch.object(
                installer_manifest.os,
                "fstat",
                return_value=reparse,
            ), patch.object(
                installer_manifest.os,
                "stat",
                return_value=reparse,
            ), patch.object(
                installer_manifest,
                "_verify_release_bundle_stream",
            ) as parser:
                with self.assertRaisesRegex(
                    installer_manifest.InstallerManifestError,
                    "release bundle must not be a Windows reparse point",
                ):
                    installer_manifest.verify_release_bundle(bundle)

            parser.assert_not_called()

    def test_reparse_ancestor_is_rejected_before_open_or_zip_parser(self) -> None:
        with TemporaryDirectory() as directory:
            bundle = Path(directory) / "nested" / "release.zip"
            bundle.parent.mkdir()
            bundle.write_bytes(b"data")
            reparse_parent = bundle.parent.absolute()

            def observed(path, *, follow_symlinks=False):
                attributes = (
                    installer_manifest._WINDOWS_REPARSE_POINT
                    if Path(path) == reparse_parent
                    else 0
                )
                return SimpleNamespace(st_file_attributes=attributes)

            with patch.object(
                installer_manifest.os,
                "stat",
                side_effect=observed,
            ), patch.object(
                installer_manifest,
                "_verify_release_bundle_stream",
            ) as parser:
                with self.assertRaisesRegex(
                    installer_manifest.InstallerManifestError,
                    "path must not contain a Windows reparse point",
                ):
                    installer_manifest.verify_release_bundle(bundle)

            parser.assert_not_called()


    def test_invalid_windows_attribute_shape_fails_closed_before_zip_parser(self) -> None:
        with TemporaryDirectory() as directory:
            bundle = Path(directory) / "release.zip"
            bundle.write_bytes(b"data")
            invalid = self._stat_snapshot(attributes="reparse")

            with patch.object(
                installer_manifest.os,
                "fstat",
                return_value=invalid,
            ), patch.object(
                installer_manifest.os,
                "stat",
                return_value=invalid,
            ), patch.object(
                installer_manifest,
                "_verify_release_bundle_stream",
            ) as parser:
                with self.assertRaisesRegex(
                    installer_manifest.InstallerManifestError,
                    "Windows file attributes are invalid",
                ):
                    installer_manifest.verify_release_bundle(bundle)

            parser.assert_not_called()

    def test_non_windows_stat_without_attribute_remains_admissible(self) -> None:
        regular = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_dev=7,
            st_ino=11,
            st_nlink=1,
            st_size=4,
            st_mtime_ns=1,
            st_ctime_ns=1,
        )
        self.assertFalse(installer_manifest._has_windows_reparse_point(regular))


if __name__ == "__main__":
    unittest.main()
