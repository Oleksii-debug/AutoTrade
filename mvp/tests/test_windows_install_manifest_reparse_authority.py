from __future__ import annotations

from contextlib import contextmanager
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

    def test_windows_retained_namespace_is_held_through_zip_and_final_identity(self) -> None:
        with TemporaryDirectory() as directory:
            bundle = (Path(directory) / "release.zip").absolute()
            bundle.write_bytes(b"data")
            active = {"parent": False, "leaf": False}

            @contextmanager
            def retained_parent(path, *, create=False):
                self.assertEqual(Path(path), bundle)
                self.assertFalse(create)
                active["parent"] = True
                try:
                    yield object()
                finally:
                    active["parent"] = False

            @contextmanager
            def retained_leaf(authority, *, target_name, subject):
                self.assertTrue(active["parent"])
                self.assertEqual(target_name, bundle.name)
                self.assertEqual(subject, "release bundle")
                descriptor = os.open(bundle, os.O_RDONLY)
                active["leaf"] = True
                try:
                    yield descriptor
                finally:
                    active["leaf"] = False
                    os.close(descriptor)

            original_identity = installer_manifest._assert_open_file_identity
            identity_cuts = []

            def identity(path, stream, *, name):
                self.assertTrue(active["parent"])
                self.assertTrue(active["leaf"])
                identity_cuts.append(name)
                return original_identity(path, stream, name=name)

            def parser(stream, digest):
                self.assertTrue(active["parent"])
                self.assertTrue(active["leaf"])
                self.assertTrue(digest.startswith("sha256:"))
                return {"verified": True}

            with patch.object(
                installer_manifest.sys,
                "platform",
                "win32",
            ), patch.object(
                installer_manifest,
                "retain_windows_parent_namespace",
                side_effect=retained_parent,
            ), patch.object(
                installer_manifest,
                "retain_windows_regular_file",
                side_effect=retained_leaf,
            ), patch.object(
                installer_manifest,
                "_verify_release_bundle_stream",
                side_effect=parser,
            ), patch.object(
                installer_manifest,
                "_assert_open_file_identity",
                side_effect=identity,
            ):
                self.assertEqual(
                    installer_manifest.verify_release_bundle(bundle),
                    {"verified": True},
                )

            self.assertEqual(identity_cuts, ["release bundle", "release bundle"])
            self.assertFalse(active["parent"])
            self.assertFalse(active["leaf"])

    def test_windows_verification_body_exception_is_not_reclassified_as_namespace_failure(self) -> None:
        with TemporaryDirectory() as directory:
            bundle = (Path(directory) / "release.zip").absolute()
            bundle.write_bytes(b"data")

            @contextmanager
            def retained_parent(path, *, create=False):
                yield object()

            @contextmanager
            def retained_leaf(authority, *, target_name, subject):
                descriptor = os.open(bundle, os.O_RDONLY)
                try:
                    yield descriptor
                finally:
                    os.close(descriptor)

            with patch.object(
                installer_manifest.sys,
                "platform",
                "win32",
            ), patch.object(
                installer_manifest,
                "retain_windows_parent_namespace",
                side_effect=retained_parent,
            ), patch.object(
                installer_manifest,
                "retain_windows_regular_file",
                side_effect=retained_leaf,
            ), patch.object(
                installer_manifest,
                "_verify_release_bundle_stream",
                side_effect=RuntimeError("parser sentinel"),
            ):
                with self.assertRaisesRegex(RuntimeError, "parser sentinel"):
                    installer_manifest.verify_release_bundle(bundle)


    def test_windows_acquisition_cleanup_failure_does_not_mask_primary_blocker(self) -> None:
        with TemporaryDirectory() as directory:
            bundle = (Path(directory) / "release.zip").absolute()
            bundle.write_bytes(b"data")

            @contextmanager
            def retained_parent(path, *, create=False):
                try:
                    yield object()
                finally:
                    raise OSError("cleanup sentinel")

            @contextmanager
            def rejected_leaf(authority, *, target_name, subject):
                raise RuntimeError("leaf sentinel")
                yield  # pragma: no cover

            with patch.object(
                installer_manifest.sys,
                "platform",
                "win32",
            ), patch.object(
                installer_manifest,
                "retain_windows_parent_namespace",
                side_effect=retained_parent,
            ), patch.object(
                installer_manifest,
                "retain_windows_regular_file",
                side_effect=rejected_leaf,
            ), patch.object(
                installer_manifest,
                "_verify_release_bundle_stream",
            ) as parser:
                with self.assertRaises(installer_manifest.InstallerManifestError) as captured:
                    installer_manifest.verify_release_bundle(bundle)

            cause = captured.exception.__cause__
            self.assertIsInstance(cause, RuntimeError)
            self.assertIn("leaf sentinel", str(cause))
            notes = getattr(cause, "__notes__", [])
            self.assertTrue(
                any(
                    "retained Windows namespace cleanup also failed" in note
                    and "cleanup sentinel" in note
                    for note in notes
                )
            )
            parser.assert_not_called()


    def test_windows_parent_namespace_rejection_prevents_leaf_open_and_zip_parse(self) -> None:
        with TemporaryDirectory() as directory:
            bundle = (Path(directory) / "release.zip").absolute()
            bundle.write_bytes(b"data")

            @contextmanager
            def rejected_parent(path, *, create=False):
                raise RuntimeError("ancestor is a reparse point")
                yield  # pragma: no cover

            with patch.object(
                installer_manifest.sys,
                "platform",
                "win32",
            ), patch.object(
                installer_manifest,
                "retain_windows_parent_namespace",
                side_effect=rejected_parent,
            ), patch.object(
                installer_manifest,
                "retain_windows_regular_file",
            ) as retained_leaf, patch.object(
                installer_manifest,
                "_verify_release_bundle_stream",
            ) as parser:
                with self.assertRaisesRegex(
                    installer_manifest.InstallerManifestError,
                    "retained Windows namespace authority failed",
                ):
                    installer_manifest.verify_release_bundle(bundle)

            retained_leaf.assert_not_called()
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
