from pathlib import Path
import tempfile
import unittest

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

    def test_configuration_and_boolean_inputs_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reserve.bin"
            for invalid in (0, -1, True, 1.5, "1024"):
                with self.subTest(reserve_bytes=invalid):
                    with self.assertRaises(DiskReserveError):
                        EmergencyDiskReserve(path, reserve_bytes=invalid)

            reserve = EmergencyDiskReserve(path, reserve_bytes=64)
            with self.assertRaisesRegex(DiskReserveError, "boolean"):
                reserve.restore_after_recovery(journal_writable=1)


if __name__ == "__main__":
    unittest.main()
