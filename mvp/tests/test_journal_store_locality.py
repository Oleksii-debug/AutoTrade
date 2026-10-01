import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.persistence as persistence_module
from mvp.autotrade_mvp.persistence import JournalStore


@unittest.skipUnless(sys.platform == "win32", "Windows filesystem qualification")
class WindowsJournalStoreLocalityTests(unittest.TestCase):
    def test_unc_path_is_rejected_before_path_freeze_or_database_creation(self):
        with patch(
            "mvp.autotrade_mvp.persistence.freeze_database_path"
        ) as freeze_database_path:
            with self.assertRaisesRegex(
                RuntimeError,
                "qualified local filesystem",
            ):
                JournalStore(r"\\server\share\autotrade\journal.sqlite3")
        freeze_database_path.assert_not_called()

    def test_mapped_remote_drive_is_rejected_before_path_freeze_or_wal(self):
        class DriveType:
            argtypes = None
            restype = None

            def __call__(self, root):
                self.last_root = root
                return 4  # DRIVE_REMOTE

        class Kernel32:
            def __init__(self):
                self.GetDriveTypeW = DriveType()

        kernel32 = Kernel32()
        with (
            patch("ctypes.WinDLL", return_value=kernel32),
            patch(
                "mvp.autotrade_mvp.persistence.freeze_database_path"
            ) as freeze_database_path,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "qualified local filesystem",
            ):
                JournalStore(r"Z:\autotrade\journal.sqlite3")
        freeze_database_path.assert_not_called()
        self.assertEqual(kernel32.GetDriveTypeW.last_root, "Z:\\")

    def test_relative_path_is_frozen_before_locality_admission_can_change_cwd(self):
        original_cwd = Path.cwd()
        with TemporaryDirectory() as first_dir, TemporaryDirectory() as second_dir:
            first = Path(first_dir)
            second = Path(second_dir)
            observed = []
            store = None
            try:
                os.chdir(first)
                original_require = (
                    persistence_module.require_qualified_local_filesystem_path
                )

                def qualify_then_change_cwd(path):
                    observed.append(Path(path))
                    original_require(path)
                    os.chdir(second)

                with patch.object(
                    persistence_module,
                    "require_qualified_local_filesystem_path",
                    new=qualify_then_change_cwd,
                ):
                    store = JournalStore("state/journal.sqlite3")
            finally:
                # Restore before TemporaryDirectory teardown on Windows.
                os.chdir(original_cwd)

            expected = first / "state" / "journal.sqlite3"
            unexpected = second / "state" / "journal.sqlite3"
            self.assertIsNotNone(store)
            self.assertEqual(len(observed), 1)
            self.assertTrue(observed[0].is_absolute())
            self.assertEqual(Path(store.path), expected)
            self.assertTrue(expected.is_file())
            self.assertFalse(unexpected.exists())

    def test_fixed_local_temp_store_keeps_existing_identity_guard(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            self.assertTrue(path.exists())
            self.assertTrue(Path(store.store_identity.canonical_path).is_absolute())
            self.assertEqual(store.store_identity.canonical_path, str(store.path))


if __name__ == "__main__":
    unittest.main()
