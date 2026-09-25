import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock
from unittest.mock import patch

from autotrade_research.artifacts.resource_lock import (
    ResourceLock,
    ResourceLockBusyError,
    ResourceLockError,
)


class ResourceLockRaceHardeningTests(unittest.TestCase):
    def test_dangling_symlink_is_rejected_without_creating_external_target(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable on this platform")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "resource.lock"
            external_target = root / "outside-target.lock"
            try:
                os.symlink(external_target, lock_path)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable on this platform: {exc}")

            with self.assertRaises(ResourceLockError):
                ResourceLock(lock_path).acquire()

            self.assertFalse(
                external_target.exists(),
                "unsafe create-through alias created the external target",
            )
            self.assertTrue(lock_path.is_symlink())

    @unittest.skipIf(
        os.name == "nt",
        "Windows normally forbids replacing an open lock pathname",
    )
    def test_path_swap_after_open_is_rejected_before_acquisition_succeeds(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "resource.lock"
            lock_path.write_bytes(b"\0")
            lock = ResourceLock(lock_path)
            original_open = Path.open

            def open_then_replace(path_obj, *args, **kwargs):
                handle = original_open(path_obj, *args, **kwargs)
                mode = args[0] if args else kwargs.get("mode")
                if Path(path_obj) == lock_path and mode == "r+b":
                    os.unlink(lock_path)
                    lock_path.write_bytes(b"replacement")
                return handle

            with patch.object(
                Path,
                "open",
                autospec=True,
                side_effect=open_then_replace,
            ):
                with self.assertRaisesRegex(
                    ResourceLockError,
                    "changed during acquisition",
                ):
                    lock.acquire()

            self.assertEqual(lock_path.read_bytes(), b"replacement")
            self.assertIsNone(lock._handle)

    @unittest.skipIf(
        os.name == "nt",
        "Windows normally forbids replacing an open lock pathname",
    )
    def test_path_swap_after_os_lock_is_rejected_and_old_lock_is_released(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "resource.lock"
            lock = ResourceLock(lock_path)
            original_lock = ResourceLock._lock_handle

            def lock_then_replace(handle):
                original_lock(handle)
                os.unlink(lock_path)
                lock_path.write_bytes(b"replacement-after-lock")

            with patch.object(
                ResourceLock,
                "_lock_handle",
                side_effect=lock_then_replace,
            ):
                with self.assertRaisesRegex(
                    ResourceLockError,
                    "changed during acquisition",
                ):
                    lock.acquire()

            self.assertEqual(lock_path.read_bytes(), b"replacement-after-lock")
            self.assertIsNone(lock._handle)
            with ResourceLock(lock_path):
                pass

    def test_acquisition_cleanup_close_failure_poisoning_preserves_primary(self):
        with TemporaryDirectory() as directory:
            lock = ResourceLock(Path(directory) / "resource.lock")
            handle = mock.MagicMock()
            handle.tell.return_value = 1
            handle.close.side_effect = OSError("simulated close failure")
            primary = ResourceLockBusyError("another process owns the resource lock")

            with (
                patch.object(lock, "_open_lock_handle", return_value=handle),
                patch.object(lock, "_validate_handle_identity"),
                patch.object(ResourceLock, "_lock_handle", side_effect=primary),
            ):
                with self.assertRaises(ResourceLockBusyError) as caught:
                    lock.acquire()

            self.assertIs(caught.exception, primary)
            notes = getattr(caught.exception, "__notes__", ())
            self.assertTrue(
                any(
                    "acquisition cleanup" in note
                    and "simulated close failure" in note
                    for note in notes
                ),
                f"cleanup evidence missing from primary exception notes: {notes!r}",
            )
            self.assertIs(lock._handle, handle)
            with self.assertRaisesRegex(ResourceLockError, "already held"):
                lock.acquire()

    def test_dual_unlock_and_close_failure_keeps_lock_fail_closed(self):
        with TemporaryDirectory() as directory:
            lock = ResourceLock(Path(directory) / "resource.lock")
            handle = mock.MagicMock()
            handle.close.side_effect = OSError("simulated close failure")
            lock._handle = handle

            with patch.object(
                ResourceLock,
                "_unlock_handle",
                side_effect=OSError("simulated unlock failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated unlock failure") as caught:
                    lock.release()

            notes = getattr(caught.exception, "__notes__", ())
            self.assertTrue(
                any(
                    "close also failed after unlock failure" in note
                    and "simulated close failure" in note
                    for note in notes
                ),
                f"secondary close evidence missing from exception notes: {notes!r}",
            )
            self.assertIs(lock._handle, handle)
            with self.assertRaisesRegex(ResourceLockError, "already held"):
                lock.acquire()

    def test_context_manager_preserves_primary_body_failure_on_release_failure(self):
        with TemporaryDirectory() as directory:
            lock = ResourceLock(Path(directory) / "resource.lock")

            with patch.object(
                ResourceLock,
                "_unlock_handle",
                side_effect=OSError("simulated unlock failure"),
            ):
                with self.assertRaisesRegex(ValueError, "primary failure") as caught:
                    with lock:
                        raise ValueError("primary failure")

            notes = getattr(caught.exception, "__notes__", ())
            self.assertTrue(
                any(
                    "ResourceLock release also failed" in note
                    and "simulated unlock failure" in note
                    for note in notes
                ),
                f"release evidence missing from primary exception notes: {notes!r}",
            )
            self.assertIsNone(lock._handle)

    def test_blocking_mode_requires_exact_bool(self):
        with self.assertRaisesRegex(TypeError, "blocking must be bool"):
            ResourceLock("resource.lock", blocking=1)


if __name__ == "__main__":
    unittest.main()
