import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.disk_reserve as disk_reserve_module
from mvp.autotrade_mvp.disk_reserve import (
    DiskReserveError,
    EmergencyDiskReserve,
    ReserveReleaseReason,
)


class EmergencyDiskReserveTests(unittest.TestCase):
    def test_provision_is_exact_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recovery" / "reserve.bin"
            reserve = EmergencyDiskReserve(path, reserve_bytes=4097)

            first = reserve.provision()
            second = reserve.provision()

            self.assertTrue(first.available_for_emergency)
            self.assertTrue(first.exact_size)
            self.assertEqual(path.stat().st_size, 4097)
            self.assertEqual(second, first)

    def test_existing_unverified_path_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reserve.bin"
            path.write_bytes(b"foreign-data")
            reserve = EmergencyDiskReserve(path, reserve_bytes=1024)

            with self.assertRaisesRegex(DiskReserveError, "refusing to overwrite"):
                reserve.provision()

            self.assertEqual(path.read_bytes(), b"foreign-data")
            self.assertFalse(reserve.status().available_for_emergency)

    def test_release_requires_explicit_reason_and_removes_only_verified_reserve(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reserve.bin"
            reserve = EmergencyDiskReserve(path, reserve_bytes=128)
            reserve.provision()

            with self.assertRaisesRegex(DiskReserveError, "explicit"):
                reserve.release_for_emergency(reason="JOURNAL_WRITE_FAILURE")

            released = reserve.release_for_emergency(
                reason=ReserveReleaseReason.JOURNAL_WRITE_FAILURE
            )
            self.assertFalse(released.present)
            self.assertFalse(path.exists())

    def test_same_sized_foreign_file_is_never_treated_as_owned_reserve(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reserve.bin"
            path.write_bytes(b"x" * 128)
            reserve = EmergencyDiskReserve(path, reserve_bytes=128)

            status = reserve.status()
            self.assertTrue(status.present)
            self.assertTrue(status.exact_size)
            self.assertFalse(status.available_for_emergency)
            with self.assertRaisesRegex(DiskReserveError, "no verified"):
                reserve.release_for_emergency(
                    reason=ReserveReleaseReason.RECOVERY_CRITICAL
                )
            self.assertEqual(path.read_bytes(), b"x" * 128)

    def test_release_refuses_missing_or_wrong_sized_reserve(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reserve.bin"
            reserve = EmergencyDiskReserve(path, reserve_bytes=128)
            with self.assertRaisesRegex(DiskReserveError, "no verified"):
                reserve.release_for_emergency(
                    reason=ReserveReleaseReason.RECOVERY_CRITICAL
                )

            path.write_bytes(b"x")
            with self.assertRaisesRegex(DiskReserveError, "no verified"):
                reserve.release_for_emergency(
                    reason=ReserveReleaseReason.RECOVERY_CRITICAL
                )
            self.assertEqual(path.read_bytes(), b"x")

    def test_restore_after_recovery_requires_writable_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reserve.bin"
            reserve = EmergencyDiskReserve(path, reserve_bytes=256)
            reserve.provision()
            reserve.release_for_emergency(
                reason=ReserveReleaseReason.JOURNAL_WRITE_FAILURE
            )

            with self.assertRaisesRegex(DiskReserveError, "journal"):
                reserve.restore_after_recovery(journal_writable=False)
            self.assertFalse(path.exists())

            restored = reserve.restore_after_recovery(journal_writable=True)
            self.assertTrue(restored.available_for_emergency)
            self.assertEqual(path.stat().st_size, 256)

    @unittest.skipUnless(sys.platform == "win32", "native Windows reserve authority")
    def test_windows_release_blocks_leaf_and_parent_replacement_during_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "recovery"
            path = parent / "reserve.bin"
            replacement = parent / "replacement.bin"
            moved_parent = root / "recovery-moved"
            reserve = EmergencyDiskReserve(path, reserve_bytes=4097)
            reserve.provision()
            replacement.write_bytes(b"replacement")

            original = reserve._status_from_descriptor
            observed = {"leaf": False, "parent": False}

            def verify_while_attacker_attempts_rebind(descriptor):
                status = original(descriptor)
                with self.assertRaises(OSError):
                    os.replace(replacement, path)
                observed["leaf"] = True
                with self.assertRaises(OSError):
                    parent.rename(moved_parent)
                observed["parent"] = True
                return status

            with patch.object(
                reserve,
                "_status_from_descriptor",
                side_effect=verify_while_attacker_attempts_rebind,
            ):
                released = reserve.release_for_emergency(
                    reason=ReserveReleaseReason.RECOVERY_CRITICAL
                )

            self.assertEqual(observed, {"leaf": True, "parent": True})
            self.assertFalse(released.present)
            self.assertFalse(path.exists())
            self.assertTrue(replacement.exists())
            self.assertTrue(parent.is_dir())
            self.assertFalse(moved_parent.exists())

    @unittest.skipUnless(sys.platform == "win32", "native Windows reserve authority")
    def test_windows_hardlink_alias_fails_closed_before_reserve_use(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "reserve.bin"
            alias = root / "reserve-alias.bin"
            reserve = EmergencyDiskReserve(path, reserve_bytes=4097)
            reserve.provision()
            os.link(path, alias)

            with self.assertRaisesRegex(
                DiskReserveError,
                "namespace authority verification failed",
            ):
                reserve.status()
            with self.assertRaisesRegex(
                DiskReserveError,
                "release authority failed",
            ):
                reserve.release_for_emergency(
                    reason=ReserveReleaseReason.RECOVERY_CRITICAL
                )

            self.assertTrue(path.exists())
            self.assertTrue(alias.exists())

    @unittest.skipUnless(sys.platform == "win32", "native Windows reserve authority")
    def test_windows_locality_rejection_precedes_parent_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "must-not-exist" / "reserve.bin"
            with patch.object(
                disk_reserve_module,
                "require_qualified_local_filesystem_path",
                side_effect=disk_reserve_module.LocalFilesystemQualificationError(
                    "unqualified"
                ),
            ):
                with self.assertRaisesRegex(
                    DiskReserveError,
                    "qualified Windows filesystem authority",
                ):
                    EmergencyDiskReserve(path, reserve_bytes=4097)

            self.assertFalse(path.parent.exists())

    def test_configuration_and_boolean_inputs_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reserve.bin"
            for invalid in (0, 1, -1, True, 1.5, "1024"):
                with self.subTest(reserve_bytes=invalid):
                    with self.assertRaises(DiskReserveError):
                        EmergencyDiskReserve(path, reserve_bytes=invalid)

            reserve = EmergencyDiskReserve(path, reserve_bytes=64)
            with self.assertRaisesRegex(DiskReserveError, "boolean"):
                reserve.restore_after_recovery(journal_writable=1)


if __name__ == "__main__":
    unittest.main()
