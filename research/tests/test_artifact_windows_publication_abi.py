import ctypes
import unittest
from unittest.mock import patch

from autotrade_research.artifacts.store import ArtifactConflict
from autotrade_research.artifacts import (
    _windows_retained_publication as windows_publication,
)
from autotrade_research.artifacts import (
    _windows_retained_publication_hardening as hardening,
)
from autotrade_research.artifacts import (
    _windows_retained_rename_fix as rename_fix,
)


class WindowsRetainedPublicationAbiTests(unittest.TestCase):
    def test_file_disposition_info_uses_one_byte_boolean(self):
        self.assertEqual(ctypes.sizeof(hardening._FileDispositionInfo), 1)

    def test_native_file_rename_information_uses_boolean_and_variable_name_offset(self):
        header = rename_fix._FileRenameInformationHeader
        self.assertEqual(header.ReplaceIfExists.size, 1)
        self.assertEqual(header.RootDirectory.offset, ctypes.sizeof(ctypes.c_void_p))
        self.assertEqual(
            rename_fix._FILE_NAME_OFFSET,
            header.FileNameLength.offset + ctypes.sizeof(ctypes.c_uint32),
        )
        self.assertLess(rename_fix._FILE_NAME_OFFSET, ctypes.sizeof(header))
        self.assertEqual(rename_fix._FILE_RENAME_INFORMATION_CLASS, 10)

    def test_native_rename_is_installed_as_retained_publication_authority(self):
        self.assertIs(windows_publication._rename_fd, rename_fix._rename_fd_native)

    def test_failed_manifest_rename_is_not_double_closed(self):
        class Store:
            pass

        store = Store()
        manifest = {"artifact_id": "00000000-0000-0000-0000-000000000001"}

        with (
            patch.object(
                windows_publication,
                "_open_mutation_directory",
                return_value=101,
            ),
            patch.object(
                windows_publication,
                "_create_temp_fd",
                return_value=(".manifest.tmp", 202),
            ),
            patch.object(
                windows_publication,
                "_publish_temp_fd",
                side_effect=PermissionError("rename denied"),
            ),
            patch.object(hardening._publication, "_write_all"),
            patch.object(
                hardening._publication,
                "_verify_staged_descriptor",
            ),
            patch.object(
                hardening._retained,
                "_assert_directory_continuity",
            ),
            patch.object(
                hardening._guard,
                "_close_windows_handle",
            ),
            patch.object(hardening.os, "close") as close_fd,
        ):
            with self.assertRaises(PermissionError):
                hardening._publish_manifest_windows(
                    store,
                    manifest=manifest,
                    replace_existing=False,
                )

        close_fd.assert_not_called()

    def test_destination_collision_preserves_artifact_conflict_contract(self):
        class Store:
            pass

        store = Store()
        manifest = {"artifact_id": "00000000-0000-0000-0000-000000000002"}
        collision = OSError("destination exists")
        collision.winerror = hardening._WINDOWS_ERROR_ALREADY_EXISTS

        with (
            patch.object(
                windows_publication,
                "_open_mutation_directory",
                return_value=101,
            ),
            patch.object(
                windows_publication,
                "_create_temp_fd",
                return_value=(".manifest.tmp", 202),
            ),
            patch.object(
                windows_publication,
                "_publish_temp_fd",
                side_effect=collision,
            ),
            patch.object(hardening._publication, "_write_all"),
            patch.object(
                hardening._publication,
                "_verify_staged_descriptor",
            ),
            patch.object(
                hardening._retained,
                "_assert_directory_continuity",
            ),
            patch.object(
                hardening._guard,
                "_close_windows_handle",
            ),
            patch.object(hardening.os, "close"),
        ):
            with self.assertRaisesRegex(
                ArtifactConflict,
                "became committed during publication",
            ):
                hardening._publish_manifest_windows(
                    store,
                    manifest=manifest,
                    replace_existing=False,
                )


if __name__ == "__main__":
    unittest.main()
