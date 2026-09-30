from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

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

    def test_fixed_local_temp_store_keeps_existing_identity_guard(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            self.assertTrue(path.exists())
            self.assertEqual(store.store_identity.canonical_path, str(path.resolve()))


if __name__ == "__main__":
    unittest.main()
